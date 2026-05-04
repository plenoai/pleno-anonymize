"""Tests for JiraConnector — separate TestCloud + TestDatacenter classes."""

from __future__ import annotations

import base64
import json
from typing import Any

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
from pleno_pii_scanner.sources.base import DocumentRef

from pleno_pii_scanner_jira import (
    SPEC,
    JiraConfig,
    JiraConnector,
)
from tests.conftest import (
    adf_doc,
    adf_text,
    cloud_issue,
    dc_issue,
    json_response,
)


# ------------------------------------------------------------------
# fixtures
# ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _cloud_config(**overrides: Any) -> JiraConfig:
    base = dict(
        flavor="cloud",
        base_url="https://acme.atlassian.net",
        email="alice@acme.com",
        api_token="api-token-xyz",
    )
    base.update(overrides)
    return JiraConfig(**base)  # type: ignore[arg-type]


def _dc_config(**overrides: Any) -> JiraConfig:
    base = dict(
        flavor="datacenter",
        base_url="https://jira.acme.internal",
        access_token="dc-pat-token",
    )
    base.update(overrides)
    return JiraConfig(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------
# config
# ------------------------------------------------------------------


class TestConfig:
    def test_rejects_invalid_flavor(self) -> None:
        with pytest.raises(ValueError, match="flavor"):
            JiraConfig(
                flavor="server",  # type: ignore[arg-type]
                base_url="https://x",
                access_token="t",
            )

    def test_rejects_empty_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            JiraConfig(flavor="cloud", base_url="", access_token="t")

    def test_rejects_non_http_base_url(self) -> None:
        with pytest.raises(ValueError, match="http"):
            JiraConfig(
                flavor="cloud", base_url="ftp://x", access_token="t"
            )

    def test_rejects_no_auth(self) -> None:
        with pytest.raises(ValueError, match="one of"):
            JiraConfig(
                flavor="cloud", base_url="https://acme.atlassian.net"
            )

    def test_rejects_two_auth_modes(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            JiraConfig(
                flavor="cloud",
                base_url="https://acme.atlassian.net",
                access_token="t",
                email="a@b",
                api_token="x",
            )

    def test_resolved_id_explicit(self) -> None:
        cfg = _cloud_config(id="custom")
        assert cfg.resolved_id() == "custom"

    def test_resolved_id_default_no_secret(self) -> None:
        cfg = _cloud_config(api_token="this-must-not-leak")
        rid = cfg.resolved_id()
        assert "this-must-not-leak" not in rid
        assert rid == "jira-cloud:acme.atlassian.net"

    def test_resolved_id_dc(self) -> None:
        cfg = _dc_config(access_token="this-must-not-leak")
        rid = cfg.resolved_id()
        assert "this-must-not-leak" not in rid
        assert rid == "jira-datacenter:jira.acme.internal"

    def test_build_auth_basic_cloud(self) -> None:
        from pleno_pii_scanner_jira.api import BasicAuth

        cfg = _cloud_config()
        auth = cfg.build_auth()
        assert isinstance(auth, BasicAuth)
        assert auth.username == "alice@acme.com"

    def test_build_auth_bearer(self) -> None:
        from pleno_pii_scanner_jira.api import BearerAuth

        cfg = JiraConfig(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            access_token="oauth-tok",
        )
        auth = cfg.build_auth()
        assert isinstance(auth, BearerAuth)
        assert auth.token == "oauth-tok"

    def test_build_auth_dc_basic(self) -> None:
        from pleno_pii_scanner_jira.api import BasicAuth

        cfg = JiraConfig(
            flavor="datacenter",
            base_url="https://jira.acme.internal",
            username="svc",
            password="p@55",
        )
        auth = cfg.build_auth()
        assert isinstance(auth, BasicAuth)


# ------------------------------------------------------------------
# protocol surface
# ------------------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = JiraConnector(_cloud_config())
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = JiraConnector(_cloud_config())
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# ------------------------------------------------------------------
# Cloud
# ------------------------------------------------------------------


class TestCloud:
    async def test_basic_auth_header_present(self) -> None:
        seen_headers: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(request.headers.get("Authorization", ""))
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert seen_headers
            assert seen_headers[0].startswith("Basic ")
            expected = "Basic " + base64.b64encode(
                b"alice@acme.com:api-token-xyz"
            ).decode()
            assert seen_headers[0] == expected
        finally:
            await c.close()

    async def test_oauth_bearer_header(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            if request.url.path.endswith("/project/search"):
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            JiraConfig(
                flavor="cloud",
                base_url="https://acme.atlassian.net",
                access_token="oauth-xyz",
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert seen[0] == "Bearer oauth-xyz"
        finally:
            await c.close()

    async def test_project_enumeration_pagination(self) -> None:
        # Two-page project list: first {isLast: false, 2 values}, second
        # {isLast: true, 1 value}. Confirms startAt/isLast handling.
        seen_starts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                start = request.url.params.get("startAt", "0")
                seen_starts.append(start)
                if start == "0":
                    return json_response(
                        {
                            "values": [{"key": "A"}, {"key": "B"}],
                            "isLast": False,
                        }
                    )
                return json_response(
                    {"values": [{"key": "C"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response({"issues": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert seen_starts == ["0", "2"]
        finally:
            await c.close()

    async def test_project_enumeration_empty_page_terminates(self) -> None:
        # Edge: page reports `isLast=false` but `values=[]`. Must stop.
        seen_starts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                start = request.url.params.get("startAt", "0")
                seen_starts.append(start)
                return json_response({"values": [], "isLast": False})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert seen_starts == ["0"]
        finally:
            await c.close()

    async def test_project_allow_list_skips_enumeration(self) -> None:
        called = {"projects": False}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                called["projects"] = True
                return json_response({"values": [], "isLast": True})
            if path.endswith("/search"):
                return json_response({"issues": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(projects=("ENG",)),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert not called["projects"]
        finally:
            await c.close()

    async def test_filter_include_exclude_on_projects(self) -> None:
        seen_jql: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response({"values": [], "isLast": True})
            if path.endswith("/search"):
                seen_jql.append(request.url.params.get("jql", ""))
                return json_response({"issues": [], "total": 0})
            return json_response({})

        # include=("A",) — only A is searched
        c = JiraConnector(
            _cloud_config(projects=("A", "B")),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [
                r
                async for r in c.discover(SourceFilter(include=("A",)), None)
            ]
            assert any('project = "A"' in j for j in seen_jql)
            assert not any('project = "B"' in j for j in seen_jql)
        finally:
            await c.close()

        # exclude=("A",) — only B is searched
        seen_jql.clear()
        c2 = JiraConnector(
            _cloud_config(projects=("A", "B")),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [
                r
                async for r in c2.discover(SourceFilter(exclude=("A",)), None)
            ]
            assert not any('project = "A"' in j for j in seen_jql)
            assert any('project = "B"' in j for j in seen_jql)
        finally:
            await c2.close()

    async def test_jql_pagination_two_pages(self) -> None:
        seen_starts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                start = request.url.params.get("startAt", "0")
                seen_starts.append(start)
                if start == "0":
                    issues = [
                        cloud_issue(
                            key=f"P-{i}",
                            updated=f"2026-05-04T00:00:0{i}.000+0000",
                        )
                        for i in range(100)
                    ]
                    return json_response(
                        {"issues": issues, "total": 101, "startAt": 0}
                    )
                return json_response(
                    {
                        "issues": [
                            cloud_issue(
                                key="P-100",
                                updated="2026-05-04T00:01:00.000+0000",
                            )
                        ],
                        "total": 101,
                        "startAt": 100,
                    }
                )
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(include_comments=False),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 101
            assert seen_starts == ["0", "100"]
        finally:
            await c.close()

    async def test_jql_pagination_terminates_on_short_page(self) -> None:
        # `total` larger than what we actually see — should still stop.
        seen_starts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                start = request.url.params.get("startAt", "0")
                seen_starts.append(start)
                return json_response(
                    {
                        "issues": [cloud_issue(key="P-1")],
                        "total": 9999,
                    }
                )
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(include_comments=False),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 1
            assert seen_starts == ["0"]
        finally:
            await c.close()

    async def test_adf_description_serialised(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                issue = cloud_issue(
                    description=adf_doc(
                        {
                            "type": "paragraph",
                            "content": [adf_text("leak: AKIA1234567890")],
                        }
                    )
                )
                return json_response({"issues": [issue], "total": 1})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 1
            docs = [d async for d in c.fetch(refs[0])]
            assert len(docs) == 1
            assert isinstance(docs[0], Document)
            text = docs[0].text or ""
            assert "key=PROJ-1" in text
            assert "summary=Investigate leak" in text
            assert "status=Open" in text
            assert "assignee=Alice" in text
            assert "reporter=Bob" in text
            assert "AKIA1234567890" in text
        finally:
            await c.close()

    async def test_comment_fetch_and_paginate(self) -> None:
        seen_starts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                start = request.url.params.get("startAt", "0")
                seen_starts.append(start)
                if start == "0":
                    comments = [
                        {
                            "id": str(i),
                            "author": {"displayName": "Carol"},
                            "body": adf_doc(
                                {
                                    "type": "paragraph",
                                    "content": [adf_text(f"comment-{i}")],
                                }
                            ),
                        }
                        for i in range(100)
                    ]
                    return json_response(
                        {"comments": comments, "total": 101}
                    )
                return json_response(
                    {
                        "comments": [
                            {
                                "id": "100",
                                "author": {"displayName": "Carol"},
                                "body": adf_doc(
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("comment-100")],
                                    }
                                ),
                            }
                        ],
                        "total": 101,
                    }
                )
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert seen_starts == ["0", "100"]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "comment[0]=Carol: comment-0" in text
            assert "comment[100]=Carol: comment-100" in text
        finally:
            await c.close()

    async def test_attachments_serialised_no_body_download(self) -> None:
        downloaded: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                issue = cloud_issue(
                    attachments=[
                        {
                            "filename": "leak.txt",
                            "content": "https://acme.atlassian.net/secure/attachment/100/leak.txt",
                        }
                    ]
                )
                return json_response({"issues": [issue], "total": 1})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            # Any attempt to GET an attachment URL would land here.
            downloaded.append(path)
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert (
                "attachment=leak.txt, url=https://acme.atlassian.net/secure/attachment/100/leak.txt"
                in text
            )
            assert downloaded == []
        finally:
            await c.close()

    async def test_attachments_disabled_when_flag_off(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                issue = cloud_issue(
                    attachments=[{"filename": "x", "content": "u"}]
                )
                return json_response({"issues": [issue], "total": 1})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(include_attachments=False),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "attachment=" not in text
        finally:
            await c.close()

    async def test_comment_disabled_when_flag_off(self) -> None:
        called = {"comment": False}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                called["comment"] = True
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(include_comments=False),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert not called["comment"]
        finally:
            await c.close()

    async def test_cursor_round_trip(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                issues = [
                    cloud_issue(
                        key="P-1",
                        updated="2026-05-04T00:00:00.000+0000",
                    ),
                    cloud_issue(
                        key="P-2",
                        updated="2026-05-04T01:00:00.000+0000",
                    ),
                ]
                return json_response({"issues": issues, "total": 2})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(include_comments=False),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            cursor = c.cursor_after_run()
            assert cursor is not None
            decoded = json.loads(cursor)
            assert decoded["highest_updated"] == "2026-05-04T01:00:00.000+0000"
        finally:
            await c.close()

    async def test_cursor_uses_jql_since(self) -> None:
        seen_jql: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                seen_jql.append(request.url.params.get("jql", ""))
                return json_response({"issues": [], "total": 0})
            return json_response({})

        prior = json.dumps(
            {"highest_updated": "2026-05-01T00:00:00.000+0000"}
        )
        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), prior)]
            assert any(
                'updated >= "2026-05-01T00:00:00.000+0000"' in j
                for j in seen_jql
            )
        finally:
            await c.close()

    async def test_malformed_cursor_silently_ignored(self) -> None:
        seen_jql: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                seen_jql.append(request.url.params.get("jql", ""))
                return json_response({"issues": [], "total": 0})
            return json_response({})

        for bad in (
            "not-json",
            '"a string"',
            "[]",
            '{"other": "x"}',
            '{"highest_updated": ""}',
            '{"highest_updated": 5}',
        ):
            c = JiraConnector(
                _cloud_config(),
                transport=httpx.MockTransport(handler),
            )
            seen_jql.clear()
            try:
                _ = [r async for r in c.discover(SourceFilter(), bad)]
                # No JQL clause for `updated >= ...` when cursor is bad.
                assert all("updated >=" not in j for j in seen_jql)
            finally:
                await c.close()

    async def test_filter_since_overrides_cursor(self) -> None:
        from datetime import datetime, timezone

        seen_jql: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                seen_jql.append(request.url.params.get("jql", ""))
                return json_response({"issues": [], "total": 0})
            return json_response({})

        prior = json.dumps(
            {"highest_updated": "2026-05-01T00:00:00.000+0000"}
        )
        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            since_dt = datetime(2026, 5, 3, tzinfo=timezone.utc)
            _ = [
                r
                async for r in c.discover(
                    SourceFilter(since=since_dt), prior
                )
            ]
            assert any("2026-05-03" in j for j in seen_jql)
            assert not any("2026-05-01" in j for j in seen_jql)
        finally:
            await c.close()

    async def test_cursor_none_when_nothing_observed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert c.cursor_after_run() is None
        finally:
            await c.close()

    async def test_429_backoff(self) -> None:
        attempts = {"n": 0}

        async def fake_sleep(_t: float) -> None:
            return None

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return httpx.Response(
                        429, headers={"Retry-After": "0.01"}
                    )
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert attempts["n"] == 2
        finally:
            await c.close()

    async def test_issue_without_key_skipped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                # Missing `key` — third-party Jira-compatible servers
                # have been seen to omit it.
                bad = {"id": "1", "fields": {"summary": "x", "updated": "x"}}
                ok = cloud_issue(key="P-1")
                return json_response({"issues": [bad, ok], "total": 2})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            keys = [r.metadata["key"] for r in refs]
            assert keys == ["P-1"]
        finally:
            await c.close()

    async def test_fetch_unknown_ref_falls_back_to_live_issue(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/issue/PROJ-99" in path and "/comment" not in path:
                return json_response(cloud_issue(key="PROJ-99"))
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="jira://PROJ/PROJ-99",
                metadata={"key": "PROJ-99"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert len(docs) == 1
        finally:
            await c.close()

    async def test_fetch_without_metadata_returns_empty(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()

    async def test_fetch_missing_issue_returns_empty(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="jira://A/MISSING-1",
                metadata={"key": "MISSING-1"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()


# ------------------------------------------------------------------
# Data Center
# ------------------------------------------------------------------


class TestDatacenter:
    async def test_basic_auth_dc(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            if request.url.path.endswith("/project/search"):
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            JiraConfig(
                flavor="datacenter",
                base_url="https://jira.acme.internal",
                username="svc",
                password="p@55",
            ),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            expected = "Basic " + base64.b64encode(b"svc:p@55").decode()
            assert seen[0] == expected
        finally:
            await c.close()

    async def test_pat_bearer_dc(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization", ""))
            if request.url.path.endswith("/project/search"):
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            _dc_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert seen[0] == "Bearer dc-pat-token"
        finally:
            await c.close()

    async def test_v2_path_used(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request.url.path)
            if request.url.path.endswith("/project/search"):
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            _dc_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert any("/rest/api/2/" in p for p in observed)
            assert not any("/rest/api/3/" in p for p in observed)
        finally:
            await c.close()

    async def test_storage_xhtml_description(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                issue = dc_issue(
                    description=(
                        "<p>SSN: <b>123-45-6789</b></p>"
                        "<p>see <a href='https://acme'>link</a></p>"
                    )
                )
                return json_response({"issues": [issue], "total": 1})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _dc_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "SSN: 123-45-6789" in text
            assert "<b>" not in text
            assert "<p>" not in text
        finally:
            await c.close()

    async def test_storage_xhtml_comment_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response({"issues": [dc_issue()], "total": 1})
            if "/comment" in path:
                return json_response(
                    {
                        "comments": [
                            {
                                "id": "5",
                                "author": {"displayName": "Carol"},
                                "body": "<p>see <em>here</em></p>",
                            }
                        ],
                        "total": 1,
                    }
                )
            return json_response({})

        c = JiraConnector(
            _dc_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "comment[5]=Carol: see here" in text
        finally:
            await c.close()

    async def test_503_backoff(self) -> None:
        attempts = {"n": 0}

        async def fake_sleep(_t: float) -> None:
            return None

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return httpx.Response(
                        503, headers={"Retry-After": "0.01"}
                    )
                return json_response({"values": [], "isLast": True})
            return json_response({})

        c = JiraConnector(
            _dc_config(),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            assert attempts["n"] == 2
        finally:
            await c.close()


# ------------------------------------------------------------------
# Factory + Spec
# ------------------------------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "jira"
        assert SPEC.version == "0.1.0"
        assert SPEC.capabilities.incremental is True

    def test_factory_minimal_cloud(self) -> None:
        register(SPEC)
        c = create(
            "jira",
            {
                "flavor": "cloud",
                "base_url": "https://acme.atlassian.net",
                "access_token": "tok",
            },
        )
        assert isinstance(c, JiraConnector)

    def test_factory_minimal_dc(self) -> None:
        register(SPEC)
        c = create(
            "jira",
            {
                "flavor": "datacenter",
                "base_url": "https://jira.acme.internal",
                "access_token": "pat",
            },
        )
        assert isinstance(c, JiraConnector)

    def test_factory_full_options(self) -> None:
        register(SPEC)
        c = create(
            "jira",
            {
                "flavor": "cloud",
                "base_url": "https://acme.atlassian.net",
                "email": "a@b",
                "api_token": "tok",
                "projects": ["A", "B"],
                "include_comments": False,
                "include_attachments": False,
                "request_timeout": 60.0,
                "id": "x",
            },
        )
        assert c.id == "x"
        assert c.config.projects == ("A", "B")

    def test_factory_rejects_invalid_flavor(self) -> None:
        with pytest.raises(ValueError, match="flavor"):
            SPEC.factory(
                {
                    "flavor": "server",
                    "base_url": "x",
                    "access_token": "t",
                }
            )

    def test_factory_rejects_missing_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            SPEC.factory({"flavor": "cloud", "access_token": "t"})

    def test_factory_rejects_bare_string_projects(self) -> None:
        with pytest.raises(ValueError, match="bare string"):
            SPEC.factory(
                {
                    "flavor": "cloud",
                    "base_url": "https://acme.atlassian.net",
                    "access_token": "t",
                    "projects": "ENG",
                }
            )

    def test_factory_rejects_non_iterable_projects(self) -> None:
        with pytest.raises(ValueError, match="iterable"):
            SPEC.factory(
                {
                    "flavor": "cloud",
                    "base_url": "https://acme.atlassian.net",
                    "access_token": "t",
                    "projects": 5,
                }
            )

    def test_factory_rejects_empty_string_in_projects(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            SPEC.factory(
                {
                    "flavor": "cloud",
                    "base_url": "https://acme.atlassian.net",
                    "access_token": "t",
                    "projects": [""],
                }
            )

    def test_factory_no_token_in_resolved_id(self) -> None:
        register(SPEC)
        c = create(
            "jira",
            {
                "flavor": "cloud",
                "base_url": "https://acme.atlassian.net",
                "access_token": "leak-token-xyz",
            },
        )
        assert "leak-token-xyz" not in c.id


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


class TestLifecycle:
    async def test_close_idempotent(self) -> None:
        c = JiraConnector(_cloud_config())
        await c.close()
        await c.close()

    async def test_close_releases_client(self) -> None:
        # Verify the underlying httpx client is closed.
        c = JiraConnector(_cloud_config())
        client = c.api._client  # type: ignore[attr-defined]
        await c.close()
        assert client.is_closed


# ------------------------------------------------------------------
# Helpers + edge cases
# ------------------------------------------------------------------


class TestSerialiserEdges:
    """Branches in `_serialise_issue` for missing / weird fields."""

    async def test_issue_without_fields_renders_only_key(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {
                        "issues": [{"id": "1", "key": "P-1"}],
                        "total": 1,
                    }
                )
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert text == "key=P-1"
        finally:
            await c.close()

    async def test_issue_with_no_summary_assignee(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                # Missing summary, status, assignee, reporter.
                return json_response(
                    {
                        "issues": [
                            {
                                "id": "1",
                                "key": "P-1",
                                "fields": {
                                    "updated": "2026-05-04T00:00:00.000+0000"
                                },
                            }
                        ],
                        "total": 1,
                    }
                )
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert text == "key=P-1"
        finally:
            await c.close()

    async def test_comment_without_author_renders_only_body(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                return json_response(
                    {
                        "comments": [
                            {
                                "id": "9",
                                # no author
                                "body": adf_doc(
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("anon")],
                                    }
                                ),
                            }
                        ],
                        "total": 1,
                    }
                )
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "comment[9]=anon" in text
        finally:
            await c.close()

    async def test_comment_without_id_renders_with_default_label(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                return json_response(
                    {
                        "comments": [
                            {
                                "body": adf_doc(
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("xx")],
                                    }
                                )
                            }
                        ],
                        "total": 1,
                    }
                )
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "comment=xx" in text
        finally:
            await c.close()

    async def test_comment_with_empty_body_is_skipped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                return json_response(
                    {
                        "comments": [
                            {"id": "9", "body": None},
                            {"id": "10"},
                        ],
                        "total": 2,
                    }
                )
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "comment[9]" not in text
            assert "comment[10]" not in text
        finally:
            await c.close()

    async def test_attachment_non_mapping_skipped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                issue = cloud_issue(
                    attachments=["bad", {"filename": "x", "content": "u"}]
                )
                return json_response({"issues": [issue], "total": 1})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "attachment=x, url=u" in text
            assert text.count("attachment=") == 1
        finally:
            await c.close()

    async def test_attachment_with_only_name_or_only_url(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                # Skip an entry with neither name nor url.
                issue = cloud_issue(
                    attachments=[
                        {"filename": "named.txt"},
                        {"contentUrl": "http://only.url"},
                        {"name": "n2"},
                        {},
                    ]
                )
                return json_response({"issues": [issue], "total": 1})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "attachment=named.txt" in text
            assert "url=http://only.url" in text
            assert "attachment=n2" in text
        finally:
            await c.close()

    async def test_serialise_no_text_yields_nothing(self) -> None:
        # An issue with no key, no summary, no description, no comments
        # → serialiser produces empty string → fetch yields nothing.
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/issue/X" in path and "/comment" not in path:
                # Empty body for the live-fetch path.
                return json_response({})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"key": "X"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()

    async def test_serialise_truthy_issue_no_text_yields_nothing(
        self,
    ) -> None:
        # Live-fetch path: 200 with a non-empty body but no extractable
        # text — every field is junk. _serialise_issue returns "" and
        # fetch must yield nothing rather than emit a blank Document
        # (which would fail the text/binary XOR invariant).
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/issue/X" in path and "/comment" not in path:
                return json_response({"junk": True})
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="x",
                metadata={"key": "X"},
            )
            docs = [d async for d in c.fetch(ref)]
            assert docs == []
        finally:
            await c.close()

    async def test_comment_pagination_short_page_terminates(self) -> None:
        # First page is full (100 items), second page is short (1 item).
        # The short-page check must terminate before a third request.
        request_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                request_count["n"] += 1
                if request_count["n"] == 1:
                    return json_response(
                        {
                            "comments": [
                                {
                                    "id": str(i),
                                    "body": adf_doc(
                                        {
                                            "type": "paragraph",
                                            "content": [adf_text("x")],
                                        }
                                    ),
                                }
                                for i in range(100)
                            ],
                            # Lie: total is much larger than what we'll return.
                            "total": 10_000,
                        }
                    )
                return json_response(
                    {
                        "comments": [
                            {
                                "id": "100",
                                "body": adf_doc(
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("x")],
                                    }
                                ),
                            }
                        ],
                        "total": 10_000,
                    }
                )
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            # Should stop after second comment page (short-page guard).
            assert request_count["n"] == 2
        finally:
            await c.close()

    def test_convert_body_dc_with_dict(self) -> None:
        # DC flavor + dict body (custom-field shape).
        c = JiraConnector(_dc_config())
        try:
            out = c._convert_body({"value": "<p>hello</p>"})
            assert out == "hello"
        finally:
            import asyncio

            asyncio.get_event_loop().run_until_complete(c.close()) if False else None

    def test_convert_body_empty_string(self) -> None:
        c = JiraConnector(_cloud_config())
        try:
            assert c._convert_body("") == ""
            assert c._convert_body(None) == ""
        finally:
            pass

    def test_named_handles_non_mapping(self) -> None:
        from pleno_pii_scanner_jira.connector import _named

        assert _named(None) == ""
        assert _named({"name": 5}) == ""

    def test_display_name_handles_non_mapping(self) -> None:
        from pleno_pii_scanner_jira.connector import _display_name

        assert _display_name(None) == ""
        # Falls back across keys.
        assert _display_name({"emailAddress": "a@b"}) == "a@b"
        assert _display_name({"accountId": "x"}) == "x"

    def test_host_only_handles_no_scheme(self) -> None:
        from pleno_pii_scanner_jira.connector import _host_only

        assert _host_only("acme.atlassian.net") == "acme.atlassian.net"
        assert _host_only("http://x/y") == "x"

    def test_issue_updated_handles_missing_or_bad(self) -> None:
        from pleno_pii_scanner_jira.connector import _issue_updated

        assert _issue_updated({"fields": "x"}) is None
        assert _issue_updated({"fields": {"updated": 5}}) is None
        assert _issue_updated({"fields": {}}) is None

    def test_parse_iso_handles_garbage(self) -> None:
        from pleno_pii_scanner_jira.connector import _parse_iso

        assert _parse_iso(None) is None
        assert _parse_iso("") is None
        assert _parse_iso("not-iso-format") is None

    def test_parse_iso_negative_offset_normalised(self) -> None:
        from pleno_pii_scanner_jira.connector import _parse_iso

        # `-0500` (no colon) — normalisation must insert the colon.
        out = _parse_iso("2026-05-04T12:00:00.000-0500")
        assert out is not None
        assert out.utcoffset() is not None

    def test_parse_iso_already_colon_offset(self) -> None:
        from pleno_pii_scanner_jira.connector import _parse_iso

        out = _parse_iso("2026-05-04T12:00:00.000+00:00")
        assert out is not None

    def test_decode_cursor_non_string_value(self) -> None:
        from pleno_pii_scanner_jira.connector import _decode_cursor

        # `highest_updated` is not a string → ignored.
        assert _decode_cursor('{"highest_updated": 5}') is None

    async def test_project_search_404_returns_empty(self) -> None:
        # 404 -> api returns empty dict -> _list_all_projects returns [].
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return httpx.Response(404)
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert refs == []
        finally:
            await c.close()

    async def test_search_404_returns_empty(self) -> None:
        # /search returning 404 → empty body → iterator stops.
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return httpx.Response(404)
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert refs == []
        finally:
            await c.close()

    async def test_issues_with_non_mapping_entries_skipped(self) -> None:
        # Defensive: an `issues` array with garbage entries is filtered.
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {
                        "issues": ["bad", None, cloud_issue(key="P-1")],
                        "total": 3,
                    }
                )
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert [r.metadata["key"] for r in refs] == ["P-1"]
        finally:
            await c.close()

    async def test_comments_404_returns_empty_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                return httpx.Response(404)
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert len(refs) == 1
            assert refs[0].metadata["comment_count"] == "0"
        finally:
            await c.close()

    async def test_comments_with_non_mapping_entries(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {"issues": [cloud_issue()], "total": 1}
                )
            if "/comment" in path:
                return json_response(
                    {
                        "comments": [
                            "bad",
                            None,
                            {
                                "id": "1",
                                "body": adf_doc(
                                    {
                                        "type": "paragraph",
                                        "content": [adf_text("ok")],
                                    }
                                ),
                            },
                        ],
                        "total": 3,
                    }
                )
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "comment[1]" in text
        finally:
            await c.close()

    async def test_project_search_skips_non_mapping_project(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": ["bad", {"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response({"issues": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            _ = [r async for r in c.discover(SourceFilter(), None)]
            # No crash → success.
        finally:
            await c.close()

    async def test_cloud_string_description_falls_through_to_storage(
        self,
    ) -> None:
        # Cloud + raw-string description (legacy custom-field shape).
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {
                        "issues": [
                            cloud_issue(
                                description="<p>raw <b>html</b></p>",  # type: ignore[arg-type]
                            )
                        ],
                        "total": 1,
                    }
                )
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            docs = [d async for d in c.fetch(refs[0])]
            text = docs[0].text or ""
            assert "raw html" in text
        finally:
            await c.close()

    async def test_issue_to_ref_without_summary(self) -> None:
        # Missing `summary` -> size=None on DocumentRef.
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/project/search"):
                return json_response(
                    {"values": [{"key": "P"}], "isLast": True}
                )
            if path.endswith("/search"):
                return json_response(
                    {
                        "issues": [
                            {
                                "id": "1",
                                "key": "P-1",
                                "fields": {
                                    "updated": "2026-05-04T00:00:00.000+0000"
                                },
                            }
                        ],
                        "total": 1,
                    }
                )
            if "/comment" in path:
                return json_response({"comments": [], "total": 0})
            return json_response({})

        c = JiraConnector(
            _cloud_config(),
            transport=httpx.MockTransport(handler),
        )
        try:
            refs = [r async for r in c.discover(SourceFilter(), None)]
            assert refs[0].size is None
            assert refs[0].native_url is not None
        finally:
            await c.close()
