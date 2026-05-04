"""Tests for SharePointConnector — uses httpx.MockTransport doubles."""

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
from pleno_pii_scanner_sharepoint import (
    SPEC,
    SharePointConfig,
    SharePointConnector,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://graph.microsoft.com",
        transport=httpx.MockTransport(handler),
    )


def _ok_token(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": "AAA", "expires_in": 3600}
    )


def _make_handler(
    *,
    sites: list[dict] | None = None,
    drives_by_site: dict[str, list[dict]] | None = None,
    delta_pages: dict[str, list[dict]] | None = None,
    lists_by_site: dict[str, list[dict]] | None = None,
    list_items: dict[str, list[dict]] | None = None,
    list_item_detail: dict[str, dict] | None = None,
    file_content: dict[str, bytes] | None = None,
    token_calls: list[dict] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    sites = sites or []
    drives_by_site = drives_by_site or {}
    delta_pages = delta_pages or {}
    lists_by_site = lists_by_site or {}
    list_items = list_items or {}
    list_item_detail = list_item_detail or {}
    file_content = file_content or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if "login.microsoftonline.com" in url and path.endswith("/oauth2/v2.0/token"):
            if token_calls is not None:
                # Capture the form payload for assertion in the test.
                from urllib.parse import parse_qs

                body = request.content.decode("utf-8")
                token_calls.append(
                    {k: v[0] for k, v in parse_qs(body).items()}
                )
            return httpx.Response(
                200, json={"access_token": "AAA", "expires_in": 3600}
            )
        if path == "/v1.0/sites" and request.url.params.get("search") == "*":
            return httpx.Response(200, json={"value": sites})
        if path.startswith("/v1.0/sites/") and path.endswith("/drives"):
            site_id = path.split("/")[3]
            return httpx.Response(
                200, json={"value": drives_by_site.get(site_id, [])}
            )
        if "/root/delta" in path or url in delta_pages:
            # Resume URLs are absolute; initial path is relative.
            page_key = url if url in delta_pages else path
            for key in (page_key, path, url):
                if key in delta_pages:
                    pages = delta_pages[key]
                    return httpx.Response(200, json=pages.pop(0))
            return httpx.Response(200, json={"value": []})
        if path.startswith("/v1.0/sites/") and path.endswith("/lists"):
            site_id = path.split("/")[3]
            return httpx.Response(
                200, json={"value": lists_by_site.get(site_id, [])}
            )
        if (
            path.startswith("/v1.0/sites/")
            and "/lists/" in path
            and path.endswith("/items")
        ):
            list_id = path.split("/")[5]
            return httpx.Response(
                200, json={"value": list_items.get(list_id, [])}
            )
        if (
            path.startswith("/v1.0/sites/")
            and "/lists/" in path
            and "/items/" in path
        ):
            item_id = path.split("/items/")[1]
            return httpx.Response(
                200, json=list_item_detail.get(item_id, {"fields": {}})
            )
        if "/items/" in path and path.endswith("/content"):
            item_id = path.split("/items/")[1].split("/")[0]
            blob = file_content.get(item_id, b"")
            return httpx.Response(200, content=blob)
        if path.startswith("/v1.0/sites/") and path.count("/") == 3:
            # /v1.0/sites/{path-form} resolved site lookup.
            return httpx.Response(
                200, json={"id": "resolved-site", "name": "resolved"}
            )
        return httpx.Response(404, content=str(request.url).encode())

    return handler


# --- config -------------------------------------------------------


class TestConfig:
    def test_rejects_empty_tenant_id(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            SharePointConfig(
                tenant_id="", client_id="c", client_secret="s"
            )

    def test_rejects_empty_client_id(self) -> None:
        with pytest.raises(ValueError, match="client_id"):
            SharePointConfig(
                tenant_id="t", client_id="", client_secret="s"
            )

    def test_rejects_neither_credential(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            SharePointConfig(tenant_id="t", client_id="c")

    def test_rejects_both_credentials(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            SharePointConfig(
                tenant_id="t",
                client_id="c",
                client_secret="s",
                federated_token="j",
            )

    def test_rejects_negative_max_size(self) -> None:
        with pytest.raises(ValueError, match="max_file_size_bytes"):
            SharePointConfig(
                tenant_id="t",
                client_id="c",
                client_secret="s",
                max_file_size_bytes=-1,
            )

    def test_explicit_id(self) -> None:
        cfg = SharePointConfig(
            tenant_id="t", client_id="c", client_secret="s", id="custom"
        )
        assert cfg.resolved_id() == "custom"

    def test_default_id_includes_tenant_and_client(self) -> None:
        cfg = SharePointConfig(
            tenant_id="t", client_id="c", client_secret="s"
        )
        rid = cfg.resolved_id()
        assert "t" in rid and "c" in rid

    def test_default_id_with_sites(self) -> None:
        cfg = SharePointConfig(
            tenant_id="t",
            client_id="c",
            client_secret="s",
            sites=("s1", "s2"),
        )
        assert "s1+s2" in cfg.resolved_id()


# --- protocol -----------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = SharePointConnector(
            SharePointConfig(
                tenant_id="t", client_id="c", client_secret="s"
            )
        )
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = SharePointConnector(
            SharePointConfig(
                tenant_id="t", client_id="c", client_secret="s"
            )
        )
        assert c.capabilities() == Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- token --------------------------------------------------------


class TestToken:
    async def test_acquire_with_client_secret(self) -> None:
        calls: list[dict] = []
        handler = _make_handler(
            sites=[{"id": "site1", "name": "TeamA"}],
            drives_by_site={"site1": []},
            token_calls=calls,
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="tnt",
                    client_id="cli",
                    client_secret="sec",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert calls
                assert calls[0]["client_id"] == "cli"
                assert calls[0]["client_secret"] == "sec"
                assert calls[0]["grant_type"] == "client_credentials"
                assert "scope" in calls[0]
            finally:
                await c.close()

    async def test_token_cache_hit(self) -> None:
        calls: list[dict] = []
        handler = _make_handler(
            sites=[{"id": "site1", "name": "TeamA"}],
            drives_by_site={"site1": []},
            token_calls=calls,
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="tnt",
                    client_id="cli",
                    client_secret="sec",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                first = len(calls)
                _ = [r async for r in c.discover(SourceFilter(), None)]
                # Second discover reuses the cached bearer.
                assert len(calls) == first
            finally:
                await c.close()

    async def test_acquire_with_federated_token(self) -> None:
        calls: list[dict] = []
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": []},
            token_calls=calls,
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="tnt",
                    client_id="cli",
                    federated_token="JWT.payload.sig",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert calls
                assert (
                    calls[0]["client_assertion_type"]
                    == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                )
                assert calls[0]["client_assertion"] == "JWT.payload.sig"
                assert "client_secret" not in calls[0]
            finally:
                await c.close()


# --- delta walk ---------------------------------------------------


def _file_item(item_id: str, name: str, *, size: int = 10, etag: str = '"e"') -> dict:
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "eTag": etag,
        "file": {"mimeType": "text/plain"},
        "parentReference": {"path": "/drive/root:/folder"},
        "webUrl": f"https://contoso.sharepoint.com/{name}",
        "lastModifiedDateTime": "2024-01-01T00:00:00Z",
    }


def _folder_item(item_id: str, name: str) -> dict:
    return {
        "id": item_id,
        "name": name,
        "folder": {"childCount": 1},
        "parentReference": {"path": "/drive/root:"},
    }


class TestDelta:
    async def test_initial_enumerates_files_skips_folders(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "Documents"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [
                            _file_item("i1", "a.txt"),
                            _folder_item("f1", "subdir"),
                            _file_item("i2", "b.txt"),
                        ],
                        "@odata.deltaLink": "https://graph.microsoft.com/delta-token-1",
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert {r.metadata["item_id"] for r in refs} == {"i1", "i2"}
                # All refs are files with eTag attached for content_hash_delta.
                assert all(r.etag == '"e"' for r in refs)
                # Path includes site/drive/parent/name.
                assert any("TeamA/Documents/folder/a.txt" in r.path for r in refs)
                # Cursor stored for resume.
                cur = c.cursor_after_run()
                assert cur is not None
                decoded = json.loads(cur)
                assert decoded["s1/d1"] == "https://graph.microsoft.com/delta-token-1"
            finally:
                await c.close()

    async def test_delta_resume_uses_stored_link(self) -> None:
        delta_pages: dict[str, list[dict]] = {
            "https://graph.microsoft.com/delta-resume": [
                {
                    "value": [_file_item("i3", "c.txt")],
                    "@odata.deltaLink": "https://graph.microsoft.com/delta-token-2",
                }
            ],
        }
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "Documents"}]},
            delta_pages=delta_pages,
        )
        prior = json.dumps({"s1/d1": "https://graph.microsoft.com/delta-resume"})
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), prior)]
                assert [r.metadata["item_id"] for r in refs] == ["i3"]
            finally:
                await c.close()

    async def test_delta_paginates_via_nextlink(self) -> None:
        delta_pages = {
            "/v1.0/sites/s1/drives/d1/root/delta": [
                {
                    "value": [_file_item("i1", "a.txt")],
                    "@odata.nextLink": "https://graph.microsoft.com/page2",
                }
            ],
            "https://graph.microsoft.com/page2": [
                {
                    "value": [_file_item("i2", "b.txt")],
                    "@odata.deltaLink": "https://graph.microsoft.com/end",
                }
            ],
        }
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "Documents"}]},
            delta_pages=delta_pages,
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert {r.metadata["item_id"] for r in refs} == {"i1", "i2"}
            finally:
                await c.close()

    async def test_resume_invalid_cursor_falls_back(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "Documents"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [_file_item("i1", "a.txt")],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                # malformed cursor: not JSON
                refs = [r async for r in c.discover(SourceFilter(), "not-json")]
                assert len(refs) == 1
            finally:
                await c.close()

    async def test_cursor_after_run_returns_none_when_empty(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": []},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert c.cursor_after_run() is None
            finally:
                await c.close()


# --- lists --------------------------------------------------------


class TestLists:
    async def test_include_lists_yields_list_item_refs(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": []},
            lists_by_site={
                "s1": [{"id": "L1", "displayName": "Tasks"}],
            },
            list_items={
                "L1": [
                    {
                        "id": "11",
                        "eTag": '"x"',
                        "lastModifiedDateTime": "2024-01-02T00:00:00Z",
                    },
                    {"id": "12"},
                ]
            },
            list_item_detail={
                "11": {
                    "fields": {
                        "Title": "Buy milk",
                        "AssignedTo": "alice@example.com",
                        "@odata.etag": "skip-me",
                        "_internal": "skip-me",
                    }
                }
            },
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_lists=True,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert all(r.metadata["kind"] == "list_item" for r in refs)
                assert {r.metadata["item_id"] for r in refs} == {"11", "12"}
                # Path includes the site / lists / list-name / item-id form.
                assert any("TeamA/lists/Tasks/11" in r.path for r in refs)
                target = next(r for r in refs if r.metadata["item_id"] == "11")
                docs = [d async for d in c.fetch(target)]
                assert isinstance(docs[0], Document)
                assert "Title=Buy milk" in docs[0].text
                assert "AssignedTo=alice@example.com" in docs[0].text
                # @odata / _ prefixed bookkeeping skipped.
                assert "@odata.etag" not in docs[0].text
                assert "_internal" not in docs[0].text
            finally:
                await c.close()

    async def test_include_lists_default_off(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": []},
            lists_by_site={"s1": [{"id": "L1", "displayName": "T"}]},
            list_items={"L1": [{"id": "1"}]},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
            finally:
                await c.close()


# --- fetch --------------------------------------------------------


class TestFetch:
    async def test_fetch_text_file(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "Docs"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [_file_item("i1", "a.txt", size=4)],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
            file_content={"i1": b"hi!\n"},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert len(docs) == 1
                assert isinstance(docs[0], Document)
                assert docs[0].text == "hi!\n"
                assert docs[0].binary is None
                assert docs[0].content_hash == '"e"'
            finally:
                await c.close()

    async def test_fetch_binary_file(self) -> None:
        item = _file_item("ix", "img.bin", size=4)
        item["file"] = {"mimeType": "application/octet-stream"}
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "Docs"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [item],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
            file_content={"ix": b"\x00\x01\x02\x03"},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert docs[0].binary == b"\x00\x01\x02\x03"
                assert docs[0].text is None
            finally:
                await c.close()

    async def test_fetch_text_falls_back_to_binary_on_decode_failure(self) -> None:
        # text/* MIME but invalid UTF-8 — must not crash.
        item = _file_item("ib", "bad.txt", size=2)
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "D"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [item],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
            file_content={"ib": b"\xff\xfe"},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert docs[0].binary == b"\xff\xfe"
            finally:
                await c.close()

    async def test_max_file_size_skips_fetch(self) -> None:
        # Ref still emitted by discover; fetch yields nothing.
        big = _file_item("big", "huge.txt", size=200)
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "D"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [big],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
            file_content={"big": b"x" * 200},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    max_file_size_bytes=100,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
                docs = [d async for d in c.fetch(refs[0])]
                assert docs == []
            finally:
                await c.close()

    async def test_fetch_unknown_kind_no_op(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(_make_handler()) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()

    async def test_fetch_file_missing_metadata_no_op(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(_make_handler()) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="x",
                    metadata={"kind": "file"},
                )
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()

    async def test_fetch_list_item_missing_metadata_no_op(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(_make_handler()) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="x",
                    metadata={"kind": "list_item"},
                )
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()

    async def test_fetch_list_item_non_mapping_fields(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": []},
            lists_by_site={"s1": [{"id": "L1", "displayName": "T"}]},
            list_items={"L1": [{"id": "1"}]},
            list_item_detail={"1": {"fields": "junk"}},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_lists=True,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                # Non-mapping fields → empty serialised body, but Document
                # still requires text XOR binary; we hit text="".
                assert docs[0].text == ""
            finally:
                await c.close()


# --- sites allowlist ---------------------------------------------


class TestSitesAllowlist:
    async def test_explicit_id_skips_search(self) -> None:
        seen_search = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path
            if "login.microsoftonline" in url:
                return _ok_token(request)
            if path == "/v1.0/sites" and request.url.params.get("search"):
                seen_search["hit"] = True
                return httpx.Response(500)
            if path.startswith("/v1.0/sites/site-pin/drives"):
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    sites=("site-pin",),
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert not seen_search["hit"]
            finally:
                await c.close()

    async def test_path_form_resolves_via_graph(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            path = request.url.path
            if "login.microsoftonline" in url:
                return _ok_token(request)
            if path == "/v1.0/sites/contoso.sharepoint.com:/sites/team":
                return httpx.Response(
                    200, json={"id": "resolved-1", "name": "team"}
                )
            if path.startswith("/v1.0/sites/resolved-1/drives"):
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    sites=("contoso.sharepoint.com:/sites/team",),
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []  # no drives in this fixture
            finally:
                await c.close()


# --- filter -------------------------------------------------------


class TestFilter:
    async def test_include_exclude(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "D"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [
                            _file_item("i1", "keep.txt"),
                            _file_item("i2", "drop.txt"),
                        ],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("*keep*",)), None
                    )
                ]
                assert {r.metadata["name"] for r in refs} == {"keep.txt"}
            finally:
                await c.close()
        async with _client(handler) as client2:
            handler2 = _make_handler(
                sites=[{"id": "s1", "name": "TeamA"}],
                drives_by_site={"s1": [{"id": "d1", "name": "D"}]},
                delta_pages={
                    "/v1.0/sites/s1/drives/d1/root/delta": [
                        {
                            "value": [
                                _file_item("i1", "keep.txt"),
                                _file_item("i2", "drop.txt"),
                            ],
                            "@odata.deltaLink": "https://graph.microsoft.com/end",
                        }
                    ]
                },
            )
            client2 = httpx.AsyncClient(
                base_url="https://graph.microsoft.com",
                transport=httpx.MockTransport(handler2),
            )
            c2 = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client2,
            )
            try:
                refs = [
                    r
                    async for r in c2.discover(
                        SourceFilter(exclude=("*drop*",)), None
                    )
                ]
                assert {r.metadata["name"] for r in refs} == {"keep.txt"}
            finally:
                await c2.close()
                await client2.aclose()

    async def test_list_filter_excludes_items(self) -> None:
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": []},
            lists_by_site={"s1": [{"id": "L1", "displayName": "Tasks"}]},
            list_items={"L1": [{"id": "1"}]},
            list_item_detail={"1": {"fields": {"Title": "x"}}},
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_lists=True,
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("TeamA/lists/*",)), None
                    )
                ]
                assert refs == []
            finally:
                await c.close()


# --- ref edge cases ----------------------------------------------


class TestRefEdges:
    async def test_file_without_parent_path(self) -> None:
        item = _file_item("io", "orphan.txt")
        item["parentReference"] = {}
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "D"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [item],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs[0].path == "TeamA/D/orphan.txt"
            finally:
                await c.close()

    async def test_file_invalid_size_and_dates(self) -> None:
        item = _file_item("inv", "a.txt")
        item["size"] = "not-a-number"
        item["lastModifiedDateTime"] = "garbage"
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "D"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [item],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs[0].size is None
                assert refs[0].last_modified is None
            finally:
                await c.close()

    async def test_uses_ctag_when_etag_missing(self) -> None:
        item = _file_item("ic", "a.txt")
        del item["eTag"]
        item["cTag"] = '"c-only"'
        handler = _make_handler(
            sites=[{"id": "s1", "name": "TeamA"}],
            drives_by_site={"s1": [{"id": "d1", "name": "D"}]},
            delta_pages={
                "/v1.0/sites/s1/drives/d1/root/delta": [
                    {
                        "value": [item],
                        "@odata.deltaLink": "https://graph.microsoft.com/end",
                    }
                ]
            },
        )
        async with _client(handler) as client:
            c = SharePointConnector(
                SharePointConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs[0].etag == '"c-only"'
            finally:
                await c.close()


# --- spec / factory ----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "sharepoint"
        assert SPEC.version == "0.1.0"
        assert "Sites.Selected" in SPEC.required_scopes
        assert "Files.Read.All" in SPEC.required_scopes
        assert SPEC.capabilities.incremental
        assert SPEC.capabilities.binary
        assert SPEC.capabilities.content_hash_delta

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create(
            "sharepoint",
            {
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
            },
        )
        assert isinstance(c, SharePointConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "sharepoint",
            {
                "tenant_id": "t",
                "client_id": "c",
                "federated_token": "JWT",
                "sites": ["s1", "s2"],
                "include_lists": True,
                "max_file_size_bytes": 1024,
                "id": "explicit",
            },
        )
        assert c.id == "explicit"

    def test_factory_rejects_missing_tenant(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            SPEC.factory({"client_id": "c", "client_secret": "s"})

    def test_factory_rejects_missing_client(self) -> None:
        with pytest.raises(ValueError, match="client_id"):
            SPEC.factory({"tenant_id": "t", "client_secret": "s"})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = SharePointConnector(
            SharePointConfig(
                tenant_id="t", client_id="c", client_secret="s"
            )
        )
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = SharePointConnector(
            SharePointConfig(
                tenant_id="t", client_id="c", client_secret="s"
            ),
            client=client,
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()
