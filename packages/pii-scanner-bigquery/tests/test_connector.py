"""Tests for BigQueryConnector — hermetic via httpx.MockTransport.

Bearer-token acquisition is monkeypatched on `_acquire_token` so we
never have to construct a real RS256 JWT in unit tests.
"""

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
from pleno_pii_scanner_bigquery import (
    SPEC,
    BigQueryConfig,
    BigQueryConnector,
    BigQueryCostCapExceeded,
)
from pleno_pii_scanner_bigquery import connector as _conn_mod


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _stub_token(c: BigQueryConnector, value: str = "test-token") -> None:
    """Pin the bearer token without forcing the real JWT/STS exchange."""

    async def _acq() -> str:
        return value

    c._acquire_token = _acq  # type: ignore[method-assign]


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_project(self) -> None:
        with pytest.raises(ValueError, match="project"):
            BigQueryConfig(project="", federated_token="t")

    def test_rejects_neither_auth(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            BigQueryConfig(project="p")

    def test_rejects_both_auth(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            BigQueryConfig(
                project="p",
                service_account_json="{}",
                federated_token="t",
            )

    def test_rejects_zero_sample(self) -> None:
        with pytest.raises(ValueError, match="sample_percent"):
            BigQueryConfig(project="p", federated_token="t", sample_percent=0)

    def test_rejects_oversized_sample(self) -> None:
        with pytest.raises(ValueError, match="sample_percent"):
            BigQueryConfig(
                project="p", federated_token="t", sample_percent=200
            )

    def test_rejects_zero_max_bytes(self) -> None:
        with pytest.raises(ValueError, match="max_bytes_billed"):
            BigQueryConfig(
                project="p", federated_token="t", max_bytes_billed=0
            )

    def test_rejects_zero_page_size(self) -> None:
        with pytest.raises(ValueError, match="page_size"):
            BigQueryConfig(project="p", federated_token="t", page_size=0)

    def test_explicit_id(self) -> None:
        cfg = BigQueryConfig(project="p", federated_token="t", id="x")
        assert cfg.resolved_id() == "x"

    def test_default_id_no_secret_leak(self) -> None:
        cfg = BigQueryConfig(
            project="p",
            federated_token="VERY-SECRET-TOKEN",
            datasets=("d1", "d2"),
        )
        rid = cfg.resolved_id()
        assert "VERY-SECRET-TOKEN" not in rid
        assert rid.startswith("bigquery:")

    def test_default_id_dataset_order_independent(self) -> None:
        a = BigQueryConfig(
            project="p", federated_token="t", datasets=("a", "b")
        )
        b = BigQueryConfig(
            project="p", federated_token="t", datasets=("b", "a")
        )
        assert a.resolved_id() == b.resolved_id()


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = BigQueryConnector(
            BigQueryConfig(project="p", federated_token="t")
        )
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = BigQueryConnector(
            BigQueryConfig(project="p", federated_token="t")
        )
        assert c.capabilities() == Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=False,
        )


# --- token acquisition ---------------------------------------------


class TestTokenAcquisition:
    async def test_federated_token_shortcut(self) -> None:
        # No HTTP needed for the shortcut path.
        async with _client(lambda _r: httpx.Response(500)) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="WIF-TOKEN"),
                client=client,
            )
            try:
                tok = await c._acquire_token()
                assert tok == "WIF-TOKEN"
                # Cached on second call.
                tok2 = await c._acquire_token()
                assert tok2 == "WIF-TOKEN"
            finally:
                await c.close()

    async def test_service_account_signing_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Monkeypatch _sign_sa_jwt so we don't pull in real RSA keys.
        monkeypatch.setattr(
            _conn_mod, "_sign_sa_jwt", lambda *_a, **_kw: "fake.jwt.signature"
        )

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = request.content.decode()
            return httpx.Response(
                200, json={"access_token": "exchanged-token", "expires_in": 3600}
            )

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(
                    project="p",
                    service_account_json=json.dumps(
                        {
                            "client_email": "sa@p.iam.gserviceaccount.com",
                            "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
                            "private_key_id": "kid-1",
                        }
                    ),
                ),
                client=client,
            )
            try:
                tok = await c._acquire_token()
                assert tok == "exchanged-token"
                assert "oauth2.googleapis.com/token" in captured["url"]
                assert "fake.jwt.signature" in captured["body"]
                assert "jwt-bearer" in captured["body"]
            finally:
                await c.close()

    async def test_service_account_token_endpoint_missing_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _conn_mod, "_sign_sa_jwt", lambda *_a, **_kw: "fake.jwt"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})  # no access_token field

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(
                    project="p",
                    service_account_json=json.dumps(
                        {
                            "client_email": "sa@p.iam.gserviceaccount.com",
                            "private_key": "x",
                            "private_key_id": "kid",
                        }
                    ),
                ),
                client=client,
            )
            try:
                with pytest.raises(RuntimeError, match="no access_token"):
                    await c._acquire_token()
            finally:
                await c.close()


