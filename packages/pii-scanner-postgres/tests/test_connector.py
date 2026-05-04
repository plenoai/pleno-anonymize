"""Tests for PostgresConnector — uses an in-process asyncpg double.

We never connect to a real PostgreSQL. Instead `_FakePool` /
`_FakeConn` mimic the asyncpg shapes we touch (`acquire`, `fetch`,
`execute`, `fetchval`, `close`) and let each test inject the rows the
discover / fetch path will see. This keeps the suite hermetic and
sub-second.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner_postgres import (
    PostgresConfig,
    PostgresConnector,
    SPEC,
)
from pleno_pii_scanner_postgres.connector import (
    PrimaryConnectionRefused,
    _redact_dsn,
    _serialise_row,
    _stringify,
)


# --- asyncpg double -------------------------------------------------


class _FakeRecord(dict):
    """asyncpg.Record duck-type — supports both `r[i]` and `r['col']`."""


class _FakeConn:
    def __init__(self, fetch_responses: list[Any], in_recovery: bool) -> None:
        self._fetch_responses = list(fetch_responses)
        self._in_recovery = in_recovery
        self.executed: list[str] = []

    async def execute(self, sql: str, *_args: Any) -> None:
        self.executed.append(sql)

    async def fetchval(self, sql: str, *_args: Any) -> Any:
        if "pg_is_in_recovery" in sql:
            return self._in_recovery
        return None

    async def fetch(self, sql: str, *_args: Any) -> list[_FakeRecord]:
        if not self._fetch_responses:
            raise AssertionError(f"unexpected fetch: {sql!r}")
        return self._fetch_responses.pop(0)


class _FakePool:
    def __init__(
        self, fetch_responses: list[Any], in_recovery: bool = True
    ) -> None:
        self._conn = _FakeConn(fetch_responses, in_recovery)
        self.closed = False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_FakeConn]:
        yield self._conn

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolated_registry():
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


@pytest.fixture
def patch_pool(monkeypatch: pytest.MonkeyPatch):
    """Returns a function that installs a `_FakePool` for create_pool."""

    def _install(fetch_responses: list[Any], *, in_recovery: bool = True):
        pool = _FakePool(fetch_responses, in_recovery=in_recovery)

        async def _create_pool(*_a: Any, **_kw: Any) -> _FakePool:
            return pool

        from pleno_pii_scanner_postgres import connector as mod

        monkeypatch.setattr(mod.asyncpg, "create_pool", _create_pool)
        return pool

    return _install


# --- config ---------------------------------------------------------


class TestConfig:
    def test_rejects_empty_dsn(self) -> None:
        with pytest.raises(ValueError, match="dsn"):
            PostgresConfig(dsn="")

    def test_rejects_empty_schemas(self) -> None:
        with pytest.raises(ValueError, match="schemas"):
            PostgresConfig(dsn="postgres://x", schemas=())

    def test_rejects_zero_sample_rows(self) -> None:
        with pytest.raises(ValueError, match="sample_rows"):
            PostgresConfig(dsn="postgres://x", sample_rows=0)

    def test_rejects_zero_pool_size(self) -> None:
        with pytest.raises(ValueError, match="pool_size"):
            PostgresConfig(dsn="postgres://x", pool_size=0)

    def test_id_redacts_password(self) -> None:
        cfg = PostgresConfig(dsn="postgresql://user:secret@host:5432/db")
        assert "secret" not in cfg.resolved_id()
        assert "user@host" in cfg.resolved_id()

    def test_id_explicit(self) -> None:
        cfg = PostgresConfig(dsn="postgres://x", id="custom-id")
        assert cfg.resolved_id() == "custom-id"

    def test_default_sample_rows_matches_adr(self) -> None:
        cfg = PostgresConfig(dsn="postgres://x")
        assert cfg.sample_rows == 299


# --- redact / serialise helpers --------------------------------------


class TestHelpers:
    def test_redact_dsn_strips_password(self) -> None:
        assert (
            _redact_dsn("postgresql://u:p@h/d") == "postgresql://u@h/d"
        )

    def test_redact_dsn_no_auth(self) -> None:
        assert _redact_dsn("postgresql://h/d") == "postgresql://h/d"

    def test_redact_dsn_user_only(self) -> None:
        assert (
            _redact_dsn("postgresql://u@h/d") == "postgresql://u@h/d"
        )

    def test_redact_dsn_keyvalue_form_passthrough(self) -> None:
        # libpq key=value: too many corner cases to safely strip, so
        # we leave it alone and document that operators should redact
        # before passing to logs themselves.
        assert _redact_dsn("host=h user=u password=p") == "host=h user=u password=p"

    def test_stringify_none(self) -> None:
        assert _stringify(None) == ""

    def test_stringify_bytes_decodes_utf8_replace(self) -> None:
        assert _stringify(b"hello") == "hello"
        # invalid utf-8 → no exception
        out = _stringify(b"\xff\xfe")
        assert isinstance(out, str)

    def test_stringify_other(self) -> None:
        assert _stringify(42) == "42"
        assert _stringify(3.14) == "3.14"

    def test_serialise_row_format(self) -> None:
        row = _FakeRecord({"name": "Alice", "email": "a@x"})
        out = _serialise_row(["name", "email"], row)
        assert out == "name=Alice\nemail=a@x"


# --- discover/fetch end-to-end --------------------------------------


class TestDiscover:
    async def test_yields_one_ref_per_table(self, patch_pool) -> None:
        # information_schema.columns response: 2 tables × 2 columns each
        rows = [
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "name",
                    "data_type": "text",
                    "estimated_rows": 5000,
                }
            ),
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "email",
                    "data_type": "text",
                    "estimated_rows": 5000,
                }
            ),
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "events",
                    "column_name": "payload",
                    "data_type": "jsonb",
                    "estimated_rows": 200_000,
                }
            ),
        ]
        patch_pool([rows])  # discover() makes one fetch
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"public.users", "public.events"}
            users = next(r for r in refs if r.path == "public.users")
            assert users.metadata["columns"] == "name,email"
            assert users.metadata["estimated_rows"] == "5000"
        finally:
            await c.close()

    async def test_table_filter_includes(self, patch_pool) -> None:
        rows = [
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "name",
                    "data_type": "text",
                    "estimated_rows": 100,
                }
            ),
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "logs",
                    "column_name": "msg",
                    "data_type": "text",
                    "estimated_rows": 100,
                }
            ),
        ]
        patch_pool([rows])
        c = PostgresConnector(
            PostgresConfig(
                dsn="postgresql://u@h/d", tables=("public.users",)
            )
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"public.users"}
        finally:
            await c.close()

    async def test_table_filter_excludes(self, patch_pool) -> None:
        rows = [
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "name",
                    "data_type": "text",
                    "estimated_rows": 100,
                }
            ),
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "logs",
                    "column_name": "msg",
                    "data_type": "text",
                    "estimated_rows": 100,
                }
            ),
        ]
        patch_pool([rows])
        c = PostgresConnector(
            PostgresConfig(
                dsn="postgresql://u@h/d",
                excluded_tables=("public.logs",),
            )
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"public.users"}
        finally:
            await c.close()

    async def test_source_filter_include_glob(self, patch_pool) -> None:
        rows = [
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "n",
                    "data_type": "text",
                    "estimated_rows": 1,
                }
            ),
            _FakeRecord(
                {
                    "table_schema": "billing",
                    "table_name": "invoices",
                    "column_name": "n",
                    "data_type": "text",
                    "estimated_rows": 1,
                }
            ),
        ]
        patch_pool([rows])
        c = PostgresConnector(
            PostgresConfig(
                dsn="postgresql://u@h/d",
                schemas=("public", "billing"),
            )
        )
        try:
            refs = [
                r
                async for r in c.discover(
                    SourceFilter(include=("billing.*",)), None
                )
            ]
            assert {r.path for r in refs} == {"billing.invoices"}
        finally:
            await c.close()

    async def test_source_filter_exclude_glob(self, patch_pool) -> None:
        rows = [
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "n",
                    "data_type": "text",
                    "estimated_rows": 1,
                }
            ),
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "logs",
                    "column_name": "n",
                    "data_type": "text",
                    "estimated_rows": 1,
                }
            ),
        ]
        patch_pool([rows])
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        try:
            refs = [
                r
                async for r in c.discover(
                    SourceFilter(exclude=("*.logs",)), None
                )
            ]
            assert {r.path for r in refs} == {"public.users"}
        finally:
            await c.close()


# --- replica enforcement --------------------------------------------


class TestReplicaEnforcement:
    async def test_refuses_primary(self, patch_pool) -> None:
        patch_pool([], in_recovery=False)
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        try:
            with pytest.raises(PrimaryConnectionRefused):
                async for _ in c.discover(SourceFilter(), None):
                    pass
        finally:
            await c.close()

    async def test_override_via_require_replica_false(self, patch_pool) -> None:
        rows = [
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "n",
                    "data_type": "text",
                    "estimated_rows": 1,
                }
            ),
        ]
        patch_pool([rows], in_recovery=False)
        c = PostgresConnector(
            PostgresConfig(
                dsn="postgresql://u@h/d", require_replica=False
            )
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert refs
        finally:
            await c.close()


# --- fetch ----------------------------------------------------------


class TestFetch:
    async def test_fetch_yields_per_row_documents(self, patch_pool) -> None:
        discover_rows = [
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "name",
                    "data_type": "text",
                    "estimated_rows": 50,
                }
            ),
            _FakeRecord(
                {
                    "table_schema": "public",
                    "table_name": "users",
                    "column_name": "email",
                    "data_type": "text",
                    "estimated_rows": 50,
                }
            ),
        ]
        sample_rows = [
            _FakeRecord({"name": "Alice", "email": "a@x"}),
            _FakeRecord({"name": "Bob", "email": "b@x"}),
        ]
        patch_pool([discover_rows, sample_rows])
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 1
            docs: list[Document] = []
            async for d in c.fetch(refs[0]):
                assert isinstance(d, Document)
                docs.append(d)
            assert len(docs) == 2
            assert "Alice" in docs[0].text
            assert "b@x" in docs[1].text
            assert docs[0].extra["row_index"] == "0"
        finally:
            await c.close()

    async def test_fetch_unknown_table_returns_empty(self, patch_pool) -> None:
        # Reload path: discover never ran, fetch arrives with a ref
        # whose path doesn't have a "." separator.
        patch_pool([])
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="no_dot_here",
                content_type="application/x-postgres-rows",
            )
            collected = [d async for d in c.fetch(ref)]
            assert collected == []
        finally:
            await c.close()

    async def test_fetch_reload_table_meta(self, patch_pool) -> None:
        # discover() never invoked → fetch must re-query
        # information_schema for the table's columns.
        reload_rows = [
            _FakeRecord({"column_name": "name", "estimated_rows": 100}),
        ]
        sample_rows = [_FakeRecord({"name": "Alice"})]
        patch_pool([reload_rows, sample_rows])
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="public.users",
                content_type="application/x-postgres-rows",
            )
            docs: list[Document] = []
            async for d in c.fetch(ref):
                assert isinstance(d, Document)
                docs.append(d)
            assert len(docs) == 1
        finally:
            await c.close()

    async def test_fetch_reload_returns_no_rows(self, patch_pool) -> None:
        # Reload path where information_schema has no matching column.
        patch_pool([[]])  # empty reload result
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="missing.table",
                content_type="application/x-postgres-rows",
            )
            collected = [d async for d in c.fetch(ref)]
            assert collected == []
        finally:
            await c.close()


# --- protocol + spec ------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=True,
        )


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "postgres"
        assert SPEC.version == "0.1.0"
        assert "select" in SPEC.required_scopes

    def test_factory_via_registry(self) -> None:
        register(SPEC)
        c = create("postgres", {"dsn": "postgresql://u@h/d"})
        assert isinstance(c, PostgresConnector)

    def test_factory_full_config(self) -> None:
        register(SPEC)
        c = create(
            "postgres",
            {
                "dsn": "postgresql://u@h/d",
                "schemas": ["public", "billing"],
                "tables": ["public.users"],
                "excluded_tables": ["public.logs"],
                "sample_rows": 500,
                "statement_timeout": "60s",
                "pool_size": 4,
                "require_replica": False,
                "id": "test-id",
            },
        )
        assert c.id == "test-id"

    def test_factory_rejects_missing_dsn(self) -> None:
        with pytest.raises(ValueError, match="dsn"):
            SPEC.factory({})

    def test_factory_empty_schemas_uses_default(self) -> None:
        register(SPEC)
        c = create(
            "postgres", {"dsn": "postgresql://u@h/d", "schemas": []}
        )
        assert c._config.schemas == ("public",)


class TestClose:
    async def test_close_drops_pool(self, patch_pool) -> None:
        pool = patch_pool([])
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        await c._ensure_pool()
        await c.close()
        assert pool.closed

    async def test_close_idempotent_without_pool(self) -> None:
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        await c.close()  # never opened — must not crash

    async def test_ensure_pool_idempotent(self, patch_pool) -> None:
        # Two ensure calls must reuse the same pool, not create a second.
        pool = patch_pool([])
        c = PostgresConnector(PostgresConfig(dsn="postgresql://u@h/d"))
        await c._ensure_pool()
        await c._ensure_pool()
        assert c._pool is pool
        await c.close()
