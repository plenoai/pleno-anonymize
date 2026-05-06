"""Tests for RedisConnector — uses an in-memory fake redis client.

The fake implements only the methods the connector calls. It is
deliberately small — testing against `fakeredis` would pull a real
Redis-compatible engine into the test environment, which we avoid
to keep the test suite hermetic and fast (≤1 sec).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner_redis import (
    AclEnforcementError,
    RedisConfig,
    RedisConnector,
    SPEC,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


# --- in-memory fake -------------------------------------------------


class _FakeRedis:
    def __init__(
        self,
        *,
        keys: dict[bytes, tuple[str, Any]] | None = None,
        whoami: str = "scanner",
        acl_user: dict[str, Any] | list[Any] | None = None,
    ) -> None:
        # Map of key -> (type, value). value is the type-native shape
        # the corresponding Redis command would return.
        self._keys: dict[bytes, tuple[str, Any]] = keys or {}
        self._whoami = whoami
        self._acl_user = acl_user or {
            "commands": "+@read +@connection -@write -@admin -@dangerous"
        }
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True

    async def execute_command(self, *args: Any) -> Any:
        if args[:1] == ("ACL",) and len(args) >= 2:
            sub = args[1]
            if sub == "WHOAMI":
                return self._whoami.encode()
            if sub == "GETUSER":
                return self._acl_user
        raise AssertionError(f"unexpected command: {args}")

    def scan_iter(self, *, match: str = "*", count: int = 100) -> AsyncIterator[bytes]:
        import fnmatch

        keys = [k for k in self._keys.keys() if fnmatch.fnmatch(k.decode(), match)]

        async def _gen() -> AsyncIterator[bytes]:
            for k in keys:
                yield k

        return _gen()

    @staticmethod
    def _norm(key: Any) -> bytes:
        # redis-py accepts both str and bytes; mirror that.
        return key if isinstance(key, bytes) else str(key).encode()

    async def type(self, key: Any) -> bytes:  # noqa: A003 - matches redis-py
        entry = self._keys.get(self._norm(key))
        return (entry[0] if entry else "none").encode()

    async def get(self, key: Any) -> bytes | None:
        entry = self._keys.get(self._norm(key))
        return entry[1] if entry and entry[0] == "string" else None

    async def lrange(self, key: Any, start: int, stop: int) -> list[bytes]:
        del start, stop
        entry = self._keys.get(self._norm(key))
        return list(entry[1]) if entry and entry[0] == "list" else []

    async def smembers(self, key: Any) -> set[bytes]:
        entry = self._keys.get(self._norm(key))
        return set(entry[1]) if entry and entry[0] == "set" else set()

    async def hgetall(self, key: Any) -> dict[bytes, bytes]:
        entry = self._keys.get(self._norm(key))
        return dict(entry[1]) if entry and entry[0] == "hash" else {}

    async def zrange(
        self, key: Any, start: int, stop: int, withscores: bool = False
    ) -> list[tuple[bytes, float]]:
        del start, stop, withscores
        entry = self._keys.get(self._norm(key))
        return list(entry[1]) if entry and entry[0] == "zset" else []

    async def xrange(
        self, key: Any, count: int = 100
    ) -> list[tuple[bytes, dict[bytes, bytes]]]:
        del count
        entry = self._keys.get(self._norm(key))
        return list(entry[1]) if entry and entry[0] == "stream" else []


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_url(self) -> None:
        with pytest.raises(ValueError, match="url must be non-empty"):
            RedisConfig(url="")

    def test_rejects_unsupported_scheme(self) -> None:
        with pytest.raises(ValueError, match="redis://"):
            RedisConfig(url="http://localhost:6379")

    def test_accepts_rediss(self) -> None:
        cfg = RedisConfig(url="rediss://localhost:6379")
        assert cfg.url.startswith("rediss://")

    def test_accepts_unix(self) -> None:
        cfg = RedisConfig(url="unix:///tmp/redis.sock")
        assert cfg.url.startswith("unix://")

    def test_rejects_bad_count_hint(self) -> None:
        with pytest.raises(ValueError, match="count_hint"):
            RedisConfig(url="redis://h", count_hint=0)

    def test_rejects_bad_max_value_bytes(self) -> None:
        with pytest.raises(ValueError, match="max_value_bytes"):
            RedisConfig(url="redis://h", max_value_bytes=0)

    def test_rejects_bad_pool_size(self) -> None:
        with pytest.raises(ValueError, match="pool_size"):
            RedisConfig(url="redis://h", pool_size=0)

    def test_rejects_bad_stream_max_entries(self) -> None:
        with pytest.raises(ValueError, match="stream_max_entries"):
            RedisConfig(url="redis://h", stream_max_entries=0)

    def test_explicit_id(self) -> None:
        cfg = RedisConfig(url="redis://h", id="my-id")
        assert cfg.resolved_id() == "my-id"

    def test_default_id_strips_credentials(self) -> None:
        cfg = RedisConfig(url="rediss://user:pass@redis.example:6380/3")
        assert cfg.resolved_id() == "redis:redis.example:6380/3"
        assert "pass" not in cfg.resolved_id()

    def test_default_id_falls_back_to_localhost(self) -> None:
        cfg = RedisConfig(url="redis://")
        assert cfg.resolved_id() == "redis:localhost:6379/0"


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = RedisConnector(RedisConfig(url="redis://h"), client=_FakeRedis())
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = RedisConnector(RedisConfig(url="redis://h"), client=_FakeRedis())
        assert c.capabilities() == Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=True,
        )


# --- ACL enforcement ----------------------------------------------


class TestAclEnforcement:
    async def test_readonly_user_passes(self) -> None:
        client = _FakeRedis(acl_user={"commands": "+@read +@connection -@write"})
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            await c._enforce_acl()
        finally:
            await c.close()

    async def test_user_with_write_rejected(self) -> None:
        client = _FakeRedis(acl_user={"commands": "+@read +@write +@connection"})
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            with pytest.raises(AclEnforcementError, match="@write"):
                await c._enforce_acl()
        finally:
            await c.close()

    async def test_user_with_admin_rejected(self) -> None:
        client = _FakeRedis(acl_user={"commands": "+@read +@admin"})
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            with pytest.raises(AclEnforcementError, match="@admin"):
                await c._enforce_acl()
        finally:
            await c.close()

    async def test_resp2_list_shape_supported(self) -> None:
        # Pre-Redis 7.2 returns ACL GETUSER as a flat alternating list.
        client = _FakeRedis(
            acl_user=[
                b"flags",
                [b"on"],
                b"commands",
                b"+@read +@connection -@write",
            ]
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            await c._enforce_acl()
        finally:
            await c.close()

    async def test_disabled_enforcement_skips_check(self) -> None:
        # Even an over-privileged user passes when enforcement is off.
        client = _FakeRedis(acl_user={"commands": "+@all"})
        c = RedisConnector(
            RedisConfig(url="redis://h", enforce_readonly=False),
            client=client,
        )
        try:
            await c._enforce_acl()
        finally:
            await c.close()

    async def test_acl_check_memoized(self) -> None:
        # Second discover/fetch call must not re-invoke ACL.
        calls: list[Any] = []

        class _Counting(_FakeRedis):
            async def execute_command(self, *args: Any) -> Any:
                calls.append(args)
                return await super().execute_command(*args)

        client = _Counting()
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            await c._enforce_acl()
            await c._enforce_acl()
        finally:
            await c.close()
        # Initial check = WHOAMI + GETUSER. Second is a no-op.
        assert len([a for a in calls if a[0] == "ACL"]) == 2

    async def test_unknown_acl_shape_treated_as_empty(self) -> None:
        # Defensive path — neither dict nor list. Connector should
        # not crash but also not granting privileges.
        client = _FakeRedis(acl_user="some-string")
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            # No forbidden category present in empty dict → passes.
            await c._enforce_acl()
        finally:
            await c.close()


# --- discover ------------------------------------------------------


class TestDiscover:
    async def test_yields_one_ref_per_key(self) -> None:
        client = _FakeRedis(
            keys={
                b"user:1": ("string", b"alice@example.com"),
                b"user:2": ("string", b"bob@example.com"),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            paths = sorted(r.path for r in refs)
            assert paths == ["user:1", "user:2"]
            assert all(r.metadata["type"] == "string" for r in refs)
        finally:
            await c.close()

    async def test_match_glob_filters_keys(self) -> None:
        client = _FakeRedis(
            keys={
                b"user:1": ("string", b"x"),
                b"session:1": ("string", b"y"),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h", match="user:*"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"user:1"}
        finally:
            await c.close()

    async def test_source_filter_include_applied(self) -> None:
        client = _FakeRedis(
            keys={
                b"user:1": ("string", b"x"),
                b"session:1": ("string", b"y"),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [
                r async for r in c.discover(SourceFilter(include=("user:*",)), None)
            ]
            assert {r.path for r in refs} == {"user:1"}
        finally:
            await c.close()

    async def test_source_filter_exclude_applied(self) -> None:
        client = _FakeRedis(
            keys={
                b"user:1": ("string", b"x"),
                b"session:1": ("string", b"y"),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [
                r async for r in c.discover(SourceFilter(exclude=("session:*",)), None)
            ]
            assert {r.path for r in refs} == {"user:1"}
        finally:
            await c.close()

    async def test_expired_key_skipped(self) -> None:
        # Key was in SCAN result but TYPE returned "none" — race.
        class _Racing(_FakeRedis):
            async def type(self, key: bytes) -> bytes:
                return b"none"

        client = _Racing(keys={b"k": ("string", b"v")})
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert refs == []
        finally:
            await c.close()


# --- fetch — every value type --------------------------------------


class TestFetchString:
    async def test_returns_text(self) -> None:
        client = _FakeRedis(keys={b"k": ("string", b"hello@example.com")})
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            assert docs[0].text == "hello@example.com"
        finally:
            await c.close()


class TestFetchList:
    async def test_joins_entries_newline(self) -> None:
        client = _FakeRedis(
            keys={
                b"q": ("list", [b"first", b"second", b"third"]),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert docs[0].text == "first\nsecond\nthird"
        finally:
            await c.close()


class TestFetchSet:
    async def test_sorted_join(self) -> None:
        client = _FakeRedis(
            keys={
                b"s": ("set", {b"banana", b"apple", b"cherry"}),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            # sorted byte-order
            assert docs[0].text == "apple\nbanana\ncherry"
        finally:
            await c.close()


class TestFetchHash:
    async def test_field_value_lines(self) -> None:
        client = _FakeRedis(
            keys={
                b"h": ("hash", {b"email": b"x@y.z", b"name": b"alice"}),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert "email=x@y.z" in docs[0].text
            assert "name=alice" in docs[0].text
        finally:
            await c.close()


class TestFetchZset:
    async def test_member_score(self) -> None:
        client = _FakeRedis(
            keys={
                b"z": ("zset", [(b"alice", 1.0), (b"bob", 2.5)]),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert "alice=1.0" in docs[0].text
            assert "bob=2.5" in docs[0].text
        finally:
            await c.close()


class TestFetchStream:
    async def test_id_and_fields(self) -> None:
        client = _FakeRedis(
            keys={
                b"x": (
                    "stream",
                    [(b"1700000000000-0", {b"event": b"login", b"user": b"alice"})],
                ),
            }
        )
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert "1700000000000-0" in docs[0].text
            assert "event=login" in docs[0].text
            assert "user=alice" in docs[0].text
        finally:
            await c.close()


class TestFetchEdge:
    async def test_unknown_type_returns_no_documents(self) -> None:
        client = _FakeRedis(keys={b"k": ("hyperloglog", b"\x00")})
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            # discover still yields the ref; fetch is the type filter.
            assert len(refs) == 1
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_oversize_value_skipped(self) -> None:
        big = b"x" * 2048
        client = _FakeRedis(keys={b"k": ("string", big)})
        c = RedisConnector(
            RedisConfig(url="redis://h", max_value_bytes=1024),
            client=client,
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_without_metadata_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        client = _FakeRedis()
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_with_missing_string_returns_empty(self) -> None:
        # SCAN said key existed, fetch finds nothing (race).
        from pleno_pii_scanner.sources.base import DocumentRef

        client = _FakeRedis()  # empty store
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="ghost",
                metadata={"key": "ghost", "type": "string"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()


# --- spec / factory -------------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "redis"
        assert SPEC.version == "0.1.0"

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("redis", {"url": "redis://h"})
        assert isinstance(c, RedisConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "redis",
            {
                "url": "rediss://localhost:6379/0",
                "match": "user:*",
                "count_hint": 500,
                "max_value_bytes": 8192,
                "stream_max_entries": 50,
                "pool_size": 4,
                "enforce_readonly": False,
                "username": "scanner",
                "password": "secret",
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_url(self) -> None:
        with pytest.raises(ValueError, match="url"):
            SPEC.factory({})


# --- close ----------------------------------------------------------


class TestHelpers:
    def test_b_bytes_passthrough(self) -> None:
        from pleno_pii_scanner_redis.connector import _b

        assert _b(b"hello") == b"hello"

    def test_b_non_bytes_coerced(self) -> None:
        from pleno_pii_scanner_redis.connector import _b

        assert _b(42) == b"42"


class TestClose:
    async def test_close_owns_client(self) -> None:
        # Production path: client created internally → close disposes.
        c = RedisConnector(RedisConfig(url="redis://h"))
        assert c._owns_client
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = _FakeRedis()
        c = RedisConnector(RedisConfig(url="redis://h"), client=client)
        await c.close()
        assert not client.closed
