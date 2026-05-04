"""Tests for PostmanConnector — uses httpx.MockTransport doubles."""

from __future__ import annotations

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
from pleno_pii_scanner_postman import (
    PostmanConfig,
    PostmanConnector,
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
        base_url="https://api.getpostman.com",
        transport=httpx.MockTransport(handler),
    )


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            PostmanConfig(api_key="")

    def test_explicit_id(self) -> None:
        cfg = PostmanConfig(api_key="k", id="x")
        assert cfg.resolved_id() == "x"

    def test_default_id_no_key_leak(self) -> None:
        cfg = PostmanConfig(api_key="VERYSECRET", workspaces=("a", "b"))
        rid = cfg.resolved_id()
        assert "VERYSECRET" not in rid
        assert rid.startswith("postman:")

    def test_default_id_order_independent(self) -> None:
        a = PostmanConfig(api_key="k", workspaces=("a", "b"))
        b = PostmanConfig(api_key="k", workspaces=("b", "a"))
        assert a.resolved_id() == b.resolved_id()


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = PostmanConnector(PostmanConfig(api_key="k"))
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = PostmanConnector(PostmanConfig(api_key="k"))
        assert c.capabilities() == Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=False,
        )


# --- end-to-end ---------------------------------------------------


def _ws_detail(env_id: str = "e1", coll_id: str = "c1") -> dict:
    return {
        "workspace": {
            "id": "w1",
            "name": "team",
            "collections": [{"id": coll_id, "name": "coll"}],
            "environments": [{"id": env_id, "name": "prod"}],
        }
    }


def _env(env_id: str = "e1") -> dict:
    return {
        "environment": {
            "id": env_id,
            "name": "prod",
            "values": [
                {"key": "api_key", "value": "AKIA12345", "enabled": True},
                {"key": "host", "value": "api.example.com", "enabled": True},
                {"key": "disabled_one", "value": "skip", "enabled": False},
            ],
        }
    }


def _collection_basic() -> dict:
    return {
        "collection": {
            "info": {"name": "MyColl"},
            "variable": [{"key": "v_collection", "value": "coll-only"}],
            "item": [
                {
                    "name": "list-users",
                    "request": {
                        "method": "GET",
                        "header": [
                            {
                                "key": "Authorization",
                                "value": "Bearer {{api_key}}",
                            },
                            {
                                "key": "X-Skipped",
                                "value": "x",
                                "disabled": True,
                            },
                        ],
                        "url": {
                            "raw": "https://{{host}}/users",
                            "protocol": "https",
                            "host": ["{{host}}"],
                            "path": ["users"],
                        },
                        "auth": {
                            "type": "bearer",
                            "bearer": [{"key": "token", "value": "{{api_key}}"}],
                        },
                        "body": {
                            "mode": "raw",
                            "raw": '{"q": "{{v_collection}}"}',
                        },
                    },
                    "event": [
                        {
                            "listen": "prerequest",
                            "script": {"exec": ["console.log('{{api_key}}')"]},
                        }
                    ],
                    "response": [
                        {"name": "200-ok", "body": "Bearer real-{{api_key}}"}
                    ],
                }
            ],
        }
    }


