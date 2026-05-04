"""Tests for JiraConnector — uses httpx.MockTransport doubles."""

from __future__ import annotations

import base64
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
from pleno_pii_scanner_jira import (
    JiraConfig,
    JiraConnector,
    SPEC,
)
from pleno_pii_scanner_jira.connector import (
    _build_jql,
    adf_to_text,
)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://acme.atlassian.net",
        transport=httpx.MockTransport(handler),
    )


# --- ADF samples --------------------------------------------------


def _adf_paragraph(text: str) -> dict:
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _adf_complex() -> dict:
    return {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Title"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "hello "},
                    {
                        "type": "text",
                        "text": "world",
                        "marks": [{"type": "strong"}],
                    },
                ],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "one"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "two "},
                                    {
                                        "type": "text",
                                        "text": "deep",
                                        "marks": [{"type": "em"}],
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
        ],
    }


def _issue(
    key: str = "SEC-1",
    *,
    project: str = "SEC",
    updated: str = "2026-05-01T10:00:00.000+0000",
    description: dict | None = None,
    summary: str = "secret leaked",
) -> dict:
    return {
        "id": f"id-{key}",
        "key": key,
        "fields": {
            "summary": summary,
            "description": description if description is not None else _adf_paragraph(
                "AKIA12345 was here"
            ),
            "updated": updated,
            "project": {"key": project},
        },
    }


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            JiraConfig(base_url="", email="e", api_token="t")

    def test_rejects_empty_api_token(self) -> None:
        with pytest.raises(ValueError, match="api_token"):
            JiraConfig(base_url="https://x", email="e", api_token="")

    def test_rejects_invalid_deployment(self) -> None:
        with pytest.raises(ValueError, match="deployment"):
            JiraConfig(
                base_url="https://x",
                email="e",
                api_token="t",
                deployment="server",  # type: ignore[arg-type]
            )

    def test_explicit_id(self) -> None:
        cfg = JiraConfig(
            base_url="https://x", email="e", api_token="t", id="custom"
        )
        assert cfg.resolved_id() == "custom"

    def test_default_id_no_token_leak(self) -> None:
        cfg = JiraConfig(
            base_url="https://x",
            email="e",
            api_token="VERYSECRET",
            projects=("a", "b"),
        )
        rid = cfg.resolved_id()
        assert "VERYSECRET" not in rid
        assert rid.startswith("jira:")

    def test_default_id_order_independent(self) -> None:
        a = JiraConfig(
            base_url="https://x", email="e", api_token="t", projects=("a", "b")
        )
        b = JiraConfig(
            base_url="https://x", email="e", api_token="t", projects=("b", "a")
        )
        assert a.resolved_id() == b.resolved_id()


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = JiraConnector(JiraConfig(base_url="https://x", email="e", api_token="t"))
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = JiraConnector(JiraConfig(base_url="https://x", email="e", api_token="t"))
        assert c.capabilities() == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- auth ----------------------------------------------------------


class TestAuth:
    async def test_cloud_basic_auth_header(self) -> None:
        seen = {"auth": ""}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={"issues": []})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="ops@acme.example",
                    api_token="secrettoken",
                    deployment="cloud",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen["auth"].startswith("Basic ")
        decoded = base64.b64decode(seen["auth"].split(" ", 1)[1]).decode()
        assert decoded == "ops@acme.example:secrettoken"

    async def test_dc_bearer_header(self) -> None:
        seen = {"auth": ""}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={"issues": [], "total": 0})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="ignored",
                    api_token="patpat",
                    deployment="dc",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen["auth"] == "Bearer patpat"


# --- ADF walker ----------------------------------------------------


class TestAdfWalker:
    def test_paragraph(self) -> None:
        out = adf_to_text(_adf_paragraph("hello world"))
        assert out == "hello world"

    def test_heading_paragraph_bullet_list(self) -> None:
        out = adf_to_text(_adf_complex())
        # All text segments must surface, in order.
        for token in ("Title", "hello world", "one", "two deep"):
            assert token in out
        # Block boundaries should produce newlines so adjacent
        # paragraphs don't run together.
        assert "Title\n" in out
        assert "hello world\n" in out

    def test_none_input(self) -> None:
        assert adf_to_text(None) == ""

    def test_string_input(self) -> None:
        # Defensive: a bare string at a non-text position is surfaced.
        assert adf_to_text("plain text") == "plain text"

    def test_non_mapping_non_list_scalar(self) -> None:
        # Numbers / booleans drop silently; we don't crash.
        assert adf_to_text(42) == ""

    def test_text_node_with_non_string_text(self) -> None:
        # Defensive: malformed text node should drop silently.
        out = adf_to_text({"type": "text", "text": 42})
        assert out == ""

    def test_block_node_without_content(self) -> None:
        # Empty paragraph: no `content` key, but type is block —
        # exercise the `content is None` + block-newline branch.
        out = adf_to_text({"type": "paragraph"})
        # Newline rstrip-ed away, so the result is empty but the
        # walker did not crash.
        assert out == ""