# --- discover / dataset listing -----------------------------------


def _job_response(
    *,
    rows: list[dict] | None = None,
    schema_fields: list[dict] | None = None,
    page_token: str | None = None,
    job_id: str = "job-1",
    location: str = "US",
) -> dict:
    body: dict = {
        "jobReference": {
            "projectId": "p",
            "jobId": job_id,
            "location": location,
        },
        "schema": {"fields": schema_fields or [{"name": "col1", "type": "STRING"}]},
        "rows": rows or [],
    }
    if page_token:
        body["pageToken"] = page_token
    return body


def _dry_run_response(total_bytes: int = 1024) -> dict:
    return {
        "statistics": {"totalBytesProcessed": str(total_bytes)},
    }


class TestDiscoverDatasets:
    async def test_lists_datasets_via_api(self) -> None:
        sql_seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/datasets"):
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}},
                        ]
                    },
                )
            if url.endswith("/datasets/d1/tables"):
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {"tableReference": {"tableId": "t1"}},
                        ]
                    },
                )
            if "dryRun=true" in url:
                payload = json.loads(request.content)
                sql_seen.append(payload["configuration"]["query"]["query"])
                return httpx.Response(200, json=_dry_run_response(1024))
            if url.endswith("/queries"):
                return httpx.Response(
                    200,
                    json=_job_response(
                        rows=[{"f": [{"v": "alice"}]}],
                    ),
                )
            return httpx.Response(404, content=url.encode())

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(
                    project="p",
                    federated_token="t",
                    sample_percent=10.0,
                ),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
                assert refs[0].path == "d1/t1/0"
                # TABLESAMPLE included for sub-100 percent.
                assert any("TABLESAMPLE SYSTEM" in s for s in sql_seen)
            finally:
                await c.close()

    async def test_dataset_allowlist_skips_list_call(self) -> None:
        api_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            api_calls.append(url)
            if url.endswith("/datasets"):
                # Should NOT be called when allowlist is set.
                return httpx.Response(500)
            if url.endswith("/datasets/only/tables"):
                return httpx.Response(
                    200,
                    json={"tables": []},
                )
            return httpx.Response(404, content=url.encode())

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(
                    project="p",
                    federated_token="t",
                    datasets=("only",),
                ),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
                assert not any(u.endswith("/datasets") for u in api_calls)
            finally:
                await c.close()

    async def test_datasets_pagination(self) -> None:
        # Cover the nextPageToken loop in _list_datasets and _list_tables.
        ds_calls = {"n": 0}
        table_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/datasets/d1/tables" in url:
                table_calls["n"] += 1
                if "pageToken=tnext" in url:
                    return httpx.Response(
                        200,
                        json={
                            "tables": [
                                {"tableReference": {"tableId": "t2"}}
                            ]
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "tables": [{"tableReference": {"tableId": "t1"}}],
                        "nextPageToken": "tnext",
                    },
                )
            if url.rstrip("?").endswith("/datasets") or (
                "/datasets" in url and "/tables" not in url
            ):
                ds_calls["n"] += 1
                if "pageToken=dnext" in url:
                    return httpx.Response(
                        200,
                        json={
                            "datasets": []  # tail page yields nothing
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}},
                            # Garbage entries skipped silently.
                            {"datasetReference": {}},
                            {},
                        ],
                        "nextPageToken": "dnext",
                    },
                )
            if "dryRun=true" in url:
                return httpx.Response(200, json=_dry_run_response(1))
            if url.endswith("/queries"):
                return httpx.Response(200, json=_job_response(rows=[]))
            return httpx.Response(404, content=url.encode())

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
                assert ds_calls["n"] == 2
                # 2 tables for d1 means 2 table-list calls.
                assert table_calls["n"] == 2
            finally:
                await c.close()


# --- TABLESAMPLE semantics ----------------------------------------


class TestTableSampleClause:
    def test_omitted_at_100_percent(self) -> None:
        c = BigQueryConnector(
            BigQueryConfig(
                project="p", federated_token="t", sample_percent=100.0
            )
        )
        sql = c._build_sql("d", "t")
        assert "TABLESAMPLE" not in sql
        assert "`p.d.t`" in sql

    def test_included_below_100(self) -> None:
        c = BigQueryConnector(
            BigQueryConfig(
                project="p", federated_token="t", sample_percent=5.0
            )
        )
        sql = c._build_sql("d", "t")
        assert "TABLESAMPLE SYSTEM (5.0 PERCENT)" in sql


# --- dry-run cost cap ---------------------------------------------


