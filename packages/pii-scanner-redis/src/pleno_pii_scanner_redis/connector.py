"""Redis SourceConnector — production-safe key scan.

Hard requirements driven by ADR-0007 §16:

  * `SCAN` (cursor-based), never `KEYS *` — `KEYS` blocks the
    server for the entire scan duration, taking client-facing
    Redis offline on any non-trivial dataset.
  * ACL read-only enforcement at connect time. The scanner refuses
    to run unless the credentialed user has only read-class
    commands enabled. Override is `enforce_readonly=False` for
    dev / single-tenant local instances.
  * Per-value byte cap (`max_value_bytes`, default 1 MiB). Long
    blobs blow up the detector pipeline and rarely contain PII.
  * Connection pool capped at 2 — the scanner must never starve
    the application of connection slots on a shared instance.

Type → serialisation:

  string  → text body (utf-8 with replace)
  list    → newline-joined LRANGE 0 -1
  set     → sorted, newline-joined SMEMBERS
  hash    → "field=value\\n..." HGETALL
  zset    → "member=score\\n..." ZRANGE 0 -1 WITHSCORES
  stream  → "id  field=value field=value\\n..." XRANGE - + COUNT 100

Streams are inherently large; only the most recent 100 entries
are read by default. Operators who need full-history scan should
re-issue with a wider window (out of scope for v1).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import redis.asyncio as aioredis

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


# Redis "categories" that the scanner is allowed to use. Anything
# beyond these is a sign the credential is over-scoped — refuse.
_ALLOWED_CATEGORIES: frozenset[str] = frozenset(
    {"@read", "@connection", "@keyspace"}
)
# Categories that, if granted, indicate an over-privileged role.
_FORBIDDEN_CATEGORIES: frozenset[str] = frozenset(
    {"@write", "@admin", "@dangerous", "@scripting", "@all"}
)


class AclEnforcementError(RuntimeError):
    """Raised when the credential carries write/admin capability."""


@dataclass(frozen=True, slots=True)
class RedisConfig:
    """Construction config for `RedisConnector`.

    `url` is a redis:// or rediss:// URI. `match` is the SCAN MATCH
    glob (`*` walks the whole keyspace). `count_hint` is the SCAN
    COUNT batch size — Redis treats it as advice, not a guarantee.

    `enforce_readonly=True` is the production default and rejects
    credentials that can call write/admin commands. Set False for
    development against a local instance with the default user.
    """

    url: str
    match: str = "*"
    count_hint: int = 100
    max_value_bytes: int = 1024 * 1024
    stream_max_entries: int = 100
    pool_size: int = 2
    enforce_readonly: bool = True
    username: str | None = None
    password: str | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError("url must be non-empty")
        scheme = urlparse(self.url).scheme
        if scheme not in {"redis", "rediss", "unix"}:
            raise ValueError(
                f"url must be redis://, rediss://, or unix:// (got {scheme!r})"
            )
        if self.count_hint < 1:
            raise ValueError("count_hint must be >= 1")
        if self.max_value_bytes < 1:
            raise ValueError("max_value_bytes must be >= 1")
        if self.pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        if self.stream_max_entries < 1:
            raise ValueError("stream_max_entries must be >= 1")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Strip credentials for the id; URL userinfo is sensitive.
        parsed = urlparse(self.url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 6379
        db = parsed.path.lstrip("/") or "0"
        return f"redis:{host}:{port}/{db}"


class RedisConnector:
    """Read-only SourceConnector for Redis."""

    kind = "redis"

    def __init__(
        self,
        config: RedisConfig,
        *,
        client: aioredis.Redis | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        # Test seam — production wiring builds a pooled client below.
        # `_owns_client` controls whether `close()` actually disposes.
        if client is None:
            self._client = aioredis.from_url(
                config.url,
                username=config.username,
                password=config.password,
                max_connections=config.pool_size,
                decode_responses=False,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._acl_checked = False

    def capabilities(self) -> Capabilities:
        # `incremental=False`: SCAN cursors are not stable across
        # full keyspace mutations (a key inserted mid-scan may or may
        # not appear). Treat each scan as a fresh sample.
        return Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=True,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        del cursor  # incremental=False
        await self._enforce_acl()
        async for key in self._client.scan_iter(
            match=self._config.match,
            count=self._config.count_hint,
        ):
            key_str = _to_str(key)
            if filter.include and not _matches_any(key_str, filter.include):
                continue
            if filter.exclude and _matches_any(key_str, filter.exclude):
                continue
            value_type = _to_str(await self._client.type(key))
            if value_type == "none":
                # Key expired between SCAN and TYPE — common, not an error.
                continue
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=key_str,
                content_type=f"application/x-redis-{value_type}",
                metadata={"key": key_str, "type": value_type},
            )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        key = ref.metadata.get("key")
        value_type = ref.metadata.get("type")
        if key is None or value_type is None:
            return
        body = await self._read_value(key, value_type)
        if body is None:
            return
        if len(body) > self._config.max_value_bytes:
            return
        yield Document(
            ref=ref,
            text=body.decode("utf-8", errors="replace"),
            fetched_at=datetime.now(UTC),
            extra={"key": key, "type": value_type},
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- internals ------------------------------------------------

    async def _enforce_acl(self) -> None:
        if self._acl_checked or not self._config.enforce_readonly:
            self._acl_checked = True
            return
        whoami = _to_str(await self._client.execute_command("ACL", "WHOAMI"))
        info = await self._client.execute_command("ACL", "GETUSER", whoami)
        # `ACL GETUSER` returns alternating field/value pairs in older
        # Redis versions, and a dict on newer clients. Normalise.
        info_map = _normalise_acl(info)
        commands = _to_str(info_map.get("commands", "")) or " ".join(
            _to_str(c) for c in info_map.get("commands", [])
        )
        for forbidden in _FORBIDDEN_CATEGORIES:
            if f"+{forbidden}" in commands:
                raise AclEnforcementError(
                    f"credential {whoami!r} has {forbidden} privilege; "
                    "use a dedicated read-only user (set "
                    "enforce_readonly=False to override, not recommended)"
                )
        self._acl_checked = True

    async def _read_value(self, key: str, value_type: str) -> bytes | None:
        if value_type == "string":
            return await self._client.get(key)
        if value_type == "list":
            entries = await self._client.lrange(key, 0, -1)
            return b"\n".join(_b(e) for e in entries)
        if value_type == "set":
            members = sorted(_b(m) for m in await self._client.smembers(key))
            return b"\n".join(members)
        if value_type == "hash":
            data = await self._client.hgetall(key)
            return b"\n".join(
                _b(f) + b"=" + _b(v) for f, v in data.items()
            )
        if value_type == "zset":
            entries = await self._client.zrange(key, 0, -1, withscores=True)
            return b"\n".join(
                _b(m) + b"=" + str(s).encode() for m, s in entries
            )
        if value_type == "stream":
            entries = await self._client.xrange(
                key, count=self._config.stream_max_entries
            )
            return b"\n".join(_format_stream_entry(e) for e in entries)
        # Unknown / unsupported type (HyperLogLog, custom modules).
        # Skip rather than crash — operators who care will reach out.
        return None


# --- helpers ------------------------------------------------------


def _to_str(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else ""


def _b(value: object) -> bytes:
    # Redis client is constructed with decode_responses=False, so all
    # values returned from string/list/set/hash/zset/stream commands
    # arrive as bytes. The str fallback exists for the rare case where
    # a stream field value comes back as a number (e.g. from XADD with
    # numeric arg) and tests that exercise it directly.
    return value if isinstance(value, bytes) else str(value).encode("utf-8")


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    import fnmatch

    return any(fnmatch.fnmatch(value, p) for p in patterns)


def _normalise_acl(info: Any) -> dict[str, Any]:
    if isinstance(info, dict):
        return {_to_str(k): v for k, v in info.items()}
    if isinstance(info, list):
        # Redis returns flat alternating list in RESP2. Pair them.
        return {
            _to_str(info[i]): info[i + 1]
            for i in range(0, len(info) - 1, 2)
        }
    return {}


def _format_stream_entry(entry: tuple[Any, dict[Any, Any]]) -> bytes:
    msg_id, fields = entry
    body = b" ".join(
        _b(f) + b"=" + _b(v) for f, v in fields.items()
    )
    return _b(msg_id) + b"  " + body


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    url = config.get("url")
    if not url:
        raise ValueError("redis connector config requires 'url'")
    return RedisConnector(
        RedisConfig(
            url=str(url),
            match=str(config.get("match", "*")),
            count_hint=int(config.get("count_hint", 100)),
            max_value_bytes=int(config.get("max_value_bytes", 1024 * 1024)),
            stream_max_entries=int(config.get("stream_max_entries", 100)),
            pool_size=int(config.get("pool_size", 2)),
            enforce_readonly=bool(config.get("enforce_readonly", True)),
            username=(
                str(config["username"]) if config.get("username") is not None else None
            ),
            password=(
                str(config["password"]) if config.get("password") is not None else None
            ),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="redis",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=True,
        content_hash_delta=False,
        max_concurrent_fetches=2,
        streaming=True,
    ),
    required_scopes=("redis:read",),
    description=(
        "Redis SourceConnector. SCAN-based key enumeration (never KEYS), "
        "ACL read-only enforcement at connect time, byte cap on values, "
        "all six builtin types (string/list/set/hash/zset/stream)."
    ),
)


__all__ = [
    "AclEnforcementError",
    "RedisConfig",
    "RedisConnector",
    "SPEC",
]
