"""Tests for MysqlConnector — uses an in-memory fake aiomysql Pool.

The fake implements only the methods the connector calls: pool
acquire/release, cursor execute/fetchall, and the canned responses
the connector relies on (SHOW VARIABLES, SHOW REPLICA STATUS,
information_schema queries, sample queries).
"""

from __future__ import annotations

import contextlib
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
from pleno_pii_scanner_mysql import (
    MysqlConfig,
    MysqlConnector,
    PrimaryConnectionRefused,
    SPEC,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


# --- in-memory fake -------------------------------------------------


class _FakeProgrammingError(Exception):
    """Stand-in for aiomysql.ProgrammingError inside fakes."""


class _FakeCursor:
    def __init__(self, fake_pool: "_FakePool") -> None:
        self._pool = fake_pool
        self._rows: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._pool.executed.append((sql, params))
        self._rows = self._pool.respond(sql, params)

    async def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, fake_pool: "_FakePool") -> None:
        self._pool = fake_pool

    def cursor(self, _kind: Any = None) -> _FakeCursor:
        return _FakeCursor(self._pool)


class _FakePool:
    def __init__(
        self,
        *,
        responses: dict[str, list[dict[str, Any]]] | None = None,
        responder: Any = None,
        replica_status: list[dict[str, Any]] | None = None,
        read_only_value: str = "ON",
        replica_command_supported: bool = True,
    ) -> None:
        self._responses = responses or {}
        self._responder = responder
        self.executed: list[tuple[str, Any]] = []
        self.closed = False
        self.waited_close = False
        self._replica_status = (
            replica_status
            if replica_status is not None
            else [{"Source_Host": "primary"}]
        )
        self._read_only_value = read_only_value
        self._replica_command_supported = replica_command_supported

    def acquire(self) -> contextlib.AbstractAsyncContextManager[_FakeConn]:
        @contextlib.asynccontextmanager
        async def _ctx() -> AsyncIterator[_FakeConn]:
            yield _FakeConn(self)

        return _ctx()

    def respond(self, sql: str, params: Any) -> list[dict[str, Any]]:
        if self._responder is not None:
            return self._responder(sql, params)
        # canned routing for replica check
        upper = sql.strip().upper()
        if "SHOW VARIABLES LIKE 'READ_ONLY'" in upper:
            return [{"Variable_name": "read_only", "Value": self._read_only_value}]
        if "SHOW REPLICA STATUS" in upper:
            if not self._replica_command_supported:
                raise _FakeProgrammingError("SHOW REPLICA STATUS not supported")
            return self._replica_status
        if "SHOW SLAVE STATUS" in upper:
            return self._replica_status
        # generic match against responses dict keyed by case-insensitive substring
        for key, rows in self._responses.items():
            if key.upper() in upper:
                return rows
        return []

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited_close = True


@pytest.fixture(autouse=True)
def _patch_aiomysql_programmingerror(monkeypatch: pytest.MonkeyPatch):
    # Connector catches aiomysql.ProgrammingError; route to our fake.
    import aiomysql

    monkeypatch.setattr(aiomysql, "ProgrammingError", _FakeProgrammingError)
    yield
    # autouse cleanup happens automatically


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_dsn(self) -> None:
        with pytest.raises(ValueError, match="dsn"):
            MysqlConfig(dsn="", schemas=("app",))

    def test_rejects_unsupported_scheme(self) -> None:
        with pytest.raises(ValueError, match="mysql://"):
            MysqlConfig(dsn="postgres://h/db", schemas=("a",))

    def test_accepts_aiomysql_form(self) -> None:
        cfg = MysqlConfig(dsn="mysql+aiomysql://u@h/db")
        assert cfg.dsn.startswith("mysql+aiomysql://")

    def test_rejects_no_schemas_no_db_in_dsn(self) -> None:
        with pytest.raises(ValueError, match="schemas"):
            MysqlConfig(dsn="mysql://u:p@h:3306/")

    def test_dsn_db_provides_default_schema(self) -> None:
        cfg = MysqlConfig(dsn="mysql://u:p@h:3306/app")
        assert cfg.resolved_schemas() == ("app",)

    def test_explicit_schemas_override_dsn_db(self) -> None:
        cfg = MysqlConfig(dsn="mysql://u:p@h:3306/app", schemas=("billing", "app"))
        assert cfg.resolved_schemas() == ("billing", "app")

    def test_rejects_bad_sample_rows(self) -> None:
        with pytest.raises(ValueError, match="sample_rows"):
            MysqlConfig(dsn="mysql://u@h/db", sample_rows=0)

    def test_rejects_bad_pool_size(self) -> None:
        with pytest.raises(ValueError, match="pool_size"):
            MysqlConfig(dsn="mysql://u@h/db", pool_size=0)

    def test_rejects_bad_timeout(self) -> None:
        with pytest.raises(ValueError, match="statement_timeout_ms"):
            MysqlConfig(dsn="mysql://u@h/db", statement_timeout_ms=0)

    def test_explicit_id(self) -> None:
        cfg = MysqlConfig(dsn="mysql://u@h/db", id="my-id")
        assert cfg.resolved_id() == "my-id"

    def test_default_id_strips_password(self) -> None:
        cfg = MysqlConfig(dsn="mysql://user:secret@host:3306/db")
        rid = cfg.resolved_id()
        assert "secret" not in rid
        assert "user" in rid
        assert "host:3306" in rid


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = MysqlConnector(MysqlConfig(dsn="mysql://u@h/db"), pool=_FakePool())
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = MysqlConnector(MysqlConfig(dsn="mysql://u@h/db"), pool=_FakePool())
        assert c.capabilities() == Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=True,
        )


