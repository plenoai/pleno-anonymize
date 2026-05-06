"""Tests for ElasticsearchConnector — uses httpx.MockTransport doubles."""

from __future__ import annotations

import base64
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
from pleno_pii_scanner_elasticsearch import (
    ElasticsearchConfig,
    ElasticsearchConnector,
    SPEC,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://es.example.com",
        transport=httpx.MockTransport(handler),
    )


# --- config -------------------------------------------------------


class TestConfig:
    def test_rejects_empty_hosts(self) -> None:
        with pytest.raises(ValueError, match="hosts"):
            ElasticsearchConfig(hosts=())

    def test_rejects_multiple_auth_modes(self) -> None:
        with pytest.raises(ValueError, match="at most one"):
            ElasticsearchConfig(hosts=("https://x",), api_key="k", bearer_token="b")

    def test_basic_user_requires_password(self) -> None:
        with pytest.raises(ValueError, match="basic_password"):
            ElasticsearchConfig(hosts=("https://x",), basic_user="u")

    def test_invalid_sample_fraction(self) -> None:
        with pytest.raises(ValueError, match="sample_fraction"):
            ElasticsearchConfig(hosts=("https://x",), sample_fraction=0.0)
        with pytest.raises(ValueError, match="sample_fraction"):
            ElasticsearchConfig(hosts=("https://x",), sample_fraction=1.5)

    def test_invalid_page_size(self) -> None:
        with pytest.raises(ValueError, match="page_size"):
            ElasticsearchConfig(hosts=("https://x",), page_size=0)

    def test_explicit_id(self) -> None:
        cfg = ElasticsearchConfig(hosts=("https://x",), id="explicit")
        assert cfg.resolved_id() == "explicit"

    def test_default_id_no_secret_leak(self) -> None:
        cfg = ElasticsearchConfig(hosts=("https://x",), api_key="VERY-SECRET")
        rid = cfg.resolved_id()
        assert "VERY-SECRET" not in rid
        assert rid.startswith("elasticsearch:")

    def test_default_id_order_independent(self) -> None:
        a = ElasticsearchConfig(hosts=("https://a", "https://b"), indices=("x", "y"))
        b = ElasticsearchConfig(hosts=("https://b", "https://a"), indices=("y", "x"))
        assert a.resolved_id() == b.resolved_id()


# --- protocol -----------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = ElasticsearchConnector(ElasticsearchConfig(hosts=("https://x",)))
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = ElasticsearchConnector(ElasticsearchConfig(hosts=("https://x",)))
        assert c.capabilities() == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- auth headers --------------------------------------------------


class TestAuth:
    def test_api_key_header(self) -> None:
        c = ElasticsearchConnector(
            ElasticsearchConfig(hosts=("https://x",), api_key="abc==")
        )
        assert c._headers["Authorization"] == "ApiKey abc=="

    def test_basic_header(self) -> None:
        c = ElasticsearchConnector(
            ElasticsearchConfig(
                hosts=("https://x",),
                basic_user="alice",
                basic_password="secret",
            )
        )
        expected = base64.b64encode(b"alice:secret").decode()
        assert c._headers["Authorization"] == f"Basic {expected}"

    def test_bearer_header(self) -> None:
        c = ElasticsearchConnector(
            ElasticsearchConfig(hosts=("https://x",), bearer_token="jwt-tok")
        )
        assert c._headers["Authorization"] == "Bearer jwt-tok"

    def test_no_auth_header_when_unset(self) -> None:
        c = ElasticsearchConnector(ElasticsearchConfig(hosts=("https://x",)))
        assert "Authorization" not in c._headers


# --- discover end-to-end ------------------------------------------


def _resolve_response(*indices: str) -> dict:
    return {"indices": [{"name": n} for n in indices]}


def _hits(*pairs: tuple[str, str, dict]) -> dict:
    """Build a hits payload. Each tuple is (index, _id, _source)."""
    return {
        "hits": {
            "hits": [
                {
                    "_index": idx,
                    "_id": doc_id,
                    "_source": src,
                    "sort": [i],
                }
                for i, (idx, doc_id, src) in enumerate(pairs, start=1)
            ]
        }
    }


