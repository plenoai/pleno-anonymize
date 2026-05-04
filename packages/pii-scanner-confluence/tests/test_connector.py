"""Tests for ConfluenceConnector — uses httpx.MockTransport doubles."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable

import httpx
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
from pleno_pii_scanner_confluence import (
    SPEC,
    ConfluenceConfig,
    ConfluenceConnector,
)
from pleno_pii_scanner_confluence.connector import (
    _decode_cursor,
    _encode_cursor,
    _next_cursor_from_links,
    _parse_iso,
    _xhtml_to_text,
)


_BASE = "https://acme.atlassian.net"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=_BASE,
        transport=httpx.MockTransport(handler),
    )


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            ConfluenceConfig(base_url="", email="e", api_token="t")

    def test_rejects_empty_api_token(self) -> None:
        with pytest.raises(ValueError, match="api_token"):
            ConfluenceConfig(base_url=_BASE, email="e", api_token="")

    def test_rejects_bad_deployment(self) -> None:
        with pytest.raises(ValueError, match="deployment"):
            ConfluenceConfig(
                base_url=_BASE,
                email="e",
                api_token="t",
                deployment="onprem",  # type: ignore[arg-type]
            )

    def test_explicit_id(self) -> None:
        cfg = ConfluenceConfig(
            base_url=_BASE, email="e", api_token="t", id="x"
        )
        assert cfg.resolved_id() == "x"

    def test_default_id_no_token_leak(self) -> None:
        cfg = ConfluenceConfig(
            base_url=_BASE,
            email="e",
            api_token="VERYSECRET",
            spaces=("A", "B"),
        )
        rid = cfg.resolved_id()
        assert "VERYSECRET" not in rid
        assert rid.startswith("confluence:")

    def test_default_id_order_independent(self) -> None:
        a = ConfluenceConfig(
            base_url=_BASE, email="e", api_token="t", spaces=("A", "B")
        )
        b = ConfluenceConfig(
            base_url=_BASE, email="e", api_token="t", spaces=("B", "A")
        )
        assert a.resolved_id() == b.resolved_id()


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(base_url=_BASE, email="e", api_token="t")
        )
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(base_url=_BASE, email="e", api_token="t")
        )
        assert c.capabilities() == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- auth ---------------------------------------------------------


class TestAuth:
    async def test_cloud_basic_auth_header(self) -> None:
        seen_auth: dict[str, str | None] = {"value": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth["value"] = request.headers.get("authorization")
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(200, json={"results": [], "_links": {}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="ops@acme.example",
                    api_token="cloud-token",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        # HTTP basic = "Basic base64(email:token)"
        expected = base64.b64encode(b"ops@acme.example:cloud-token").decode()
        assert seen_auth["value"] == f"Basic {expected}"

    async def test_dc_bearer_header(self) -> None:
        seen_auth: dict[str, str | None] = {"value": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth["value"] = request.headers.get("authorization")
            if request.url.path.endswith("/rest/api/content"):
                return httpx.Response(
                    200, json={"results": [], "size": 0, "limit": 100}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="ignored",
                    api_token="dc-pat",
                    deployment="dc",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen_auth["value"] == "Bearer dc-pat"


# --- pagination + discover ---------------------------------------


def _cloud_page(
    page_id: str,
    *,
    space_id: str = "100",
    title: str = "Page",
    body: str = "<p>hi</p>",
    version_number: int = 1,
    created_at: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "id": page_id,
        "title": title,
        "spaceId": space_id,
        "body": {"storage": {"value": body, "representation": "storage"}},
        "version": {"number": version_number, "createdAt": created_at},
    }


class TestCloudPagination:
    async def test_cursor_paginates_and_yields_refs(self) -> None:
        page_one = _cloud_page("1", title="A", body="<p>alpha</p>")
        page_two = _cloud_page(
            "2", title="B", body="<p>beta</p>", created_at="2026-02-01T00:00:00Z"
        )
        seen_cursors: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                cursor = request.url.params.get("cursor")
                seen_cursors.append(cursor)
                if cursor is None:
                    return httpx.Response(
                        200,
                        json={
                            "results": [page_one],
                            "_links": {
                                "next": "/wiki/api/v2/pages?cursor=ABC&limit=100"
                            },
                        },
                    )
                if cursor == "ABC":
                    return httpx.Response(
                        200,
                        json={"results": [page_two], "_links": {}},
                    )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.metadata["page_id"] for r in refs] == ["1", "2"]
        assert seen_cursors == [None, "ABC"]

    async def test_no_attachments_when_flag_off(self) -> None:
        attempted: dict[str, bool] = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if "/attachments" in request.url.path:
                attempted["hit"] = True
                return httpx.Response(500)
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={"results": [_cloud_page("1")], "_links": {}},
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert attempted["hit"] is False

    async def test_attachments_emitted_with_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={"results": [_cloud_page("1")], "_links": {}},
                )
            if request.url.path.endswith("/wiki/api/v2/pages/1/attachments"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "att-1",
                                "title": "diagram.png",
                                "mediaType": "image/png",
                                "fileSize": 1234,
                            }
                        ],
                        "_links": {},
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        att_refs = [r for r in refs if r.metadata.get("kind") == "attachment"]
        assert len(att_refs) == 1
        assert att_refs[0].content_type == "image/png"
        assert att_refs[0].size == 1234

    async def test_attachments_paginate(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={"results": [_cloud_page("1")], "_links": {}},
                )
            if request.url.path.endswith("/wiki/api/v2/pages/1/attachments"):
                cursor = request.url.params.get("cursor")
                if cursor is None:
                    return httpx.Response(
                        200,
                        json={
                            "results": [
                                {
                                    "id": "a1",
                                    "title": "f1",
                                    "mediaType": "text/plain",
                                }
                            ],
                            "_links": {
                                "next": "/wiki/api/v2/pages/1/attachments?cursor=NEXT"
                            },
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "a2",
                                "title": "f2",
                                "mediaType": "text/plain",
                            }
                        ],
                        "_links": {},
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(base_url=_BASE, email="e", api_token="t"),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        att_titles = [
            r.metadata["title"] for r in refs if r.metadata.get("kind") == "attachment"
        ]
        assert att_titles == ["f1", "f2"]


# --- DC pagination ------------------------------------------------


def _dc_page(
    page_id: str,
    *,
    space_key: str = "ENG",
    title: str = "Page",
    body: str = "<p>dc</p>",
    version_number: int = 1,
    when: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "id": page_id,
        "title": title,
        "space": {"key": space_key},
        "body": {"storage": {"value": body, "representation": "storage"}},
        "version": {"number": version_number, "when": when},
    }


class TestDcPagination:
    async def test_start_limit_walks_and_attachments(self) -> None:
        seen_starts: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content"):
                start = request.url.params.get("start")
                seen_starts.append(start)
                if start in (None, "0"):
                    return httpx.Response(
                        200,
                        json={
                            "results": [_dc_page("1"), _dc_page("2")],
                            "size": 2,
                            "limit": 2,
                        },
                    )
                if start == "2":
                    return httpx.Response(
                        200,
                        json={
                            "results": [_dc_page("3")],
                            "size": 1,
                            "limit": 2,
                        },
                    )
            if request.url.path.endswith("/child/attachment"):
                # Empty page so the inner pagination break runs.
                return httpx.Response(
                    200,
                    json={"results": [], "size": 0, "limit": 100},
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="ignored",
                    api_token="dc",
                    deployment="dc",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        # Limit was 2; first page returned size=2 → start advances to 2.
        # Second page returned size=1 < limit → stop.
        assert [r.metadata["page_id"] for r in refs] == ["1", "2", "3"]
        assert seen_starts == ["0", "2"]

    async def test_dc_attachment_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content"):
                return httpx.Response(
                    200,
                    json={
                        "results": [_dc_page("1")],
                        "size": 1,
                        "limit": 100,
                    },
                )
            if request.url.path.endswith("/rest/api/content/1/child/attachment"):
                start = request.url.params.get("start")
                if start in (None, "0"):
                    return httpx.Response(
                        200,
                        json={
                            "results": [
                                {
                                    "id": "att1",
                                    "title": "secret.csv",
                                    "extensions": {
                                        "mediaType": "text/csv",
                                        "fileSize": 99,
                                    },
                                }
                            ],
                            "size": 1,
                            "limit": 1,
                        },
                    )
                return httpx.Response(
                    200, json={"results": [], "size": 0, "limit": 1}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        att = next(r for r in refs if r.metadata.get("kind") == "attachment")
        assert att.content_type == "text/csv"
        assert att.size == 99
        assert att.path.endswith("/attachments/secret.csv")

    async def test_dc_spaces_allowlist_passes_spacekey(self) -> None:
        seen_keys: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content"):
                seen_keys.append(request.url.params.get("spaceKey"))
                return httpx.Response(
                    200, json={"results": [], "size": 0, "limit": 100}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                    spaces=("ENG", "OPS"),
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert sorted(k for k in seen_keys if k is not None) == ["ENG", "OPS"]


# --- spaces allowlist (cloud) ------------------------------------


class TestCloudSpacesAllowlist:
    async def test_allowlist_resolves_keys_to_ids(self) -> None:
        seen_space_ids: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/spaces"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {"id": "100", "key": "ENG"},
                            {"id": "200", "key": "OPS"},
                            {"id": "300", "key": "OTHER"},
                        ],
                        "_links": {},
                    },
                )
            if request.url.path.endswith("/wiki/api/v2/pages"):
                seen_space_ids.append(request.url.params.get("space-id"))
                return httpx.Response(
                    200, json={"results": [], "_links": {}}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    spaces=("ENG", "OPS"),
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert sorted(s for s in seen_space_ids if s is not None) == ["100", "200"]

    async def test_allowlist_paginates_spaces_lookup(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/spaces"):
                cursor = request.url.params.get("cursor")
                if cursor is None:
                    return httpx.Response(
                        200,
                        json={
                            "results": [{"id": "1", "key": "OTHER"}],
                            "_links": {
                                "next": "/wiki/api/v2/spaces?cursor=NEXT"
                            },
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "results": [{"id": "9", "key": "ENG"}],
                        "_links": {},
                    },
                )
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200, json={"results": [], "_links": {}}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    spaces=("ENG",),
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()


# --- XHTML walker -------------------------------------------------


class TestXhtmlWalker:
    def test_paragraph_heading_list(self) -> None:
        xhtml = (
            "<h1>Title</h1>"
            "<p>First paragraph.</p>"
            "<ul><li>one</li><li>two</li></ul>"
        )
        text = _xhtml_to_text(xhtml)
        assert "Title" in text
        assert "First paragraph." in text
        assert "one" in text and "two" in text
        # Block boundaries → distinct lines.
        lines = text.splitlines()
        assert "Title" in lines
        assert "one" in lines
        assert "two" in lines

    def test_table_renders_cells(self) -> None:
        xhtml = (
            "<table>"
            "<tr><th>k</th><th>v</th></tr>"
            "<tr><td>token</td><td>AKIA12345</td></tr>"
            "</table>"
        )
        text = _xhtml_to_text(xhtml)
        assert "AKIA12345" in text
        assert "token" in text

    def test_structured_macro_emits_name_and_inner_text(self) -> None:
        xhtml = (
            "<p>before</p>"
            '<ac:structured-macro ac:name="code">'
            "<ac:plain-text-body>API_KEY=AKIASECRET</ac:plain-text-body>"
            "</ac:structured-macro>"
            "<p>after</p>"
        )
        text = _xhtml_to_text(xhtml)
        assert "[macro code]" in text
        assert "API_KEY=AKIASECRET" in text
        assert "before" in text
        assert "after" in text

    def test_empty_returns_empty(self) -> None:
        assert _xhtml_to_text("") == ""
        assert _xhtml_to_text("    \n  ") == ""

    def test_malformed_falls_back_to_strip(self) -> None:
        # Unbalanced tag → ParseError → regex fallback strips tags.
        xhtml = "<p>open <strong>but never closed</p>"
        text = _xhtml_to_text(xhtml)
        # Either path keeps the visible payload.
        assert "but never closed" in text

    def test_macro_inner_text_and_tail_preserved(self) -> None:
        # Direct macro text + tail-after-macro both flow into the output
        # (covers _walk macro-text and macro-tail branches).
        xhtml = (
            '<p>head</p>'
            '<ac:structured-macro ac:name="warning">DANGER-INSIDE</ac:structured-macro>'
            'tail-after-macro'
            '<p>foot</p>'
        )
        text = _xhtml_to_text(xhtml)
        assert "[macro warning]" in text
        assert "DANGER-INSIDE" in text
        assert "tail-after-macro" in text

    def test_inline_tail_after_block(self) -> None:
        # `<strong>` is inline; its tail (text after the closing tag but
        # before the next element) must survive the walker.
        xhtml = "<p>before <strong>BOLD</strong>after-tail</p>"
        text = _xhtml_to_text(xhtml)
        assert "BOLD" in text
        assert "after-tail" in text


# --- filter -------------------------------------------------------


class TestFilter:
    async def test_include_exclude_on_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            _cloud_page("1", space_id="ENG"),
                            _cloud_page("2", space_id="OPS"),
                        ],
                        "_links": {},
                    },
                )
            if "/attachments" in request.url.path:
                return httpx.Response(200, json={"results": [], "_links": {}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("ENG/*",)), None
                    )
                ]
            finally:
                await c.close()
        assert [r.metadata["page_id"] for r in refs] == ["1"]

        async with _client(handler) as client2:
            c2 = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client2,
            )
            try:
                refs2 = [
                    r
                    async for r in c2.discover(
                        SourceFilter(exclude=("OPS/*",)), None
                    )
                ]
            finally:
                await c2.close()
        assert [r.metadata["page_id"] for r in refs2] == ["1"]

    async def test_attachment_path_filter(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={"results": [_cloud_page("1")], "_links": {}},
                )
            if request.url.path.endswith("/wiki/api/v2/pages/1/attachments"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "a1",
                                "title": "skip-me",
                                "mediaType": "text/plain",
                            },
                            {
                                "id": "a2",
                                "title": "keep-me",
                                "mediaType": "text/plain",
                            },
                        ],
                        "_links": {},
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(base_url=_BASE, email="e", api_token="t"),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("*/skip-me",)), None
                    )
                ]
            finally:
                await c.close()
        attach_titles = [
            r.metadata["title"] for r in refs if r.metadata.get("kind") == "attachment"
        ]
        assert attach_titles == ["keep-me"]

    async def test_dc_page_filter_include_exclude_and_threshold(self) -> None:
        old = _dc_page("1", when="2025-01-01T00:00:00Z")
        keep = _dc_page("2", when="2026-01-01T00:00:00Z")
        other = _dc_page("3", space_key="OPS", when="2026-02-01T00:00:00Z")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content"):
                return httpx.Response(
                    200,
                    json={
                        "results": [old, keep, other],
                        "size": 3,
                        "limit": 100,
                    },
                )
            if request.url.path.endswith("/child/attachment"):
                return httpx.Response(
                    200, json={"results": [], "size": 0, "limit": 100}
                )
            return httpx.Response(404)

        # include filter drops space "OPS" (line 423)
        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                    include_attachments_meta=False,
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("ENG/*",)), None
                    )
                ]
            finally:
                await c.close()
        assert {r.metadata["page_id"] for r in refs} == {"1", "2"}

        # exclude filter drops the "OPS" space (line 425)
        async with _client(handler) as client2:
            c2 = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                    include_attachments_meta=False,
                ),
                client=client2,
            )
            try:
                refs2 = [
                    r
                    async for r in c2.discover(
                        SourceFilter(exclude=("OPS/*",)), None
                    )
                ]
            finally:
                await c2.close()
        assert {r.metadata["page_id"] for r in refs2} == {"1", "2"}

        # threshold cursor drops the 2025 page (line 430)
        cursor = _encode_cursor({"last_modified": "2025-12-31T23:59:59Z"})
        async with _client(handler) as client3:
            c3 = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                    include_attachments_meta=False,
                ),
                client=client3,
            )
            try:
                refs3 = [r async for r in c3.discover(SourceFilter(), cursor)]
            finally:
                await c3.close()
        assert {r.metadata["page_id"] for r in refs3} == {"2", "3"}

    async def test_dc_attachment_exclude_skips(self) -> None:
        # Hit the exclude branch in _dc_attachments (line 476).
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content"):
                return httpx.Response(
                    200,
                    json={
                        "results": [_dc_page("1")],
                        "size": 1,
                        "limit": 100,
                    },
                )
            if request.url.path.endswith("/rest/api/content/1/child/attachment"):
                start = request.url.params.get("start")
                if start in (None, "0"):
                    return httpx.Response(
                        200,
                        json={
                            "results": [
                                {
                                    "id": "a1",
                                    "title": "skip-me",
                                    "extensions": {"mediaType": "text/csv"},
                                },
                                {
                                    "id": "a2",
                                    "title": "keep",
                                    "extensions": {"mediaType": "text/csv"},
                                },
                            ],
                            "size": 2,
                            # size == limit → second-page lookup taken.
                            "limit": 2,
                        },
                    )
                return httpx.Response(
                    200, json={"results": [], "size": 0, "limit": 2}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("*/skip-me",)), None
                    )
                ]
            finally:
                await c.close()
        att_titles = [
            r.metadata["title"]
            for r in refs
            if r.metadata.get("kind") == "attachment"
        ]
        assert att_titles == ["keep"]

    async def test_dc_attachment_loop_breaks_on_short_page(self) -> None:
        # Single-call response with size < limit hits the early-break
        # path at line 502 instead of line 469's empty-page break.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content"):
                return httpx.Response(
                    200,
                    json={
                        "results": [_dc_page("1")],
                        "size": 1,
                        "limit": 100,
                    },
                )
            if request.url.path.endswith("/rest/api/content/1/child/attachment"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "a1",
                                "title": "single",
                                "extensions": {"mediaType": "text/csv"},
                            }
                        ],
                        "size": 1,
                        "limit": 100,
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert any(r.metadata.get("title") == "single" for r in refs)

    async def test_cloud_attachment_include_filter_skips(self) -> None:
        # Hit the include-skip branch in _cloud_attachments (line 299).
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={"results": [_cloud_page("1")], "_links": {}},
                )
            if request.url.path.endswith("/wiki/api/v2/pages/1/attachments"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "a1",
                                "title": "skip-me",
                                "mediaType": "text/plain",
                            },
                            {
                                "id": "a2",
                                "title": "keep",
                                "mediaType": "text/plain",
                            },
                        ],
                        "_links": {},
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(base_url=_BASE, email="e", api_token="t"),
                client=client,
            )
            try:
                # include matches the page + only the "keep" attachment;
                # "skip-me" is dropped by the include miss.
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("100/1", "100/1/attachments/keep")),
                        None,
                    )
                ]
            finally:
                await c.close()
        att_titles = [
            r.metadata["title"] for r in refs if r.metadata.get("kind") == "attachment"
        ]
        assert att_titles == ["keep"]

    async def test_dc_attachment_filter_include(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/rest/api/content"):
                return httpx.Response(
                    200,
                    json={
                        "results": [_dc_page("1")],
                        "size": 1,
                        "limit": 100,
                    },
                )
            if request.url.path.endswith("/rest/api/content/1/child/attachment"):
                start = request.url.params.get("start")
                if start in (None, "0"):
                    return httpx.Response(
                        200,
                        json={
                            "results": [
                                {
                                    "id": "a1",
                                    "title": "secret.csv",
                                    "extensions": {"mediaType": "text/csv"},
                                },
                                {
                                    "id": "a2",
                                    "title": "diagram.png",
                                    "extensions": {"mediaType": "image/png"},
                                },
                            ],
                            "size": 2,
                            "limit": 2,
                        },
                    )
                return httpx.Response(
                    200, json={"results": [], "size": 0, "limit": 2}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="x",
                    api_token="dc",
                    deployment="dc",
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("ENG/1", "ENG/1/attachments/secret.csv")),
                        None,
                    )
                ]
            finally:
                await c.close()
        kinds = [r.metadata.get("kind") for r in refs]
        titles = [
            r.metadata.get("title")
            for r in refs
            if r.metadata.get("kind") == "attachment"
        ]
        assert "page" in kinds
        assert titles == ["secret.csv"]


# --- cursor incremental ------------------------------------------


class TestCursorResume:
    async def test_resume_skips_older_pages(self) -> None:
        old = _cloud_page(
            "1",
            title="old",
            body="<p>old</p>",
            created_at="2025-01-01T00:00:00Z",
        )
        new = _cloud_page(
            "2",
            title="new",
            body="<p>new</p>",
            created_at="2026-06-01T00:00:00Z",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200, json={"results": [old, new], "_links": {}}
                )
            if "/attachments" in request.url.path:
                return httpx.Response(200, json={"results": [], "_links": {}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client,
            )
            try:
                first_refs = [r async for r in c.discover(SourceFilter(), None)]
                cursor = c.cursor_after_run()
            finally:
                await c.close()
        assert {r.metadata["page_id"] for r in first_refs} == {"1", "2"}
        assert cursor is not None
        # Round-trip cursor → second pass returns no pages (both seen).
        async with _client(handler) as client2:
            c2 = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client2,
            )
            try:
                second_refs = [r async for r in c2.discover(SourceFilter(), cursor)]
            finally:
                await c2.close()
        assert second_refs == []

    async def test_cursor_after_run_none_when_no_pages(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200, json={"results": [], "_links": {}}
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert c.cursor_after_run() is None
            finally:
                await c.close()


# --- fetch + close ------------------------------------------------


class TestFetchAndClose:
    async def test_fetch_returns_text_for_page(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            _cloud_page(
                                "1",
                                body="<p>Hello <strong>world</strong></p>",
                            )
                        ],
                        "_links": {},
                    },
                )
            if "/attachments" in request.url.path:
                return httpx.Response(200, json={"results": [], "_links": {}})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(
                    base_url=_BASE,
                    email="e",
                    api_token="t",
                    include_attachments_meta=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert len(docs) == 1
                assert isinstance(docs[0], Document)
                assert "Hello" in docs[0].text
                assert "world" in docs[0].text
            finally:
                await c.close()

    async def test_fetch_attachment_yields_nothing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/wiki/api/v2/pages"):
                return httpx.Response(
                    200,
                    json={"results": [_cloud_page("1")], "_links": {}},
                )
            if request.url.path.endswith("/wiki/api/v2/pages/1/attachments"):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": "a1",
                                "title": "f",
                                "mediaType": "text/plain",
                            }
                        ],
                        "_links": {},
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(base_url=_BASE, email="e", api_token="t"),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                att = next(r for r in refs if r.metadata.get("kind") == "attachment")
                docs = [d async for d in c.fetch(att)]
                assert docs == []
            finally:
                await c.close()

    async def test_fetch_unknown_path_yields_nothing(self) -> None:
        async with _client(lambda _r: httpx.Response(404)) as client:
            c = ConfluenceConnector(
                ConfluenceConfig(base_url=_BASE, email="e", api_token="t"),
                client=client,
            )
            try:
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="UNKNOWN/123",
                )
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()

    async def test_close_owns_client(self) -> None:
        c = ConfluenceConnector(
            ConfluenceConfig(base_url=_BASE, email="e", api_token="t")
        )
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient(base_url=_BASE)
        c = ConfluenceConnector(
            ConfluenceConfig(base_url=_BASE, email="e", api_token="t"),
            client=client,
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()


# --- spec / factory -----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "confluence"
        assert SPEC.version == "0.1.0"
        assert SPEC.required_scopes == ("confluence:read",)
        assert SPEC.capabilities.incremental is True
        assert SPEC.capabilities.max_concurrent_fetches == 4

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create(
            "confluence",
            {"base_url": _BASE, "email": "e", "api_token": "t"},
        )
        assert isinstance(c, ConfluenceConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "confluence",
            {
                "base_url": _BASE,
                "email": "e",
                "api_token": "t",
                "spaces": ["ENG"],
                "include_attachments_meta": False,
                "deployment": "dc",
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            SPEC.factory({"email": "e", "api_token": "t"})

    def test_factory_rejects_missing_email(self) -> None:
        with pytest.raises(ValueError, match="email"):
            SPEC.factory({"base_url": _BASE, "api_token": "t"})

    def test_factory_rejects_missing_token(self) -> None:
        with pytest.raises(ValueError, match="api_token"):
            SPEC.factory({"base_url": _BASE, "email": "e"})


# --- helpers (cursor + ISO + link parser) -------------------------


class TestHelpers:
    def test_cursor_round_trip(self) -> None:
        encoded = _encode_cursor({"last_modified": "2026-01-01T00:00:00Z"})
        decoded = _decode_cursor(encoded)
        assert decoded == {"last_modified": "2026-01-01T00:00:00Z"}

    def test_cursor_decode_empty_and_garbage(self) -> None:
        assert _decode_cursor(None) == {}
        assert _decode_cursor("") == {}
        assert _decode_cursor("not-json") == {}
        assert _decode_cursor(json.dumps([1, 2, 3])) == {}

    def test_parse_iso_z_suffix(self) -> None:
        ts = _parse_iso("2026-01-01T00:00:00Z")
        assert ts is not None
        assert ts.year == 2026
        assert _parse_iso("garbage") is None
        assert _parse_iso("") is None

    def test_next_cursor_from_links_extracts_value(self) -> None:
        assert (
            _next_cursor_from_links(
                {"next": "/wiki/api/v2/pages?cursor=ABC&limit=100"}
            )
            == "ABC"
        )
        assert (
            _next_cursor_from_links({"next": "/foo?cursor=END"}) == "END"
        )
        assert _next_cursor_from_links({"next": "/foo?other=x"}) is None
        assert _next_cursor_from_links({}) is None
        assert _next_cursor_from_links({"next": ""}) is None
        # Non-mapping → None
        assert _next_cursor_from_links([]) is None  # type: ignore[arg-type]