# --- end-to-end discover ------------------------------------------


class TestDiscover:
    async def test_cloud_pagination_two_pages(self) -> None:
        page1 = {
            "issues": [_issue("SEC-1", updated="2026-05-01T10:00:00.000+0000")],
            "nextPageToken": "tok2",
        }
        page2 = {
            "issues": [_issue("SEC-2", updated="2026-05-02T10:00:00.000+0000")],
            # No nextPageToken — end of stream.
        }

        calls: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/rest/api/3/search" in request.url.path
            params = dict(request.url.params)
            calls.append(params)
            if "nextPageToken" not in params:
                return httpx.Response(200, json=page1)
            assert params["nextPageToken"] == "tok2"
            return httpx.Response(200, json=page2)

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["SEC/SEC-1", "SEC/SEC-2"]
        assert len(calls) == 2

    async def test_dc_pagination_uses_start_at(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            start_at = int(params.get("startAt", "0"))
            if start_at == 0:
                return httpx.Response(
                    200,
                    json={
                        "issues": [_issue("DC-1")],
                        "total": 2,
                        "maxResults": 1,
                        "startAt": 0,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "issues": [_issue("DC-2", project="DC")],
                    "total": 2,
                    "maxResults": 1,
                    "startAt": 1,
                },
            )

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    deployment="dc",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert {r.path for r in refs} == {"SEC/DC-1", "DC/DC-2"}

    async def test_dc_terminates_on_empty_page(self) -> None:
        # No `total` field — connector must stop on empty response.
        seen = {"calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["calls"] += 1
            if seen["calls"] == 1:
                return httpx.Response(200, json={"issues": [_issue("X-1", project="X")]})
            return httpx.Response(200, json={"issues": []})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    deployment="dc",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["X/X-1"]
        assert seen["calls"] == 2

    async def test_projects_allowlist_injects_jql(self) -> None:
        seen_jql: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_jql["jql"] = request.url.params.get("jql", "")
            return httpx.Response(200, json={"issues": []})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    projects=("SEC", "INFRA"),
                    include_comments=False,
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert 'project in ("SEC", "INFRA")' in seen_jql["jql"]
        assert "ORDER BY updated ASC" in seen_jql["jql"]


# --- include_comments ---------------------------------------------


class TestComments:
    async def test_comments_yielded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "comments": [
                            {
                                "id": "10001",
                                "author": {"displayName": "alice"},
                                "body": _adf_paragraph("AKIA-IN-COMMENT"),
                                "updated": "2026-05-03T11:00:00.000+0000",
                            }
                        ],
                        "total": 1,
                    },
                )
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=True,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                paths = [r.path for r in refs]
                assert "SEC/SEC-1" in paths
                assert "SEC/SEC-1/comments/10001" in paths
                comment_ref = next(
                    r for r in refs if r.metadata["kind"] == "comment"
                )
                docs = [d async for d in c.fetch(comment_ref)]
                assert isinstance(docs[0], Document)
                assert "AKIA-IN-COMMENT" in docs[0].text
                assert "alice" in docs[0].text
            finally:
                await c.close()

    async def test_include_comments_false_skips_comment_endpoint(self) -> None:
        seen = {"comment_hit": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                seen["comment_hit"] = True
                return httpx.Response(500)
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert all(r.metadata["kind"] == "issue" for r in refs)
        assert not seen["comment_hit"]

    async def test_comment_pagination(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                start_at = int(request.url.params.get("startAt", "0"))
                if start_at == 0:
                    return httpx.Response(
                        200,
                        json={
                            "comments": [
                                {"id": "1", "body": _adf_paragraph("a")}
                            ],
                            "total": 2,
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "comments": [
                            {"id": "2", "body": _adf_paragraph("b")}
                        ],
                        "total": 2,
                    },
                )
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=True,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        comment_paths = {
            r.path for r in refs if r.metadata["kind"] == "comment"
        }
        assert comment_paths == {
            "SEC/SEC-1/comments/1",
            "SEC/SEC-1/comments/2",
        }

    async def test_comment_terminates_on_empty_page(self) -> None:
        # No `total` field — empty page must stop the loop.
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=True,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert all(r.metadata["kind"] == "issue" for r in refs)


# --- cursor --------------------------------------------------------


class TestCursor:
    async def test_cursor_round_trip(self) -> None:
        # First scan: returns one issue at t1, advancing the high-water mark.
        first_issues = [
            _issue("SEC-1", updated="2026-05-01T10:00:00.000+0000"),
            _issue("SEC-2", updated="2026-05-02T10:00:00.000+0000"),
        ]
        # Second scan: server gets a JQL with `updated >=` filter, returns
        # only the newer issue.
        second_issues = [_issue("SEC-3", updated="2026-05-03T10:00:00.000+0000")]

        # Run 1
        seen_jql: list[str] = []

        def handler1(request: httpx.Request) -> httpx.Response:
            seen_jql.append(request.url.params.get("jql", ""))
            return httpx.Response(200, json={"issues": first_issues})

        async with _client(handler1) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                cursor = c.cursor_after_run()
            finally:
                await c.close()
        assert cursor == "2026-05-02T10:00:00.000+0000"
        assert "updated >=" not in seen_jql[0]

        # Run 2
        def handler2(request: httpx.Request) -> httpx.Response:
            seen_jql.append(request.url.params.get("jql", ""))
            return httpx.Response(200, json={"issues": second_issues})

        async with _client(handler2) as client2:
            c2 = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client2,
            )
            try:
                refs = [r async for r in c2.discover(SourceFilter(), cursor)]
                cursor2 = c2.cursor_after_run()
            finally:
                await c2.close()
        assert [r.path for r in refs] == ["SEC/SEC-3"]
        assert cursor2 == "2026-05-03T10:00:00.000+0000"
        assert f'updated >= "{cursor}"' in seen_jql[-1]

    async def test_cursor_after_run_none_when_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"issues": []})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert c.cursor_after_run() is None

    async def test_high_water_only_advances_forward(self) -> None:
        # Server returns issues out of timestamp order — high water
        # must be the maximum, not the last seen.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "issues": [
                        _issue("SEC-1", updated="2026-05-05T10:00:00.000+0000"),
                        _issue("SEC-2", updated="2026-05-03T10:00:00.000+0000"),
                    ]
                },
            )

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert c.cursor_after_run() == "2026-05-05T10:00:00.000+0000"