class TestDryRunCostCap:
    async def test_raises_when_estimate_over_cap(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/datasets"):
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}}
                        ]
                    },
                )
            if url.endswith("/datasets/d1/tables"):
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {"tableReference": {"tableId": "huge"}}
                        ]
                    },
                )
            if "dryRun=true" in url:
                # 10 GB estimate vs 1 KB cap.
                return httpx.Response(
                    200, json=_dry_run_response(10 * 1024**3)
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(
                    project="p",
                    federated_token="t",
                    max_bytes_billed=1024,
                ),
                client=client,
            )
            _stub_token(c)
            try:
                with pytest.raises(BigQueryCostCapExceeded) as exc_info:
                    _ = [r async for r in c.discover(SourceFilter(), None)]
                assert exc_info.value.cap == 1024
                assert exc_info.value.total_bytes_processed == 10 * 1024**3
                assert "huge" in exc_info.value.sql
            finally:
                await c.close()

    async def test_passes_when_estimate_under_cap(self) -> None:
        # Exercises the "stats says 0 / unparseable" path too.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/datasets"):
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}}
                        ]
                    },
                )
            if url.endswith("/datasets/d1/tables"):
                return httpx.Response(
                    200,
                    json={
                        "tables": [{"tableReference": {"tableId": "t1"}}]
                    },
                )
            if "dryRun=true" in url:
                # Garbage value → falls through to 0, well under cap.
                return httpx.Response(
                    200, json={"statistics": {"totalBytesProcessed": "not-a-number"}}
                )
            if url.endswith("/queries"):
                return httpx.Response(200, json=_job_response(rows=[]))
            return httpx.Response(404)

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
            finally:
                await c.close()


# --- pagination ----------------------------------------------------


class TestPagination:
    async def test_paginates_query_results(self) -> None:
        get_query_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/datasets"):
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}}
                        ]
                    },
                )
            if url.endswith("/datasets/d1/tables"):
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {"tableReference": {"tableId": "t1"}}
                        ]
                    },
                )
            if "dryRun=true" in url:
                return httpx.Response(200, json=_dry_run_response(1))
            if url.endswith("/queries"):
                # First page with token → triggers getQueryResults follow-up.
                return httpx.Response(
                    200,
                    json=_job_response(
                        rows=[
                            {"f": [{"v": "row0"}]},
                            {"f": [{"v": "row1"}]},
                        ],
                        page_token="next-1",
                    ),
                )
            if "/queries/job-1" in url:
                get_query_calls["n"] += 1
                if "pageToken=next-1" in url:
                    return httpx.Response(
                        200,
                        json=_job_response(
                            rows=[{"f": [{"v": "row2"}]}],
                            page_token="next-2",
                            job_id="job-1",
                        ),
                    )
                if "pageToken=next-2" in url:
                    return httpx.Response(
                        200,
                        json=_job_response(
                            rows=[{"f": [{"v": "row3"}]}],
                            job_id="job-1",
                        ),
                    )
            return httpx.Response(404, content=url.encode())

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert [r.path for r in refs] == [
                    "d1/t1/0",
                    "d1/t1/1",
                    "d1/t1/2",
                    "d1/t1/3",
                ]
                # Second + third pages each via getQueryResults.
                assert get_query_calls["n"] == 2
            finally:
                await c.close()

    async def test_no_job_id_aborts_after_first_page(self) -> None:
        # Defensive: server returns a pageToken without a jobId — we
        # cannot follow up, so we stop cleanly rather than 404-loop.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/datasets"):
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}}
                        ]
                    },
                )
            if url.endswith("/datasets/d1/tables"):
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {"tableReference": {"tableId": "t1"}}
                        ]
                    },
                )
            if "dryRun=true" in url:
                return httpx.Response(200, json=_dry_run_response(1))
            if url.endswith("/queries"):
                # pageToken set but jobReference missing — no follow-up.
                return httpx.Response(
                    200,
                    json={
                        "schema": {"fields": [{"name": "c"}]},
                        "rows": [{"f": [{"v": "x"}]}],
                        "pageToken": "next",
                    },
                )
            return httpx.Response(404, content=url.encode())

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # First page yielded once; pagination aborted gracefully.
                assert [r.path for r in refs] == ["d1/t1/0"]
            finally:
                await c.close()


# --- document body / row projection -------------------------------


