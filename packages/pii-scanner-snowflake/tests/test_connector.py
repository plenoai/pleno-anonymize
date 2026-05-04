"""Tests for SnowflakeConnector — httpx.MockTransport hermetic doubles."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
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
from pleno_pii_scanner_snowflake import (
    SPEC,
    SnowflakeConfig,
    SnowflakeConnector,
)


_FAKE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "fake-pem-not-actually-used-tests-monkeypatch-the-signer\n"
    "-----END PRIVATE KEY-----\n"
)
_FAKE_JWT = "header.payload.signature"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch: pytest.MonkeyPatch):
    """Bypass real RS256 signing — tests don't ship key material."""
    async def _fixed(self):  # noqa: ARG001
        return _FAKE_JWT

    monkeypatch.setattr(SnowflakeConnector, "_acquire_token", _fixed)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://acct.snowflakecomputing.com",
        transport=httpx.MockTransport(handler),
    )


def _config(**overrides) -> SnowflakeConfig:
    base = dict(
        account="acct",
        user="svc",
        private_key_pem=_FAKE_KEY,
    )
    base.update(overrides)
    return SnowflakeConfig(**base)


def _ok(rows: list[list], columns: list[str], *, partitions: int = 1, handle: str = "h") -> dict:
    return {
        "statementHandle": handle,
        "resultSetMetaData": {
            "rowType": [{"name": c} for c in columns],
            "partitionInfo": [{"rowCount": len(rows)} for _ in range(partitions)],
        },
        "data": rows,
    }


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_account(self) -> None:
        with pytest.raises(ValueError, match="account"):
            SnowflakeConfig(account="", user="u", private_key_pem=_FAKE_KEY)

    def test_rejects_empty_user(self) -> None:
        with pytest.raises(ValueError, match="user"):
            SnowflakeConfig(account="a", user="", private_key_pem=_FAKE_KEY)

    def test_rejects_empty_private_key(self) -> None:
        with pytest.raises(ValueError, match="private_key_pem"):
            SnowflakeConfig(account="a", user="u", private_key_pem="")

    def test_rejects_empty_warehouse(self) -> None:
        with pytest.raises(ValueError, match="warehouse"):
            SnowflakeConfig(
                account="a", user="u", private_key_pem=_FAKE_KEY, warehouse=""
            )

    def test_rejects_empty_role(self) -> None:
        with pytest.raises(ValueError, match="role"):
            SnowflakeConfig(
                account="a", user="u", private_key_pem=_FAKE_KEY, role=""
            )

    def test_rejects_zero_statement_timeout(self) -> None:
        with pytest.raises(ValueError, match="statement_timeout_seconds"):
            SnowflakeConfig(
                account="a",
                user="u",
                private_key_pem=_FAKE_KEY,
                statement_timeout_seconds=0,
            )

    def test_rejects_negative_statement_timeout(self) -> None:
        with pytest.raises(ValueError, match="statement_timeout_seconds"):
            SnowflakeConfig(
                account="a",
                user="u",
                private_key_pem=_FAKE_KEY,
                statement_timeout_seconds=-1,
            )

    def test_rejects_zero_sample_rows(self) -> None:
        with pytest.raises(ValueError, match="sample_rows"):
            SnowflakeConfig(
                account="a", user="u", private_key_pem=_FAKE_KEY, sample_rows=0
            )

    def test_explicit_id(self) -> None:
        cfg = _config(id="custom")
        assert cfg.resolved_id() == "custom"

    def test_default_id(self) -> None:
        cfg = _config()
        assert cfg.resolved_id() == "snowflake:acct:svc"

    def test_base_url(self) -> None:
        assert _config().base_url() == "https://acct.snowflakecomputing.com"


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = SnowflakeConnector(_config())
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = SnowflakeConnector(_config())
        assert c.capabilities() == Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=False,
        )


# --- end-to-end ---------------------------------------------------