class TestDiscover:
    async def test_full_pipeline_yields_refs(self) -> None:
        opened_pit: dict[str, bool] = {"yes": False}
        closed_pit: dict[str, bool] = {"yes": False}
        page_calls: dict[str, int] = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("Authorization") == "ApiKey k"
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("logs-2026.05"))
            if url.path.endswith("/_pit") and request.method == "DELETE":
                closed_pit["yes"] = True
                return httpx.Response(200, json={"succeeded": True})
            if url.path.endswith("/_pit"):
                opened_pit["yes"] = True
                return httpx.Response(200, json={"id": "PIT-1"})
            if url.path == "/_search":
                page_calls["n"] += 1
                if page_calls["n"] == 1:
                    return httpx.Response(
                        200,
                        json=_hits(
                            ("logs-2026.05", "a", {"message": "alice@x.com"}),
                            ("logs-2026.05", "b", {"message": "bob@x.com"}),
                        ),
                    )
                # Empty page → terminate.
                return httpx.Response(200, json={"hits": {"hits": []}})
            return httpx.Response(404, content=str(url).encode())

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(
                    hosts=("https://es.example.com",),
                    api_key="k",
                    text_fields=("message",),
                    page_size=2,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert opened_pit["yes"]
                assert len(refs) == 2
                assert all(r.path.startswith("logs-2026.05/") for r in refs)
                # Each ref carries a fresh cursor.
                assert all(r.metadata["_cursor"] for r in refs)
                docs = [d async for d in c.fetch(refs[0])]
                assert isinstance(docs[0], Document)
                assert "message=alice@x.com" in docs[0].text
            finally:
                await c.close()
        assert closed_pit["yes"], "PIT must be closed after discover"

    async def test_pagination_advances_search_after(self) -> None:
        captured_search_after: list[list | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT-1"})
            if url.path == "/_search":
                body = json.loads(request.content)
                captured_search_after.append(body.get("search_after"))
                # Two full pages then empty.
                if len(captured_search_after) == 1:
                    return httpx.Response(
                        200,
                        json={
                            "hits": {
                                "hits": [
                                    {
                                        "_index": "idx",
                                        "_id": "1",
                                        "_source": {},
                                        "sort": [10],
                                    },
                                    {
                                        "_index": "idx",
                                        "_id": "2",
                                        "_source": {},
                                        "sort": [20],
                                    },
                                ]
                            }
                        },
                    )
                if len(captured_search_after) == 2:
                    return httpx.Response(
                        200,
                        json={
                            "hits": {
                                "hits": [
                                    {
                                        "_index": "idx",
                                        "_id": "3",
                                        "_source": {},
                                        "sort": [30],
                                    },
                                    {
                                        "_index": "idx",
                                        "_id": "4",
                                        "_source": {},
                                        "sort": [40],
                                    },
                                ]
                            }
                        },
                    )
                return httpx.Response(200, json={"hits": {"hits": []}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(
                    hosts=("https://es.example.com",),
                    api_key="k",
                    page_size=2,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert [r.path for r in refs] == [
                    "idx/1",
                    "idx/2",
                    "idx/3",
                    "idx/4",
                ]
                # First search has no search_after; subsequent ones do.
                assert captured_search_after[0] is None
                assert captured_search_after[1] == [20]
                assert captured_search_after[2] == [40]
            finally:
                await c.close()

    async def test_partial_page_terminates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT-1"})
            if url.path == "/_search":
                return httpx.Response(
                    200,
                    json={
                        "hits": {
                            "hits": [
                                {
                                    "_index": "idx",
                                    "_id": "1",
                                    "_source": {},
                                    "sort": [1],
                                },
                            ]
                        }
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(
                    hosts=("https://es.example.com",),
                    api_key="k",
                    page_size=10,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # 1 hit < page_size 10 → discover returns immediately
                assert len(refs) == 1
            finally:
                await c.close()

    async def test_cursor_resume_uses_supplied_pit(self) -> None:
        opened: dict[str, int] = {"n": 0}
        last_search_after: list[list | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                opened["n"] += 1
                return httpx.Response(200, json={"id": "PIT-NEW"})
            if url.path == "/_search":
                body = json.loads(request.content)
                last_search_after.append(body.get("search_after"))
                # Provided PIT must be used — assert it matches what we supplied.
                assert body["pit"]["id"] == "PIT-RESUMED"
                return httpx.Response(200, json={"hits": {"hits": []}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://es.example.com",), api_key="k"),
                client=client,
            )
            try:
                cursor = json.dumps({"pit_id": "PIT-RESUMED", "search_after": [99]})
                refs = [r async for r in c.discover(SourceFilter(), cursor)]
                assert refs == []
                # Did not open a fresh PIT when one was supplied.
                assert opened["n"] == 0
                # search_after restored.
                assert last_search_after[0] == [99]
            finally:
                await c.close()

    async def test_no_indices_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json={"indices": []})
            return httpx.Response(500)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k"),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
            finally:
                await c.close()

    async def test_empty_first_page_terminates_without_yield(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT"})
            if url.path == "/_search":
                return httpx.Response(200, json={"hits": {"hits": []}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k"),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
            finally:
                await c.close()

    async def test_missing_sort_terminates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT"})
            if url.path == "/_search":
                # Page with hits but no `sort` field — defensive termination
                return httpx.Response(
                    200,
                    json={
                        "hits": {"hits": [{"_index": "idx", "_id": "x", "_source": {}}]}
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k", page_size=100),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # Yields the one hit then bails because sort is missing.
                assert len(refs) == 1
            finally:
                await c.close()


# --- sampling ------------------------------------------------------


class TestSampling:
    async def test_sample_fraction_uses_random_score(self) -> None:
        captured_query: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT"})
            if url.path == "/_search":
                body = json.loads(request.content)
                captured_query.append(body["query"])
                return httpx.Response(200, json={"hits": {"hits": []}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(
                    hosts=("https://x",),
                    api_key="k",
                    sample_fraction=0.05,
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert "function_score" in captured_query[0]
                fs = captured_query[0]["function_score"]
                assert "random_score" in fs
                assert fs["min_score"] == pytest.approx(0.95)
            finally:
                await c.close()


# --- flavor: opensearch -------------------------------------------


class TestOpenSearchFlavor:
    async def test_opensearch_uses_point_in_time_url(self) -> None:
        opened: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_search/point_in_time"):
                opened["url"] = url.path
                return httpx.Response(200, json={"pit_id": "OS-PIT"})
            if url.path == "/_search/point_in_time" and request.method == "DELETE":
                return httpx.Response(200)
            if url.path == "/_search":
                return httpx.Response(200, json={"hits": {"hits": []}})
            return httpx.Response(404, content=str(url).encode())

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(
                    hosts=("https://x",), api_key="k", flavor="opensearch"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
                assert opened["url"].endswith("/_search/point_in_time")
            finally:
                await c.close()

    async def test_pit_unavailable_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                return httpx.Response(404)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k"),
                client=client,
            )
            try:
                with pytest.raises(RuntimeError, match="PIT unavailable"):
                    _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()


# --- filter --------------------------------------------------------


class TestFilter:
    async def test_include_exclude_filters_paths(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("logs", "audit"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT"})
            if url.path == "/_search":
                return httpx.Response(
                    200,
                    json={
                        "hits": {
                            "hits": [
                                {
                                    "_index": "logs",
                                    "_id": "1",
                                    "_source": {},
                                    "sort": [1],
                                },
                                {
                                    "_index": "audit",
                                    "_id": "2",
                                    "_source": {},
                                    "sort": [2],
                                },
                            ]
                        }
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k", page_size=10),
                client=client,
            )
            try:
                refs = [
                    r async for r in c.discover(SourceFilter(include=("logs/*",)), None)
                ]
                assert [r.path for r in refs] == ["logs/1"]
            finally:
                await c.close()
        async with _client(handler) as client2:
            c2 = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k", page_size=10),
                client=client2,
            )
            try:
                refs2 = [
                    r
                    async for r in c2.discover(SourceFilter(exclude=("audit/*",)), None)
                ]
                assert [r.path for r in refs2] == ["logs/1"]
            finally:
                await c2.close()


# --- _source rendering --------------------------------------------


class TestRenderSource:
    async def test_default_serialises_full_source_as_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT"})
            if url.path == "/_search":
                return httpx.Response(
                    200,
                    json={
                        "hits": {
                            "hits": [
                                {
                                    "_index": "idx",
                                    "_id": "x",
                                    "_source": {
                                        "user": "alice",
                                        "ip": "1.2.3.4",
                                    },
                                    "sort": [1],
                                }
                            ]
                        }
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k", page_size=10),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                # Whole _source serialised as JSON
                assert "alice" in docs[0].text
                assert "1.2.3.4" in docs[0].text
            finally:
                await c.close()

    async def test_text_fields_handles_complex_values(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = request.url
            if url.path.startswith("/_resolve/index/"):
                return httpx.Response(200, json=_resolve_response("idx"))
            if url.path.endswith("/_pit"):
                if request.method == "DELETE":
                    return httpx.Response(200)
                return httpx.Response(200, json={"id": "PIT"})
            if url.path == "/_search":
                return httpx.Response(
                    200,
                    json={
                        "hits": {
                            "hits": [
                                {
                                    "_index": "idx",
                                    "_id": "x",
                                    "_source": {
                                        "msg": "hello",
                                        "tags": ["a", "b"],
                                        "meta": {"k": "v"},
                                        "missing": None,
                                        "count": 42,
                                    },
                                    "sort": [1],
                                }
                            ]
                        }
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(
                    hosts=("https://x",),
                    api_key="k",
                    page_size=10,
                    text_fields=("msg", "tags", "meta", "missing", "count"),
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                t = docs[0].text
                assert "msg=hello" in t
                assert "tags=a, b" in t
                assert '"k": "v"' in t
                assert "count=42" in t
                # None-valued field skipped
                assert "missing=" not in t
            finally:
                await c.close()


# --- fetch edges --------------------------------------------------


class TestFetchEdges:
    async def test_fetch_unknown_path_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(lambda _r: httpx.Response(404)) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k"),
                client=client,
            )
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="missing")
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = ElasticsearchConnector(ElasticsearchConfig(hosts=("https://x",)))
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = ElasticsearchConnector(
            ElasticsearchConfig(hosts=("https://x",)), client=client
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()

    async def test_close_swallows_pit_errors(self) -> None:
        # Simulate the underlying transport raising — _close_pit must not
        # propagate; otherwise a network blip during shutdown would surface
        # as a noisy traceback to the scheduler.
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.RequestError("simulated transport failure")

        async with _client(handler) as client:
            c = ElasticsearchConnector(
                ElasticsearchConfig(hosts=("https://x",), api_key="k"),
                client=client,
            )
            c._open_pits.append("STALE-PIT")
            await c.close()
            assert c._open_pits == []


# --- spec / factory -----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "elasticsearch"
        assert SPEC.version == "0.1.0"

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("elasticsearch", {"hosts": ["https://x"]})
        assert isinstance(c, ElasticsearchConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "elasticsearch",
            {
                "hosts": ["https://x"],
                "indices": ["a", "b"],
                "api_key": "k",
                "flavor": "opensearch",
                "sample_fraction": 0.5,
                "text_fields": ["msg"],
                "page_size": 10,
                "keep_alive": "1m",
                "id": "explicit",
            },
        )
        assert c.id == "explicit"

    def test_factory_rejects_missing_hosts(self) -> None:
        with pytest.raises(ValueError, match="hosts"):
            SPEC.factory({})

    def test_factory_rejects_invalid_flavor(self) -> None:
        with pytest.raises(ValueError, match="flavor"):
            SPEC.factory({"hosts": ["https://x"], "flavor": "bogus"})