# --- filter --------------------------------------------------------


class TestFilter:
    async def test_include_pattern_keeps_matching_issues(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            return httpx.Response(
                200,
                json={
                    "issues": [
                        _issue("SEC-1", project="SEC"),
                        _issue("INFRA-9", project="INFRA"),
                    ]
                },
            )

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("SEC/*",)), None
                    )
                ]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["SEC/SEC-1"]

    async def test_exclude_pattern_drops_matching_issues(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            return httpx.Response(
                200,
                json={
                    "issues": [
                        _issue("SEC-1", project="SEC"),
                        _issue("INFRA-9", project="INFRA"),
                    ]
                },
            )

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("INFRA/*",)), None
                    )
                ]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["SEC/SEC-1"]

    async def test_comment_filter_include_exclude(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "comments": [
                            {"id": "1", "body": _adf_paragraph("ok")},
                            {"id": "2", "body": _adf_paragraph("ok")},
                        ],
                        "total": 2,
                    },
                )
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        # exclude one comment
        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(exclude=("*/comments/2",)), None
                    )
                ]
            finally:
                await c.close()
        comment_paths = {
            r.path for r in refs if r.metadata["kind"] == "comment"
        }
        assert comment_paths == {"SEC/SEC-1/comments/1"}

        # include only one comment + one issue
        async with _client(handler) as client2:
            c2 = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                ),
                client=client2,
            )
            try:
                refs2 = [
                    r
                    async for r in c2.discover(
                        SourceFilter(
                            include=("SEC/SEC-1", "SEC/SEC-1/comments/2")
                        ),
                        None,
                    )
                ]
            finally:
                await c2.close()
        assert {r.path for r in refs2} == {
            "SEC/SEC-1",
            "SEC/SEC-1/comments/2",
        }


# --- fetch edges --------------------------------------------------