# --- replica enforcement ------------------------------------------


class TestReplicaEnforcement:
    async def test_replica_passes(self) -> None:
        c = MysqlConnector(MysqlConfig(dsn="mysql://u@h/db"), pool=_FakePool())
        try:
            await c._enforce_replica()
        finally:
            await c.close()

    async def test_primary_rejected(self) -> None:
        # read_only=OFF → primary
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db"),
            pool=_FakePool(read_only_value="OFF"),
        )
        try:
            with pytest.raises(PrimaryConnectionRefused, match="read_only"):
                await c._enforce_replica()
        finally:
            await c.close()

    async def test_read_only_but_not_replicating_rejected(self) -> None:
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db"),
            pool=_FakePool(replica_status=[]),
        )
        try:
            with pytest.raises(PrimaryConnectionRefused, match="replication"):
                await c._enforce_replica()
        finally:
            await c.close()

    async def test_falls_back_to_show_slave_status_on_old_mysql(self) -> None:
        # SHOW REPLICA STATUS not supported → falls through to SHOW SLAVE STATUS
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db"),
            pool=_FakePool(replica_command_supported=False),
        )
        try:
            await c._enforce_replica()
        finally:
            await c.close()

    async def test_disabled_enforcement_skips(self) -> None:
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", require_replica=False),
            pool=_FakePool(read_only_value="OFF"),
        )
        try:
            await c._enforce_replica()
        finally:
            await c.close()

    async def test_no_replica_status_command_succeeds_propagates(self) -> None:
        # Both SHOW REPLICA + SHOW SLAVE raise → connector re-raises
        def both_fail(sql: str, _p: Any) -> list[dict[str, Any]]:
            upper = sql.upper()
            if "SHOW VARIABLES LIKE 'READ_ONLY'" in upper:
                return [{"Variable_name": "read_only", "Value": "ON"}]
            if "SHOW REPLICA STATUS" in upper or "SHOW SLAVE STATUS" in upper:
                raise _FakeProgrammingError("denied")
            return []

        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db"),
            pool=_FakePool(responder=both_fail),
        )
        try:
            with pytest.raises(_FakeProgrammingError):
                await c._enforce_replica()
        finally:
            await c.close()


# --- discover ------------------------------------------------------