class TestEndToEnd:
    async def test_full_pipeline_resolves_vars(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("X-Api-Key") == "k"
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(200, json=_ws_detail())
            if path.endswith("/environments/e1"):
                return httpx.Response(200, json=_env())
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=_collection_basic())
            return httpx.Response(404, content=str(request.url).encode())

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # 1 request + 1 environment.
                assert len(refs) == 2
                req_ref = next(r for r in refs if r.metadata["kind"] == "request")
                env_ref = next(r for r in refs if r.metadata["kind"] == "environment")
                docs = [d async for d in c.fetch(req_ref)]
                assert len(docs) == 1
                assert isinstance(docs[0], Document)
                # Env var resolved
                assert "Bearer AKIA12345" in docs[0].text
                # URL var resolved
                assert "https://api.example.com/users" in docs[0].text
                # Disabled header skipped
                assert "X-Skipped" not in docs[0].text
                # Auth bearer
                assert "auth.token=AKIA12345" in docs[0].text
                # Body
                assert "body.raw" in docs[0].text
                # Collection-scoped var beats env (none with same key, so just verify present)
                assert "coll-only" in docs[0].text
                # Pre-request script
                assert "script.prerequest=console.log('AKIA12345')" in docs[0].text
                # Response example
                assert "example.200-ok=Bearer real-AKIA12345" in docs[0].text
                env_docs = [d async for d in c.fetch(env_ref)]
                assert "api_key=AKIA12345" in env_docs[0].text
                # Disabled env var skipped (key=value form)
                assert "disabled_one=skip" not in env_docs[0].text
            finally:
                await c.close()

    async def test_explicit_workspaces_skip_global_list(self) -> None:
        called_global = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                called_global["hit"] = True
                return httpx.Response(500)
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "w1",
                            "collections": [],
                            "environments": [],
                        }
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(
                PostmanConfig(api_key="k", workspaces=("w1",)), client=client
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert not called_global["hit"]
            finally:
                await c.close()

    async def test_include_examples_off(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(200, json=_ws_detail())
            if path.endswith("/environments/e1"):
                return httpx.Response(200, json=_env())
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=_collection_basic())
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(
                PostmanConfig(api_key="k", include_examples=False),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                req_ref = next(r for r in refs if r.metadata["kind"] == "request")
                docs = [d async for d in c.fetch(req_ref)]
                assert "example.200-ok" not in docs[0].text
            finally:
                await c.close()

    async def test_include_environments_off(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(200, json=_ws_detail())
            if path.endswith("/environments/e1"):
                return httpx.Response(200, json=_env())
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json={"collection": {"info": {"name": "C"}, "item": []}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(
                PostmanConfig(api_key="k", include_environments=False),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # Empty collection → 0 request refs; env disabled → 0 env refs
                assert refs == []
            finally:
                await c.close()


# --- nested folders + edge cases ----------------------------------


class TestEdgeCases:
    async def test_nested_folder_recursion(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {
                        "name": "folder1",
                        "item": [
                            {
                                "name": "folder2",
                                "item": [
                                    {
                                        "name": "deep-req",
                                        "request": {
                                            "method": "GET",
                                            "url": "https://example.com",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                paths = {r.path for r in refs}
                assert any("deep-req" in p for p in paths)
            finally:
                await c.close()

    async def test_url_only_string_form(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {"name": "r1", "request": "https://example.com/{{path}}"}
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                # Unresolved var stays as-is (no env)
                assert "{{path}}" in docs[0].text
            finally:
                await c.close()

    async def test_url_object_without_raw_composes(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {
                        "name": "r1",
                        "request": {
                            "method": "POST",
                            "url": {
                                "protocol": "https",
                                "host": ["example", "com"],
                                "path": ["api", "v1"],
                            },
                        },
                    }
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert "https://example.com/api/v1" in docs[0].text
            finally:
                await c.close()

    async def test_body_form_modes(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {
                        "name": "r-form",
                        "request": {
                            "method": "POST",
                            "url": "https://example.com",
                            "body": {
                                "mode": "urlencoded",
                                "urlencoded": [
                                    {"key": "user", "value": "alice"},
                                    {"key": "skip", "value": "x", "disabled": True},
                                ],
                            },
                        },
                    },
                    {
                        "name": "r-multipart",
                        "request": {
                            "method": "POST",
                            "url": "https://example.com",
                            "body": {
                                "mode": "formdata",
                                "formdata": [{"key": "f", "value": "v"}],
                            },
                        },
                    },
                    {
                        "name": "r-file",
                        "request": {
                            "method": "POST",
                            "url": "https://example.com",
                            "body": {
                                "mode": "file",
                                "file": {"src": "/tmp/upload.bin"},
                            },
                        },
                    },
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                texts = []
                for ref in refs:
                    docs = [d async for d in c.fetch(ref)]
                    texts.append(docs[0].text)
                assert any("body.urlencoded.user=alice" in t for t in texts)
                assert all("body.urlencoded.skip" not in t for t in texts)
                assert any("body.formdata.f=v" in t for t in texts)
                assert any("body.file=/tmp/upload.bin" in t for t in texts)
            finally:
                await c.close()

    async def test_event_script_string_form(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {
                        "name": "r1",
                        "request": {"method": "GET", "url": "https://x"},
                        "event": [
                            {"listen": "test", "script": {"exec": "single-line"}}
                        ],
                    }
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert "script.test=single-line" in docs[0].text
            finally:
                await c.close()

    async def test_request_with_no_request_field_skipped(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {"name": "header-only"},
                    {"name": "r1", "request": "https://example.com"},
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
            finally:
                await c.close()


# --- filter -------------------------------------------------------


class TestFilter:
    async def test_include_exclude_filters_request_paths(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {"name": "users", "request": "https://example.com/users"},
                    {"name": "internal", "request": "https://example.com/i"},
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("*/users",)), None
                    )
                ]
                assert all(r.metadata["request_name"] == "users" for r in refs)
            finally:
                await c.close()
        async with _client(handler) as client2:
            c2 = PostmanConnector(PostmanConfig(api_key="k"), client=client2)
            try:
                refs2 = [
                    r
                    async for r in c2.discover(
                        SourceFilter(exclude=("*/internal",)), None
                    )
                ]
                assert all(r.metadata["request_name"] == "users" for r in refs2)
            finally:
                await c2.close()

    async def test_environment_path_filter(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(200, json=_ws_detail())
            if path.endswith("/environments/e1"):
                return httpx.Response(200, json=_env())
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json={"collection": {"info": {"name": "C"}, "item": []}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("team/__env__/*",)), None
                    )
                ]
                assert refs == []
            finally:
                await c.close()


# --- interlock ----------------------------------------------------


class TestInterlock:
    async def test_pattern_redacts_in_text(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {
                        "name": "r",
                        "request": "https://example.com?token=AKIAEXPOSED",
                    }
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(
                PostmanConfig(
                    api_key="k", interlock_patterns=("AKIA[A-Z0-9]+",)
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert "AKIAEXPOSED" not in docs[0].text
                assert "[REDACTED-INTERLOCK]" in docs[0].text
            finally:
                await c.close()


# --- depth defense ------------------------------------------------


class TestDepthDefense:
    async def test_deeply_nested_truncated(self) -> None:
        # Build a 105-deep chain — exceeds the 100-deep cap.
        deepest = {"name": "deep", "request": "https://example.com"}
        node: dict = deepest
        for i in range(105):
            node = {"name": f"f{i}", "item": [node]}

        coll = {
            "collection": {"info": {"name": "C"}, "item": [node]}
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                # Should not crash; deepest node truncated by depth cap.
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # Cap at 100 means deep is unreachable — no refs.
                assert refs == []
            finally:
                await c.close()


# --- fetch edges --------------------------------------------------


class TestFetchEdges:
    async def test_fetch_unknown_path_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(lambda _r: httpx.Response(404)) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()


# --- malformed shapes ---------------------------------------------


class TestMalformedShapes:
    """Wire data is untrusted — shapes that are not Mapping/list must
    be tolerated rather than crashing the scan."""

    async def test_non_mapping_event_and_response_skipped(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    {
                        "name": "r",
                        "request": {
                            "method": "GET",
                            "url": "https://example.com",
                        },
                        "event": ["not-a-mapping"],
                        "response": ["not-a-mapping"],
                    }
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                # Did not crash; event/response noise dropped.
                assert "script." not in docs[0].text
                assert "example.unnamed" not in docs[0].text
            finally:
                await c.close()

    async def test_url_garbage_types_safe(self) -> None:
        coll = {
            "collection": {
                "info": {"name": "C"},
                "item": [
                    # url is None → ""
                    {
                        "name": "r-none",
                        "request": {"method": "GET", "url": None},
                    },
                    # url is int → ""
                    {
                        "name": "r-int",
                        "request": {"method": "GET", "url": 42},
                    },
                    # url object with int host/path → coerced to ""
                    {
                        "name": "r-obj",
                        "request": {
                            "method": "GET",
                            "url": {"host": 5, "path": 9},
                        },
                    },
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(
                    200,
                    json={
                        "workspace": {
                            "id": "w1",
                            "name": "team",
                            "collections": [{"id": "c1"}],
                            "environments": [],
                        }
                    },
                )
            if path.endswith("/collections/c1"):
                return httpx.Response(200, json=coll)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # Three requests rendered without crashing.
                assert len(refs) == 3
                for ref in refs:
                    docs = [d async for d in c.fetch(ref)]
                    assert isinstance(docs[0], Document)
            finally:
                await c.close()


# --- env include path --------------------------------------------


class TestEnvIncludeFilter:
    async def test_environment_serialisation_skips_garbage_values(self) -> None:
        env = {
            "environment": {
                "id": "e1",
                "name": "prod",
                "values": [
                    "not-a-mapping",
                    {"value": "no-key-here"},
                    {"key": "good", "value": "v"},
                ],
            }
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(200, json=_ws_detail())
            if path.endswith("/environments/e1"):
                return httpx.Response(200, json=env)
            if path.endswith("/collections/c1"):
                return httpx.Response(
                    200, json={"collection": {"info": {"name": "C"}, "item": []}}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                env_ref = next(r for r in refs if r.metadata["kind"] == "environment")
                docs = [d async for d in c.fetch(env_ref)]
                assert "good=v" in docs[0].text
                assert "no-key-here" not in docs[0].text
            finally:
                await c.close()

    async def test_environment_include_keeps_matching(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/workspaces"):
                return httpx.Response(
                    200, json={"workspaces": [{"id": "w1", "name": "team"}]}
                )
            if path.endswith("/workspaces/w1"):
                return httpx.Response(200, json=_ws_detail())
            if path.endswith("/environments/e1"):
                return httpx.Response(200, json=_env())
            if path.endswith("/collections/c1"):
                return httpx.Response(
                    200, json={"collection": {"info": {"name": "C"}, "item": []}}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
            try:
                # Include pattern that does NOT match → env dropped
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("nope/*",)), None
                    )
                ]
                assert refs == []
            finally:
                await c.close()


# --- spec / factory -----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "postman"
        assert SPEC.version == "0.1.0"

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("postman", {"api_key": "k"})
        assert isinstance(c, PostmanConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "postman",
            {
                "api_key": "k",
                "workspaces": ["w1"],
                "include_environments": False,
                "include_examples": False,
                "interlock_patterns": ["secret-.*"],
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_api_key(self) -> None:
        with pytest.raises(ValueError, match="api_key"):
            SPEC.factory({})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = PostmanConnector(PostmanConfig(api_key="k"))
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = PostmanConnector(PostmanConfig(api_key="k"), client=client)
        await c.close()
        assert not client.is_closed
        await client.aclose()