def _build_handler(
    *,
    expect_databases: list[str] | None = None,
    expect_schemas: dict[str, list[str]] | None = None,
    expect_tables: dict[tuple[str, str], list[str]] | None = None,
    table_data: dict[tuple[str, str, str], tuple[list[list], list[str]]] | None = None,
    captured_bodies: list[dict] | None = None,
    captured_headers: list[dict] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured_headers is not None:
            captured_headers.append(dict(request.headers))
        body = json.loads(request.content) if request.content else {}
        if captured_bodies is not None:
            captured_bodies.append(body)
        sql = body.get("statement", "")
        if sql == "SHOW DATABASES":
            names = expect_databases or []
            return httpx.Response(200, json=_ok([[n] for n in names], ["name"]))
        if sql.startswith("SHOW SCHEMAS IN DATABASE"):
            db = sql.split('"')[1]
            names = (expect_schemas or {}).get(db, [])
            return httpx.Response(200, json=_ok([[n] for n in names], ["name"]))
        if sql.startswith("SHOW TABLES IN SCHEMA"):
            parts = sql.split('"')
            db, schema = parts[1], parts[3]
            names = (expect_tables or {}).get((db, schema), [])
            return httpx.Response(200, json=_ok([[n] for n in names], ["name"]))
        if sql.startswith("SELECT * FROM"):
            parts = sql.split('"')
            db, schema, table = parts[1], parts[3], parts[5]
            rows, cols = (table_data or {}).get((db, schema, table), ([], []))
            return httpx.Response(200, json=_ok(rows, cols))
        return httpx.Response(404, content=f"unhandled: {sql!r}".encode())

    return handler


class TestEndToEnd:
    async def test_full_pipeline(self) -> None:
        handler = _build_handler(
            expect_databases=["DB1"],
            expect_schemas={"DB1": ["PUB"]},
            expect_tables={("DB1", "PUB"): ["T1"]},
            table_data={
                ("DB1", "PUB", "T1"): (
                    [["alice", 30], ["bob", 41]],
                    ["NAME", "AGE"],
                )
            },
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 2
                assert refs[0].path == "DB1/PUB/T1/0"
                assert refs[1].path == "DB1/PUB/T1/1"
                docs = [d async for d in c.fetch(refs[0])]
                assert isinstance(docs[0], Document)
                payload = json.loads(docs[0].text)
                assert payload == {"NAME": "alice", "AGE": 30}
            finally:
                await c.close()

    async def test_databases_allowlist_skips_show(self) -> None:
        bodies: list[dict] = []
        handler = _build_handler(
            expect_schemas={"PROD": ["S"]},
            expect_tables={("PROD", "S"): []},
            captured_bodies=bodies,
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(
                _config(databases=("PROD",)), client=client
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert not any(b["statement"] == "SHOW DATABASES" for b in bodies)

    async def test_schemas_allowlist_skips_show(self) -> None:
        bodies: list[dict] = []
        handler = _build_handler(
            expect_databases=["DB1"],
            expect_tables={("DB1", "S"): []},
            captured_bodies=bodies,
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(schemas=("S",)), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert not any(
            b["statement"].startswith("SHOW SCHEMAS") for b in bodies
        )

    async def test_session_parameters_present(self) -> None:
        bodies: list[dict] = []
        handler = _build_handler(captured_bodies=bodies)
        async with _client(handler) as client:
            c = SnowflakeConnector(
                _config(
                    warehouse="MY_XS",
                    role="MY_RO",
                    statement_timeout_seconds=123,
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert bodies
        params = bodies[0]["parameters"]
        assert params["WAREHOUSE"] == "MY_XS"
        assert params["ROLE"] == "MY_RO"
        assert params["STATEMENT_TIMEOUT_IN_SECONDS"] == "123"

    async def test_jwt_auth_headers(self) -> None:
        headers: list[dict] = []
        handler = _build_handler(captured_headers=headers)
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert headers
        h = headers[0]
        assert h["authorization"] == f"Bearer {_FAKE_JWT}"
        assert h["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"

    async def test_sample_clause_uses_sample_rows(self) -> None:
        bodies: list[dict] = []
        handler = _build_handler(
            expect_databases=["DB"],
            expect_schemas={"DB": ["S"]},
            expect_tables={("DB", "S"): ["T"]},
            table_data={("DB", "S", "T"): ([], ["X"])},
            captured_bodies=bodies,
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(sample_rows=42), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        sample_sql = next(
            b["statement"] for b in bodies if b["statement"].startswith("SELECT")
        )
        assert "SAMPLE (42 ROWS)" in sample_sql
        assert '"DB"."S"."T"' in sample_sql

    async def test_show_tables_uses_qualified_schema(self) -> None:
        bodies: list[dict] = []
        handler = _build_handler(
            expect_databases=["DB"],
            expect_schemas={"DB": ["MY_S"]},
            expect_tables={("DB", "MY_S"): []},
            captured_bodies=bodies,
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert any(
            b["statement"] == 'SHOW TABLES IN SCHEMA "DB"."MY_S"' for b in bodies
        )


# --- pagination ---------------------------------------------------


class TestPagination:
    async def test_multi_partition_aggregates(self) -> None:
        # Two partitions: first comes back inline with the POST,
        # second comes via GET ?partition=1.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                body = json.loads(request.content)
                sql = body["statement"]
                if sql == "SHOW DATABASES":
                    return httpx.Response(200, json=_ok([["DB"]], ["name"]))
                if sql.startswith("SHOW SCHEMAS"):
                    return httpx.Response(200, json=_ok([["S"]], ["name"]))
                if sql.startswith("SHOW TABLES"):
                    return httpx.Response(200, json=_ok([["T"]], ["name"]))
                if sql.startswith("SELECT"):
                    payload = _ok(
                        [["a"]], ["NAME"], partitions=2, handle="HX"
                    )
                    return httpx.Response(200, json=payload)
            if request.method == "GET":
                assert "/api/v2/statements/HX" in str(request.url)
                assert request.url.params["partition"] == "1"
                return httpx.Response(
                    200, json={"data": [["b"], ["c"]]}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = SnowflakeConnector(_config(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # 1 + 2 rows aggregated across partitions.
                assert len(refs) == 3
                payloads = [json.loads(r.metadata["_payload"]) for r in refs]
                assert payloads == [
                    {"NAME": "a"},
                    {"NAME": "b"},
                    {"NAME": "c"},
                ]
            finally:
                await c.close()


# --- filter -------------------------------------------------------


class TestFilter:
    async def test_include_matches_path(self) -> None:
        handler = _build_handler(
            expect_databases=["DB"],
            expect_schemas={"DB": ["S"]},
            expect_tables={("DB", "S"): ["T"]},
            table_data={("DB", "S", "T"): ([["a"], ["b"], ["c"]], ["X"])},
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(), client=client)
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("DB/S/T/0",)), None
                    )
                ]
                assert [r.path for r in refs] == ["DB/S/T/0"]
            finally:
                await c.close()

    async def test_exclude_drops_path(self) -> None:
        handler = _build_handler(
            expect_databases=["DB"],
            expect_schemas={"DB": ["S"]},
            expect_tables={("DB", "S"): ["T"]},
            table_data={("DB", "S", "T"): ([["a"], ["b"]], ["X"])},
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(), client=client)
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("DB/S/T/0",)), None
                    )
                ]
                assert [r.path for r in refs] == ["DB/S/T/1"]
            finally:
                await c.close()


# --- fetch payload -------------------------------------------------


class TestFetchPayload:
    async def test_document_text_is_row_json(self) -> None:
        handler = _build_handler(
            expect_databases=["D"],
            expect_schemas={"D": ["S"]},
            expect_tables={("D", "S"): ["T"]},
            table_data={
                ("D", "S", "T"): (
                    [["alice", "alice@example.com"]],
                    ["NAME", "EMAIL"],
                )
            },
        )
        async with _client(handler) as client:
            c = SnowflakeConnector(_config(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert isinstance(docs[0], Document)
                row = json.loads(docs[0].text)
                assert row == {"NAME": "alice", "EMAIL": "alice@example.com"}
            finally:
                await c.close()


# --- factory / spec -----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "snowflake"
        assert SPEC.version == "0.1.0"
        assert SPEC.required_scopes == ("snowflake:read",)

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create(
            "snowflake",
            {"account": "a", "user": "u", "private_key_pem": _FAKE_KEY},
        )
        assert isinstance(c, SnowflakeConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "snowflake",
            {
                "account": "a",
                "user": "u",
                "private_key_pem": _FAKE_KEY,
                "warehouse": "WH",
                "databases": ["D"],
                "schemas": ["S"],
                "sample_rows": 5,
                "statement_timeout_seconds": 10,
                "role": "R",
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_account(self) -> None:
        with pytest.raises(ValueError, match="account"):
            SPEC.factory({"user": "u", "private_key_pem": _FAKE_KEY})

    def test_factory_rejects_missing_user(self) -> None:
        with pytest.raises(ValueError, match="user"):
            SPEC.factory({"account": "a", "private_key_pem": _FAKE_KEY})

    def test_factory_rejects_missing_private_key(self) -> None:
        with pytest.raises(ValueError, match="private_key_pem"):
            SPEC.factory({"account": "a", "user": "u"})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = SnowflakeConnector(_config())
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = SnowflakeConnector(_config(), client=client)
        await c.close()
        assert not client.is_closed
        await client.aclose()


# --- helpers / projection edges ----------------------------------


class TestRowProjection:
    async def test_extra_row_columns_get_synthetic_names(self) -> None:
        # If the row is wider than rowType (defensive — server bug
        # or schema-evolution race), synthesize `_colN` placeholders
        # rather than dropping data on the floor.
        from pleno_pii_scanner_snowflake.connector import _project_row

        out = _project_row(["A"], ["x", "y", "z"])
        assert out == {"A": "x", "_col1": "y", "_col2": "z"}

    async def test_pick_falls_back_to_first_column(self) -> None:
        from pleno_pii_scanner_snowflake.connector import _pick

        # Column not present → first cell as fallback.
        assert _pick(["other"], ["fallback"], "name") == "fallback"
        # Empty row → empty string.
        assert _pick(["other"], [], "name") == ""