class TestFetchDocument:
    async def test_text_contains_row_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/datasets"):
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}}
                        ]
                    },
                )
            if url.endswith("/datasets/d1/tables"):
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {"tableReference": {"tableId": "users"}}
                        ]
                    },
                )
            if "dryRun=true" in url:
                return httpx.Response(200, json=_dry_run_response(1))
            if url.endswith("/queries"):
                return httpx.Response(
                    200,
                    json=_job_response(
                        schema_fields=[
                            {"name": "name"},
                            {"name": "email"},
                        ],
                        rows=[
                            {
                                "f": [
                                    {"v": "Alice"},
                                    {"v": "alice@example.com"},
                                ]
                            }
                        ],
                    ),
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
                docs = [d async for d in c.fetch(refs[0])]
                assert len(docs) == 1
                assert isinstance(docs[0], Document)
                payload = json.loads(docs[0].text)
                assert payload == {"name": "Alice", "email": "alice@example.com"}
                # Metadata propagated.
                assert refs[0].metadata["bq_dataset"] == "d1"
                assert refs[0].metadata["bq_table"] == "users"
                assert refs[0].metadata["row_index"] == "0"
            finally:
                await c.close()

    async def test_unknown_path_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(lambda _r: httpx.Response(404)) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client,
            )
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()

    def test_project_row_handles_garbage_shape(self) -> None:
        from pleno_pii_scanner_bigquery.connector import _project_row

        # Non-mapping cell falls back to None.
        assert _project_row(
            {"f": [{"v": "x"}, "garbage"]}, ["a", "b"]
        ) == {"a": "x", "b": None}
        # Cell list longer than schema is truncated.
        assert _project_row(
            {"f": [{"v": 1}, {"v": 2}]}, ["a"]
        ) == {"a": 1}
        # Missing 'f' returns empty dict.
        assert _project_row({"x": 1}, ["a"]) == {}
        # Non-mapping row returns empty dict.
        assert _project_row("not-a-mapping", ["a"]) == {}  # type: ignore[arg-type]


# --- filter --------------------------------------------------------


class TestFilter:
    async def test_include_exclude_filters_paths(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.endswith("/datasets"):
                return httpx.Response(
                    200,
                    json={
                        "datasets": [
                            {"datasetReference": {"datasetId": "d1"}}
                        ]
                    },
                )
            if url.endswith("/datasets/d1/tables"):
                return httpx.Response(
                    200,
                    json={
                        "tables": [
                            {"tableReference": {"tableId": "users"}},
                            {"tableReference": {"tableId": "internal"}},
                        ]
                    },
                )
            if "dryRun=true" in url:
                return httpx.Response(200, json=_dry_run_response(1))
            if url.endswith("/queries"):
                # Each table issued the same shape.
                return httpx.Response(
                    200,
                    json=_job_response(
                        rows=[{"f": [{"v": "x"}]}],
                    ),
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client,
            )
            _stub_token(c)
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("d1/users/*",)), None
                    )
                ]
                assert all(r.path.startswith("d1/users/") for r in refs)
                assert all(
                    r.metadata["bq_table"] == "users" for r in refs
                )
            finally:
                await c.close()

        async with _client(handler) as client2:
            c2 = BigQueryConnector(
                BigQueryConfig(project="p", federated_token="t"),
                client=client2,
            )
            _stub_token(c2)
            try:
                refs2 = [
                    r
                    async for r in c2.discover(
                        SourceFilter(exclude=("d1/internal/*",)), None
                    )
                ]
                assert all(r.metadata["bq_table"] == "users" for r in refs2)
            finally:
                await c2.close()


# --- factory + spec -----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "bigquery"
        assert SPEC.version == "0.1.0"
        assert SPEC.required_scopes == (
            "https://www.googleapis.com/auth/bigquery.readonly",
        )

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("bigquery", {"project": "p", "federated_token": "t"})
        assert isinstance(c, BigQueryConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "bigquery",
            {
                "project": "p",
                "datasets": ["d1"],
                "service_account_json": json.dumps(
                    {
                        "client_email": "x",
                        "private_key": "y",
                        "private_key_id": "z",
                    }
                ),
                "sample_percent": 5.0,
                "max_bytes_billed": 2048,
                "page_size": 100,
                "location": "EU",
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_project(self) -> None:
        with pytest.raises(ValueError, match="project"):
            SPEC.factory({})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = BigQueryConnector(
            BigQueryConfig(project="p", federated_token="t")
        )
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = BigQueryConnector(
            BigQueryConfig(project="p", federated_token="t"),
            client=client,
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()


# --- helpers ------------------------------------------------------


class TestHelpers:
    def test_b64url_strips_padding(self) -> None:
        from pleno_pii_scanner_bigquery.connector import _b64url

        # Multiple of 3 → no padding to strip; non-multiple → padding stripped.
        assert _b64url(b"abc") == "YWJj"
        assert "=" not in _b64url(b"a")

    def test_sign_sa_jwt_produces_three_segments(self) -> None:
        # End-to-end signing path with a generated key — covers the
        # otherwise-unreached cryptography branch.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        sa = {
            "client_email": "sa@p.iam.gserviceaccount.com",
            "private_key": pem,
            "private_key_id": "kid",
        }
        from pleno_pii_scanner_bigquery.connector import _sign_sa_jwt

        token = _sign_sa_jwt(sa, scope="s", lifetime_secs=60)
        assert token.count(".") == 2