class TestDiscover:
    async def test_yields_one_ref_per_table(self) -> None:
        rows = [
            {
                "table_schema": "app",
                "table_name": "users",
                "column_name": "email",
                "data_type": "varchar",
                "estimated_rows": 1000,
            },
            {
                "table_schema": "app",
                "table_name": "users",
                "column_name": "phone",
                "data_type": "varchar",
                "estimated_rows": 1000,
            },
            {
                "table_schema": "app",
                "table_name": "events",
                "column_name": "payload",
                "data_type": "json",
                "estimated_rows": 50_000,
            },
        ]
        pool = _FakePool(responses={"information_schema.columns": rows})
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)), pool=pool
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            paths = {r.path for r in refs}
            assert paths == {"app.users", "app.events"}
            users = next(r for r in refs if r.path == "app.users")
            assert users.metadata["columns"] == "email,phone"
        finally:
            await c.close()

    async def test_table_allowlist_filters(self) -> None:
        rows = [
            {
                "table_schema": "app",
                "table_name": "users",
                "column_name": "email",
                "data_type": "varchar",
                "estimated_rows": 1,
            },
            {
                "table_schema": "app",
                "table_name": "events",
                "column_name": "payload",
                "data_type": "json",
                "estimated_rows": 1,
            },
        ]
        pool = _FakePool(responses={"information_schema.columns": rows})
        c = MysqlConnector(
            MysqlConfig(
                dsn="mysql://u@h/db",
                schemas=("app",),
                tables=("app.users",),
            ),
            pool=pool,
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_excluded_tables_filter(self) -> None:
        rows = [
            {
                "table_schema": "app",
                "table_name": "users",
                "column_name": "email",
                "data_type": "varchar",
                "estimated_rows": 1,
            },
            {
                "table_schema": "app",
                "table_name": "events",
                "column_name": "payload",
                "data_type": "json",
                "estimated_rows": 1,
            },
        ]
        pool = _FakePool(responses={"information_schema.columns": rows})
        c = MysqlConnector(
            MysqlConfig(
                dsn="mysql://u@h/db",
                schemas=("app",),
                excluded_tables=("app.events",),
            ),
            pool=pool,
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()

    async def test_source_filter_include_exclude(self) -> None:
        rows = [
            {
                "table_schema": "app",
                "table_name": "users",
                "column_name": "email",
                "data_type": "varchar",
                "estimated_rows": 1,
            },
            {
                "table_schema": "app",
                "table_name": "internal_logs",
                "column_name": "msg",
                "data_type": "text",
                "estimated_rows": 1,
            },
        ]
        pool = _FakePool(responses={"information_schema.columns": rows})
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)), pool=pool
        )
        try:
            refs = [
                r async for r in c.discover(SourceFilter(include=("app.users",)), None)
            ]
            assert {r.path for r in refs} == {"app.users"}
        finally:
            await c.close()
        pool2 = _FakePool(responses={"information_schema.columns": rows})
        c2 = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)), pool=pool2
        )
        try:
            refs2 = [
                r
                async for r in c2.discover(
                    SourceFilter(exclude=("app.internal_*",)), None
                )
            ]
            assert {r.path for r in refs2} == {"app.users"}
        finally:
            await c2.close()


# --- fetch --------------------------------------------------------


class TestFetch:
    async def test_fetch_serialises_row_columns(self) -> None:
        discover_rows = [
            {
                "table_schema": "app",
                "table_name": "users",
                "column_name": "email",
                "data_type": "varchar",
                "estimated_rows": 100,
            },
            {
                "table_schema": "app",
                "table_name": "users",
                "column_name": "name",
                "data_type": "varchar",
                "estimated_rows": 100,
            },
        ]
        sample_rows = [
            {"email": "alice@example.com", "name": "Alice"},
            {"email": "bob@example.com", "name": "Bob"},
        ]

        def responder(sql: str, _p: Any) -> list[dict[str, Any]]:
            upper = sql.upper()
            if "SHOW VARIABLES LIKE 'READ_ONLY'" in upper:
                return [{"Variable_name": "read_only", "Value": "ON"}]
            if "SHOW REPLICA STATUS" in upper:
                return [{"Source_Host": "primary"}]
            if "INFORMATION_SCHEMA.COLUMNS" in upper:
                return discover_rows
            if "SELECT" in upper and "USERS" in upper:
                return sample_rows
            return []

        pool = _FakePool(responder=responder)
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)), pool=pool
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 1
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 2
            assert isinstance(docs[0], Document)
            assert "email=alice@example.com" in docs[0].text
            assert "name=Alice" in docs[0].text
        finally:
            await c.close()

    async def test_fetch_with_cold_ref_reloads(self) -> None:
        # Cold path: discover cache empty, fetch reloads via _RELOAD_SQL.
        from pleno_pii_scanner.sources.base import DocumentRef

        reload_rows = [
            {"column_name": "email", "estimated_rows": 50},
            {"column_name": "name", "estimated_rows": 50},
        ]
        sample_rows = [{"email": "x@y.z", "name": "Z"}]

        def responder(sql: str, params: Any) -> list[dict[str, Any]]:
            upper = sql.upper()
            if "SHOW VARIABLES LIKE 'READ_ONLY'" in upper:
                return [{"Variable_name": "read_only", "Value": "ON"}]
            if "SHOW REPLICA STATUS" in upper:
                return [{"Source_Host": "primary"}]
            if "INFORMATION_SCHEMA.COLUMNS" in upper and params:
                return reload_rows
            if "SELECT" in upper and "USERS" in upper:
                return sample_rows
            return []

        pool = _FakePool(responder=responder)
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)), pool=pool
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="app.users",
                metadata={},
            )
            docs = [d async for d in c.fetch(ref)]
            assert len(docs) == 1
        finally:
            await c.close()

    async def test_fetch_unknown_table_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        pool = _FakePool()  # no information_schema rows
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)), pool=pool
        )
        try:
            ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="ghost.gone")
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_malformed_path_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        pool = _FakePool()
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)), pool=pool
        )
        try:
            ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="no_dot")
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()