class TestFetch:
    async def test_fetch_yields_full_document(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            return httpx.Response(
                200,
                json={
                    "issues": [
                        _issue(
                            "SEC-1",
                            description=_adf_complex(),
                            summary="hot finding",
                        )
                    ]
                },
            )

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
            finally:
                await c.close()
        assert isinstance(docs[0], Document)
        assert "summary=hot finding" in docs[0].text
        assert "Title" in docs[0].text
        assert "two deep" in docs[0].text

    async def test_fetch_unknown_path_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(lambda _r: httpx.Response(200, json={"issues": []})) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                ),
                client=client,
            )
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()

    async def test_issue_without_description(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            issue = _issue("SEC-1")
            issue["fields"]["description"] = None
            return httpx.Response(200, json={"issues": [issue]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
            finally:
                await c.close()
        assert "description=" not in docs[0].text
        assert "summary=secret leaked" in docs[0].text

    async def test_comment_without_body_or_author(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "comments": [{"id": "99"}],
                        "total": 1,
                    },
                )
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                comment_ref = next(
                    r for r in refs if r.metadata["kind"] == "comment"
                )
                docs = [d async for d in c.fetch(comment_ref)]
            finally:
                await c.close()
        assert "id=99" in docs[0].text
        assert "author=" not in docs[0].text
        assert "body=" not in docs[0].text


# --- malformed shapes ---------------------------------------------


class TestMalformed:
    async def test_issue_without_project_key_falls_back_to_key_prefix(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            issue = _issue("FOO-7")
            issue["fields"]["project"] = {}  # empty mapping
            return httpx.Response(200, json={"issues": [issue]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].path == "FOO/FOO-7"

    async def test_comment_with_non_mapping_author(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "comments": [
                            {
                                "id": "1",
                                "author": "not-a-mapping",
                                "body": _adf_paragraph("hi"),
                            }
                        ],
                        "total": 1,
                    },
                )
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                comment_ref = next(
                    r for r in refs if r.metadata["kind"] == "comment"
                )
                docs = [d async for d in c.fetch(comment_ref)]
            finally:
                await c.close()
        # Non-mapping author silently dropped; body still surfaces.
        assert "author=" not in docs[0].text
        assert "hi" in docs[0].text

    async def test_updated_field_missing_does_not_advance_high_water(self) -> None:
        # `updated` is not a string → high water must stay None and the
        # ref still emit successfully.
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            issue = _issue("SEC-1")
            issue["fields"]["updated"] = None
            return httpx.Response(200, json={"issues": [issue]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert len(refs) == 1
        assert c.cursor_after_run() is None

    async def test_updated_with_unparseable_iso_value(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(200, json={"comments": []})
            issue = _issue("SEC-1", updated="not-an-iso-timestamp")
            return httpx.Response(200, json={"issues": [issue]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net",
                    email="e",
                    api_token="t",
                    include_comments=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        # last_modified silently None for unparseable input.
        assert refs[0].last_modified is None


# --- jql builder --------------------------------------------------


class TestJqlBuilder:
    def test_no_projects_no_cursor(self) -> None:
        assert _build_jql((), None) == "ORDER BY updated ASC"

    def test_projects_only(self) -> None:
        assert _build_jql(("A",), None) == 'project in ("A") ORDER BY updated ASC'

    def test_cursor_only(self) -> None:
        assert (
            _build_jql((), "2026-05-01T00:00:00.000+0000")
            == 'updated >= "2026-05-01T00:00:00.000+0000" ORDER BY updated ASC'
        )

    def test_projects_and_cursor(self) -> None:
        out = _build_jql(("SEC",), "2026-05-01T00:00:00.000+0000")
        assert out == (
            'project in ("SEC") AND '
            'updated >= "2026-05-01T00:00:00.000+0000" ORDER BY updated ASC'
        )


# --- native_url ---------------------------------------------------


class TestNativeUrl:
    async def test_issue_url_set(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/comment" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "comments": [
                            {"id": "55", "body": _adf_paragraph("c")}
                        ],
                        "total": 1,
                    },
                )
            return httpx.Response(200, json={"issues": [_issue("SEC-1")]})

        async with _client(handler) as client:
            c = JiraConnector(
                JiraConfig(
                    base_url="https://acme.atlassian.net/",
                    email="e",
                    api_token="t",
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        issue_ref = next(r for r in refs if r.metadata["kind"] == "issue")
        comment_ref = next(r for r in refs if r.metadata["kind"] == "comment")
        assert issue_ref.native_url == "https://acme.atlassian.net/browse/SEC-1"
        assert comment_ref.native_url == (
            "https://acme.atlassian.net/browse/SEC-1?focusedCommentId=55"
        )


# --- spec / factory -----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "jira"
        assert SPEC.version == "0.1.0"
        assert "jira:read" in SPEC.required_scopes
        assert SPEC.capabilities.incremental is True
        assert SPEC.capabilities.max_concurrent_fetches == 4

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create(
            "jira",
            {"base_url": "https://x", "email": "e", "api_token": "t"},
        )
        assert isinstance(c, JiraConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "jira",
            {
                "base_url": "https://x",
                "email": "e",
                "api_token": "t",
                "projects": ["A", "B"],
                "include_comments": False,
                "deployment": "dc",
                "id": "explicit",
            },
        )
        assert c.id == "explicit"

    def test_factory_rejects_missing_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            SPEC.factory({"api_token": "t"})

    def test_factory_rejects_missing_api_token(self) -> None:
        with pytest.raises(ValueError, match="api_token"):
            SPEC.factory({"base_url": "https://x"})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = JiraConnector(JiraConfig(base_url="https://x", email="e", api_token="t"))
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = JiraConnector(
            JiraConfig(base_url="https://x", email="e", api_token="t"),
            client=client,
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()
