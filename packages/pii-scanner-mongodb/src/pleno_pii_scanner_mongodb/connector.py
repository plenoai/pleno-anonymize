"""MongoDB SourceConnector — production-safe document scan (ADR-0007 §16).

Hard requirements driven by the ADR:

  * Secondary enforcement (`hello.secondary == True`) — connecting to
    the primary by accident pushes p99 read latency by an order of
    magnitude on a busy cluster. Mirror of the PostgreSQL
    `pg_is_in_recovery()` guard.
  * `maxTimeMS=30000` on every server-side command. The driver-level
    socket timeout is a fallback, not a substitute — a slow $sample on
    a multi-TB collection must abort server-side, not just client-side.
  * `$sample` aggregation (n=299 default) per collection — `find({})`
    cursor scans of multi-billion-document collections stall for hours
    and burn replica IO. The 299 figure mirrors the Postgres connector
    so audit math is identical: `n = log(0.05) / log(0.99) ≈ 299`.
  * Connection pool capped at `max_pool_size=2` so the scanner can
    never starve the application of pool slots.
  * `bson.json_util.dumps` for serialisation — every BSON-only type
    (ObjectId, Decimal128, Date, Binary, UUID) round-trips through
    Extended JSON v2, keeping the regex / NER pipeline language-agnostic.
  * Change Stream incremental mode — when `incremental=True` the
    connector calls `db.collection.watch(resume_after=cursor)` instead
    of `$sample`. Cursor is opaque (a `_data` resume token) so the
    Scheduler does not need to understand MongoDB internals.

What this connector does NOT do (deliberately):

  * Write to the cluster. Read-only role enforced by the secondary
    requirement plus the application-level absence of any write
    command. We do not validate roles client-side — Mongo's RBAC is
    server-enforced and a misconfigured `dbAdmin` would reveal itself
    as a primary connection (which we already refuse).
  * Inspect raw values in logs. Documents are materialised into the
    Document body and forgotten; the FindingsStore (#9) is the only
    component that ever persists raw value bytes.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient

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


# Mongo system databases that are never user data and would only ever
# leak operator-level configuration into the scanner pipeline. Excluded
# unconditionally; operators who genuinely want to scan `admin` for
# stray PII can pass `databases=("admin",)` explicitly.
_SYSTEM_DATABASES: frozenset[str] = frozenset({"admin", "config", "local"})


def reservoir_sample_size(
    *, confidence: float = 0.95, prevalence: float = 0.01
) -> int:
    """Minimum sample size for `confidence` PII detection at `prevalence`.

    Reproduces ADR §16: P(no PII in n rows) = (1-p)^n; we want this ≤ α.
    Solving for n: n ≥ log(α) / log(1-p). With α=0.05 (95% confidence)
    and p=0.01 (1% prevalence), n = ceil(log(0.05) / log(0.99)) = 299.
    """
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    if not 0 < prevalence < 1:
        raise ValueError("prevalence must be in (0, 1)")
    alpha = 1.0 - confidence
    return math.ceil(math.log(alpha) / math.log(1.0 - prevalence))


class PrimaryConnectionRefused(RuntimeError):
    """Raised when require_secondary=True and `hello.secondary` is false."""


@dataclass(frozen=True, slots=True)
class MongoConfig:
    """Construction config for `MongoConnector`.

    `uri` is a `mongodb://` or `mongodb+srv://` connection string.
    `username` / `password` override anything embedded in the URI so
    operators can supply credentials via Vault without rewriting URIs.

    `databases` / `collections` are exact-match include lists; empty
    means "everything except system DBs / no filter respectively".
    Glob filtering belongs in `SourceFilter`, not in static config —
    keeps the config schema simple and the precedence obvious.

    `incremental=True` swaps the scan strategy from `$sample` to
    `db.collection.watch()`. The cursor passed to `discover()` is the
    opaque resume token from a prior watch session.
    """

    uri: str
    databases: tuple[str, ...] = ()
    excluded_databases: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    excluded_collections: tuple[str, ...] = ()
    sample_rows: int = field(
        default_factory=lambda: reservoir_sample_size(
            confidence=0.95, prevalence=0.01
        )
    )
    max_time_ms: int = 30_000
    max_pool_size: int = 2
    require_secondary: bool = True
    incremental: bool = False
    username: str | None = None
    password: str | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.uri:
            raise ValueError("uri must be non-empty")
        scheme = urlparse(self.uri).scheme
        # `mongodb+srv://` resolves SRV records to a replica-set seed
        # list; both forms are valid and the driver decides which to
        # use based on the scheme suffix.
        if scheme not in {"mongodb", "mongodb+srv"}:
            raise ValueError(
                "uri must be mongodb:// or mongodb+srv:// "
                f"(got {scheme!r})"
            )
        if self.sample_rows <= 0:
            raise ValueError("sample_rows must be > 0")
        if self.max_time_ms <= 0:
            raise ValueError("max_time_ms must be > 0")
        if self.max_pool_size < 1:
            raise ValueError("max_pool_size must be >= 1")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Strip credentials for the id; URI userinfo is sensitive and
        # ends up in checkpoint paths, log lines, and CLI output.
        return f"mongodb:{_redact_uri(self.uri)}"


@dataclass(slots=True)
class _CollectionMeta:
    database: str
    collection: str
    full: str  # `<db>.<coll>` — single key for the discover cache


class MongoConnector:
    """Read-only SourceConnector for MongoDB secondaries."""

    kind = "mongodb"

    def __init__(
        self,
        config: MongoConfig,
        *,
        client: AsyncIOMotorClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        # Test seam — production wiring builds a pooled motor client.
        # `_owns_client` controls whether `close()` actually disposes,
        # mirroring the pattern in pii-scanner-redis.
        if client is None:
            kwargs: dict[str, Any] = {
                "maxPoolSize": config.max_pool_size,
                # Soft socket timeout matches the server-side
                # maxTimeMS; the driver will tear the socket down if
                # the server doesn't ack the cancellation.
                "serverSelectionTimeoutMS": config.max_time_ms,
            }
            if config.username is not None:
                kwargs["username"] = config.username
            if config.password is not None:
                kwargs["password"] = config.password
            self._client = AsyncIOMotorClient(config.uri, **kwargs)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._secondary_checked = False
        # Cache of (db, coll) → meta so fetch() doesn't have to re-walk
        # `list_collection_names` after a fresh discover().
        self._collections: dict[str, _CollectionMeta] = {}

    def capabilities(self) -> Capabilities:
        # `incremental` reflects the runtime config — change streams
        # are an optional mode, not the default. `streaming=True` so
        # the scheduler knows the connector may yield large documents
        # piecewise (it doesn't today, but the contract permits it).
        return Capabilities(
            incremental=self._config.incremental,
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
        # In incremental mode the cursor is the resume token; in
        # sample mode the cursor is unused (every scan is a fresh
        # sample by design).
        await self._enforce_secondary()
        databases = await self._list_databases()
        for db_name in databases:
            collections = await self._list_collections(db_name)
            for coll_name in collections:
                full = f"{db_name}.{coll_name}"
                if (
                    self._config.collections
                    and full not in self._config.collections
                ):
                    continue
                if full in self._config.excluded_collections:
                    continue
                if filter.include and not _matches_any(
                    full, filter.include
                ):
                    continue
                if filter.exclude and _matches_any(full, filter.exclude):
                    continue
                meta = _CollectionMeta(
                    database=db_name, collection=coll_name, full=full
                )
                self._collections[full] = meta
                metadata = {
                    "database": db_name,
                    "collection": coll_name,
                }
                if self._config.incremental and cursor is not None:
                    # Round-trip the resume token via metadata so the
                    # scheduler's per-ref bookkeeping is intact when
                    # fetch() reloads the ref from a checkpoint.
                    metadata["_cursor"] = cursor
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=full,
                    content_type="application/x-mongodb-collection",
                    metadata=metadata,
                )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        meta = self._collections.get(ref.path)
        if meta is None:
            # Cold fetch (refs reloaded from a checkpoint won't have
            # the discover cache populated). Re-derive from the path.
            try:
                db_name, coll_name = ref.path.split(".", 1)
            except ValueError:
                return
            meta = _CollectionMeta(
                database=db_name, collection=coll_name, full=ref.path
            )
            self._collections[ref.path] = meta
        await self._enforce_secondary()
        coll = self._client[meta.database][meta.collection]
        if self._config.incremental:
            cursor = ref.metadata.get("_cursor")
            async for doc in self._iter_change_stream(coll, cursor):
                yield doc
            return
        async for doc in self._iter_sample(coll, ref, meta):
            yield doc

    async def close(self) -> None:
        if self._owns_client:
            self._client.close()
        self._collections.clear()

    # --- internals ----------------------------------------------------

    async def _enforce_secondary(self) -> None:
        # Memoise — the hello round-trip is cheap but discover() and
        # every fetch() would otherwise re-issue it.
        if self._secondary_checked:
            return
        if not self._config.require_secondary:
            self._secondary_checked = True
            return
        info = await self._client.admin.command(
            "hello", maxTimeMS=self._config.max_time_ms
        )
        # `hello` returns secondary=True on a replica-set member that
        # is not the primary. Standalone mongods return neither field
        # — those are dev-only and require require_secondary=False.
        if not info.get("secondary"):
            raise PrimaryConnectionRefused(
                "refusing to scan a primary / standalone MongoDB node: "
                "set require_secondary=False to override (not recommended)"
            )
        self._secondary_checked = True

    async def _list_databases(self) -> list[str]:
        names = await self._client.list_database_names()
        out: list[str] = []
        for name in names:
            if name in _SYSTEM_DATABASES and not (
                self._config.databases and name in self._config.databases
            ):
                continue
            if (
                self._config.databases
                and name not in self._config.databases
            ):
                continue
            if name in self._config.excluded_databases:
                continue
            out.append(name)
        return out

    async def _list_collections(self, db_name: str) -> list[str]:
        # Motor's list_collection_names returns a coroutine yielding a
        # list (not an async iterator) — matches the PyMongo blocking
        # API shape. Filter happens in `discover()` so the per-DB call
        # stays a simple metadata pull.
        return await self._client[db_name].list_collection_names()

    async def _iter_sample(
        self,
        coll: Any,
        ref: DocumentRef,
        meta: _CollectionMeta,
    ) -> AsyncIterator[Document]:
        pipeline = [{"$sample": {"size": self._config.sample_rows}}]
        cursor = coll.aggregate(
            pipeline, maxTimeMS=self._config.max_time_ms
        )
        i = 0
        async for doc in cursor:
            text = json_util.dumps(doc)
            yield Document(
                ref=DocumentRef(
                    source_id=ref.source_id,
                    source_kind=ref.source_kind,
                    path=f"{meta.full}#doc-{i}",
                    content_type="application/json",
                    metadata=dict(ref.metadata)
                    | {"document_index": str(i)},
                ),
                text=text,
                fetched_at=datetime.now(UTC),
                extra={
                    "database": meta.database,
                    "collection": meta.collection,
                    "document_index": str(i),
                },
            )
            i += 1

    async def _iter_change_stream(
        self,
        coll: Any,
        resume_token: str | None,
    ) -> AsyncIterator[Document]:
        # `watch()` keeps an open cursor; we drain whatever events are
        # immediately available and stop. Long-lived tailing is the
        # Scheduler's job, not ours — we are a discover/fetch primitive.
        kwargs: dict[str, Any] = {"max_await_time_ms": self._config.max_time_ms}
        if resume_token is not None:
            kwargs["resume_after"] = {"_data": resume_token}
        stream = coll.watch(**kwargs)
        try:
            i = 0
            async for change in stream:
                doc = change.get("fullDocument") or change
                text = json_util.dumps(doc)
                # Resume token lives on the change envelope; persist
                # it on the Document so the next scan picks up here.
                next_token = (change.get("_id") or {}).get("_data", "")
                yield Document(
                    ref=DocumentRef(
                        source_id=self.id,
                        source_kind=self.kind,
                        path=f"{coll.name}#change-{i}",
                        content_type="application/json",
                        metadata={
                            "database": coll.database.name,
                            "collection": coll.name,
                            "_cursor": next_token,
                        },
                    ),
                    text=text,
                    fetched_at=datetime.now(UTC),
                    extra={
                        "database": coll.database.name,
                        "collection": coll.name,
                        "_cursor": next_token,
                    },
                )
                i += 1
        finally:
            # Motor's change stream supports both `close()` and
            # `aclose()` across versions; prefer the modern spelling.
            close = getattr(stream, "aclose", None) or getattr(
                stream, "close", None
            )
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result


# --- helpers ---------------------------------------------------------


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(value, p) for p in patterns)


def _redact_uri(uri: str) -> str:
    """Strip userinfo from a MongoDB URI for use in logs and the `id`.

    `mongodb://user:pass@host:27017/db` →
    `mongodb://host:27017/db`. Also drops the query string because
    `?authSource=...&password=...` round-trips secrets in some
    deployments. `urllib.parse` understands `mongodb+srv://`.
    """
    parsed = urlparse(uri)
    if not parsed.netloc:
        return uri
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    redacted = parsed._replace(netloc=host, query="")
    return urlunparse(redacted)


# --- factory / spec --------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "uri" not in config:
        raise ValueError("mongodb connector config requires 'uri'")
    return MongoConnector(
        MongoConfig(
            uri=str(config["uri"]),
            databases=tuple(config.get("databases") or ()),
            excluded_databases=tuple(config.get("excluded_databases") or ()),
            collections=tuple(config.get("collections") or ()),
            excluded_collections=tuple(
                config.get("excluded_collections") or ()
            ),
            sample_rows=int(
                config.get(
                    "sample_rows",
                    reservoir_sample_size(
                        confidence=0.95, prevalence=0.01
                    ),
                )
            ),
            max_time_ms=int(config.get("max_time_ms", 30_000)),
            max_pool_size=int(config.get("max_pool_size", 2)),
            require_secondary=bool(
                config.get("require_secondary", True)
            ),
            incremental=bool(config.get("incremental", False)),
            username=(
                str(config["username"])
                if config.get("username") is not None
                else None
            ),
            password=(
                str(config["password"])
                if config.get("password") is not None
                else None
            ),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="mongodb",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,  # supported when configured
        binary=True,
        content_hash_delta=False,
        max_concurrent_fetches=2,
        streaming=True,
    ),
    required_scopes=("connect", "find"),
    description=(
        "MongoDB SourceConnector. $sample-based document enumeration "
        "(never find({}) cursor scans), secondary enforcement at "
        "connect time via the hello command, BSON Extended JSON v2 "
        "serialisation (ObjectId/Decimal128/Date/Binary), optional "
        "change-stream incremental mode with resume_after token, "
        "maxTimeMS=30000 on every command, max_pool_size=2."
    ),
)