# --- timeout hint -------------------------------------------------


class TestTimeoutHint:
    def test_select_gets_hint(self) -> None:
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)),
            pool=_FakePool(),
        )
        out = c._with_timeout_hint("SELECT 1")
        assert "MAX_EXECUTION_TIME(30000)" in out
        assert out.startswith("SELECT /*+ ")

    def test_non_select_passthrough(self) -> None:
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)),
            pool=_FakePool(),
        )
        out = c._with_timeout_hint("SHOW TABLES")
        assert "MAX_EXECUTION_TIME" not in out

    def test_leading_whitespace_preserved(self) -> None:
        c = MysqlConnector(
            MysqlConfig(dsn="mysql://u@h/db", schemas=("app",)),
            pool=_FakePool(),
        )
        out = c._with_timeout_hint("  \nSELECT 1")
        assert out.startswith("  \nSELECT /*+ ")


# --- helpers ------------------------------------------------------


class TestHelpers:
    def test_redact_dsn_strips_password(self) -> None:
        from pleno_pii_scanner_mysql.connector import _redact_dsn

        assert _redact_dsn("mysql://u:secret@h:3306/db") == "mysql://u@h:3306/db"

    def test_redact_dsn_no_password(self) -> None:
        from pleno_pii_scanner_mysql.connector import _redact_dsn

        assert _redact_dsn("mysql://u@h/db") == "mysql://u@h/db"

    def test_stringify_bytes(self) -> None:
        from pleno_pii_scanner_mysql.connector import _stringify

        assert _stringify(b"hello") == "hello"

    def test_stringify_bytearray(self) -> None:
        from pleno_pii_scanner_mysql.connector import _stringify

        assert _stringify(bytearray(b"hi")) == "hi"

    def test_stringify_none(self) -> None:
        from pleno_pii_scanner_mysql.connector import _stringify

        assert _stringify(None) == ""

    def test_stringify_invalid_utf8_replaced(self) -> None:
        from pleno_pii_scanner_mysql.connector import _stringify

        assert "�" in _stringify(b"\xff\xfe")

    def test_parse_dsn(self) -> None:
        from pleno_pii_scanner_mysql.connector import _parse_dsn

        p = _parse_dsn("mysql://u:p@h:3307/d")
        assert p["host"] == "h"
        assert p["port"] == 3307
        assert p["user"] == "u"
        assert p["password"] == "p"
        assert p["db"] == "d"

    def test_row_value_default_empty(self) -> None:
        from pleno_pii_scanner_mysql.connector import _row_value

        assert _row_value({}) == ""


# --- spec / factory ----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "mysql"
        assert SPEC.version == "0.1.0"

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("mysql", {"dsn": "mysql://u@h/db"})
        assert isinstance(c, MysqlConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "mysql",
            {
                "dsn": "mysql://u:p@h:3306/db",
                "schemas": ["app", "billing"],
                "tables": ["app.users"],
                "excluded_tables": ["app.internal_logs"],
                "sample_rows": 500,
                "statement_timeout_ms": 60_000,
                "pool_size": 4,
                "require_replica": False,
                "id": "my-id",
            },
        )
        assert c.id == "my-id"

    def test_factory_rejects_missing_dsn(self) -> None:
        with pytest.raises(ValueError, match="dsn"):
            SPEC.factory({})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_pool(self) -> None:
        # Build connector without pool injection; the pool is lazy
        # and never created here, so close is a no-op for the pool.
        c = MysqlConnector(MysqlConfig(dsn="mysql://u@h/db"))
        assert c._owns_pool
        await c.close()

    async def test_close_owns_pool_after_ensure(self) -> None:
        # Inject a fake pool but mark ownership via _owns_pool=True
        # to exercise the close path that touches the pool.
        c = MysqlConnector(MysqlConfig(dsn="mysql://u@h/db"))
        c._pool = _FakePool()
        await c.close()
        # Cleared after close
        assert c._pool is None

    async def test_close_external_pool_not_closed(self) -> None:
        pool = _FakePool()
        c = MysqlConnector(MysqlConfig(dsn="mysql://u@h/db"), pool=pool)
        await c.close()
        assert not pool.closed
