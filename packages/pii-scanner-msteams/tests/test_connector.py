"""Tests for MsTeamsConnector — uses httpx.MockTransport doubles."""

from __future__ import annotations

import json
from collections.abc import Callable
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
from pleno_pii_scanner_msteams import (
    SPEC,
    MsTeamsConfig,
    MsTeamsConnector,
)
from pleno_pii_scanner_msteams.connector import _strip_html


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    # No base_url — connector uses absolute URLs for both login.
    # microsoftonline.com and graph.microsoft.com.
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _token_response(token: str = "tok-1", expires_in: int = 3600) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "token_type": "Bearer",
            "expires_in": expires_in,
            "access_token": token,
        },
    )


def _msg(
    msg_id: str = "m1",
    body: str = "hello",
    content_type: str = "text",
    display: str = "Alice",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "etag": f"etag-{msg_id}",
        "createdDateTime": "2025-01-02T03:04:05Z",
        "from": {"user": {"displayName": display}},
        "body": {"contentType": content_type, "content": body},
    }


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_tenant(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            MsTeamsConfig(tenant_id="", client_id="c", client_secret="s")

    def test_rejects_empty_client(self) -> None:
        with pytest.raises(ValueError, match="client_id"):
            MsTeamsConfig(tenant_id="t", client_id="", client_secret="s")

    def test_rejects_no_auth(self) -> None:
        with pytest.raises(ValueError, match="client_secret / federated_token"):
            MsTeamsConfig(tenant_id="t", client_id="c")

    def test_rejects_both_auth(self) -> None:
        with pytest.raises(ValueError, match="client_secret / federated_token"):
            MsTeamsConfig(
                tenant_id="t",
                client_id="c",
                client_secret="s",
                federated_token="f",
            )

    def test_explicit_id(self) -> None:
        cfg = MsTeamsConfig(
            tenant_id="t", client_id="c", client_secret="s", id="x"
        )
        assert cfg.resolved_id() == "x"

    def test_default_id_no_secret_leak(self) -> None:
        cfg = MsTeamsConfig(
            tenant_id="t",
            client_id="c",
            client_secret="VERYSECRET",
            teams=("a", "b"),
        )
        rid = cfg.resolved_id()
        assert "VERYSECRET" not in rid
        assert rid.startswith("msteams:")

    def test_default_id_order_independent(self) -> None:
        a = MsTeamsConfig(
            tenant_id="t", client_id="c", client_secret="s", teams=("a", "b")
        )
        b = MsTeamsConfig(
            tenant_id="t", client_id="c", client_secret="s", teams=("b", "a")
        )
        assert a.resolved_id() == b.resolved_id()


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = MsTeamsConnector(
            MsTeamsConfig(tenant_id="t", client_id="c", client_secret="s")
        )
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = MsTeamsConnector(
            MsTeamsConfig(tenant_id="t", client_id="c", client_secret="s")
        )
        assert c.capabilities() == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- token acquisition --------------------------------------------


class TestToken:
    async def test_client_secret_grant(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                form = dict(
                    item.split("=", 1)
                    for item in request.content.decode().split("&")
                )
                seen.append(form)
                return _token_response()
            if request.url.path == "/v1.0/teams":
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="tid", client_id="cid", client_secret="csec"
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert len(seen) == 1
        assert seen[0]["grant_type"] == "client_credentials"
        assert seen[0]["client_id"] == "cid"
        assert seen[0]["client_secret"] == "csec"
        assert "client_assertion" not in seen[0]

    async def test_federated_jwt_bearer_grant(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                form = dict(
                    item.split("=", 1)
                    for item in request.content.decode().split("&")
                )
                seen.append(form)
                return _token_response()
            if request.url.path == "/v1.0/teams":
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="tid",
                    client_id="cid",
                    federated_token="signed-jwt",
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen[0]["client_assertion"] == "signed-jwt"
        assert seen[0]["client_assertion_type"].startswith("urn%3A") or seen[0][
            "client_assertion_type"
        ].startswith("urn:")
        assert "client_secret" not in seen[0]

    async def test_token_cache_hit(self) -> None:
        token_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                token_calls["n"] += 1
                return _token_response(expires_in=3600)
            if request.url.path == "/v1.0/teams":
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="tid", client_id="cid", client_secret="csec"
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        # Two discover() runs, but only one token round-trip — cache hit.
        assert token_calls["n"] == 1


# --- delta walk ---------------------------------------------------


def _make_handler(
    *,
    teams: list[dict[str, Any]] | None = None,
    channels: list[dict[str, Any]] | None = None,
    delta_pages: dict[str, list[dict[str, Any]]] | None = None,
    deltalink: str = "https://graph.microsoft.com/v1.0/delta-next",
    replies: dict[str, list[dict[str, Any]]] | None = None,
    resume_link_match: str | None = None,
) -> tuple[Callable[[httpx.Request], httpx.Response], dict[str, list[str]]]:
    """Build a Graph mock. `delta_pages[channel_id]` is a flat list of
    messages returned in one shot (single page with deltaLink)."""
    teams = teams if teams is not None else [{"id": "T1", "displayName": "team"}]
    channels = channels if channels is not None else [
        {"id": "C1", "displayName": "general"}
    ]
    delta_pages = delta_pages or {}
    replies = replies or {}
    seen: dict[str, list[str]] = {"paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["paths"].append(str(request.url))
        if request.url.host == "login.microsoftonline.com":
            return _token_response()
        path = request.url.path
        if path == "/v1.0/teams":
            return httpx.Response(200, json={"value": teams})
        if path.endswith("/channels") and "/teams/" in path:
            return httpx.Response(200, json={"value": channels})
        if "/messages/delta" in path:
            chan = path.split("/channels/")[1].split("/")[0]
            msgs = delta_pages.get(chan, [])
            return httpx.Response(
                200,
                json={"value": msgs, "@odata.deltaLink": deltalink},
            )
        if (
            resume_link_match is not None
            and resume_link_match in str(request.url)
        ):
            chan = "C1"
            return httpx.Response(
                200,
                json={
                    "value": delta_pages.get(chan, []),
                    "@odata.deltaLink": deltalink,
                },
            )
        if path.endswith("/replies"):
            msg_id = path.split("/messages/")[1].split("/")[0]
            return httpx.Response(
                200, json={"value": replies.get(msg_id, [])}
            )
        return httpx.Response(404, content=str(request.url).encode())

    return handler, seen


class TestDelta:
    async def test_initial_walk_yields_messages(self) -> None:
        handler, _seen = _make_handler(
            delta_pages={"C1": [_msg("m1"), _msg("m2")]},
            deltalink="https://graph.microsoft.com/v1.0/delta-next",
        )
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert {r.metadata["message_id"] for r in refs} == {"m1", "m2"}
                # Cursor captured the deltaLink for next-run resume.
                cur = c.cursor_after_run()
                assert cur is not None
                decoded = json.loads(cur)
                assert decoded["C1"].endswith("/delta-next")
            finally:
                await c.close()

    async def test_resume_uses_stored_deltalink(self) -> None:
        # The stored cursor should be the URL the connector hits next
        # time, not the canonical delta endpoint.
        resume_url = "https://graph.microsoft.com/v1.0/delta-resume-token"
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            if request.url.host == "login.microsoftonline.com":
                return _token_response()
            path = request.url.path
            if path == "/v1.0/teams":
                return httpx.Response(
                    200, json={"value": [{"id": "T1", "displayName": "team"}]}
                )
            if path.endswith("/channels") and "/teams/" in path:
                return httpx.Response(
                    200,
                    json={
                        "value": [{"id": "C1", "displayName": "general"}]
                    },
                )
            if str(request.url) == resume_url:
                return httpx.Response(
                    200,
                    json={
                        "value": [_msg("m_new")],
                        "@odata.deltaLink": (
                            "https://graph.microsoft.com/v1.0/delta-next"
                        ),
                    },
                )
            if "/messages/delta" in path:
                # Must NOT be hit on resume.
                raise AssertionError(
                    "initial /messages/delta hit when resume_link should drive walk"
                )
            return httpx.Response(404)

        cursor = json.dumps({"C1": resume_url})
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), cursor)]
                assert {r.metadata["message_id"] for r in refs} == {"m_new"}
                assert any(u == resume_url for u in seen_urls)
            finally:
                await c.close()

    async def test_empty_resume_keeps_prior_link(self) -> None:
        # Server returns empty value AND no deltaLink — connector must
        # preserve the prior link so we do not re-walk history next run.
        resume_url = "https://graph.microsoft.com/v1.0/delta-stable"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                return _token_response()
            path = request.url.path
            if path == "/v1.0/teams":
                return httpx.Response(
                    200, json={"value": [{"id": "T1", "displayName": "team"}]}
                )
            if path.endswith("/channels") and "/teams/" in path:
                return httpx.Response(
                    200,
                    json={"value": [{"id": "C1", "displayName": "g"}]},
                )
            if str(request.url) == resume_url:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                _ = [
                    r
                    async for r in c.discover(
                        SourceFilter(), json.dumps({"C1": resume_url})
                    )
                ]
                cur = c.cursor_after_run()
                assert cur is not None
                assert json.loads(cur)["C1"] == resume_url
            finally:
                await c.close()

    async def test_nextlink_pagination(self) -> None:
        next_url = "https://graph.microsoft.com/v1.0/page2"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                return _token_response()
            path = request.url.path
            if path == "/v1.0/teams":
                return httpx.Response(
                    200, json={"value": [{"id": "T1", "displayName": "team"}]}
                )
            if path.endswith("/channels") and "/teams/" in path:
                return httpx.Response(
                    200,
                    json={"value": [{"id": "C1", "displayName": "g"}]},
                )
            if str(request.url) == next_url:
                return httpx.Response(
                    200,
                    json={
                        "value": [_msg("m2")],
                        "@odata.deltaLink": (
                            "https://graph.microsoft.com/v1.0/delta-end"
                        ),
                    },
                )
            if "/messages/delta" in path:
                return httpx.Response(
                    200,
                    json={
                        "value": [_msg("m1")],
                        "@odata.nextLink": next_url,
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert {r.metadata["message_id"] for r in refs} == {"m1", "m2"}
                cur = c.cursor_after_run()
                assert cur is not None
                assert json.loads(cur)["C1"].endswith("/delta-end")
            finally:
                await c.close()


# --- replies / allowlist / filter ---------------------------------


class TestReplies:
    async def test_include_replies_yields_replies(self) -> None:
        handler, _ = _make_handler(
            delta_pages={"C1": [_msg("m1")]},
            replies={"m1": [_msg("r1", body="reply body")]},
        )
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=True,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                kinds = {r.metadata["kind"] for r in refs}
                assert kinds == {"message", "reply"}
                reply = next(r for r in refs if r.metadata["kind"] == "reply")
                assert reply.metadata["parent_message_id"] == "m1"
                docs = [d async for d in c.fetch(reply)]
                assert "reply body" in docs[0].text
            finally:
                await c.close()

    async def test_include_replies_off_skips(self) -> None:
        handler, _ = _make_handler(
            delta_pages={"C1": [_msg("m1")]},
            replies={"m1": [_msg("r1")]},
        )
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert {r.metadata["kind"] for r in refs} == {"message"}
            finally:
                await c.close()

    async def test_replies_paginate(self) -> None:
        next_url = (
            "https://graph.microsoft.com/v1.0/replies-page-2"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                return _token_response()
            path = request.url.path
            if path == "/v1.0/teams":
                return httpx.Response(
                    200, json={"value": [{"id": "T1", "displayName": "team"}]}
                )
            if path.endswith("/channels") and "/teams/" in path:
                return httpx.Response(
                    200,
                    json={"value": [{"id": "C1", "displayName": "g"}]},
                )
            if "/messages/delta" in path:
                return httpx.Response(
                    200,
                    json={
                        "value": [_msg("m1")],
                        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d",
                    },
                )
            if str(request.url) == next_url:
                return httpx.Response(
                    200, json={"value": [_msg("r2")]}
                )
            if path.endswith("/replies"):
                return httpx.Response(
                    200,
                    json={
                        "value": [_msg("r1")],
                        "@odata.nextLink": next_url,
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                reply_ids = {
                    r.metadata["message_id"]
                    for r in refs
                    if r.metadata["kind"] == "reply"
                }
                assert reply_ids == {"r1", "r2"}
            finally:
                await c.close()


class TestAllowlist:
    async def test_teams_allowlist_skips_global_list(self) -> None:
        called_global = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                return _token_response()
            path = request.url.path
            if path == "/v1.0/teams":
                called_global["hit"] = True
                return httpx.Response(500)
            if path.endswith("/channels") and "/teams/" in path:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    teams=("T_pinned",),
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert not called_global["hit"]
            finally:
                await c.close()


class TestFilter:
    async def test_include_exclude_filters_message_paths(self) -> None:
        handler, _ = _make_handler(
            delta_pages={
                "C1": [_msg("keep"), _msg("drop")]
            }
        )
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("*/keep",)), None
                    )
                ]
                assert {r.metadata["message_id"] for r in refs} == {"keep"}
            finally:
                await c.close()
        async with _client(handler) as client2:
            c2 = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client2,
            )
            try:
                refs = [
                    r
                    async for r in c2.discover(
                        SourceFilter(exclude=("*/drop",)), None
                    )
                ]
                assert {r.metadata["message_id"] for r in refs} == {"keep"}
            finally:
                await c2.close()

    async def test_reply_filter_drops_reply_only(self) -> None:
        handler, _ = _make_handler(
            delta_pages={"C1": [_msg("m1")]},
            replies={"m1": [_msg("r_drop")]},
        )
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                ),
                client=client,
            )
            try:
                # Include matches m1 but not the reply path.
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(include=("*/m1",)), None
                    )
                ]
                ids = {r.metadata["message_id"] for r in refs}
                assert ids == {"m1"}
            finally:
                await c.close()

        async with _client(handler) as client2:
            c2 = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                ),
                client=client2,
            )
            try:
                refs = [
                    r
                    async for r in c2.discover(
                        SourceFilter(exclude=("*/replies/*",)), None
                    )
                ]
                ids = {r.metadata["message_id"] for r in refs}
                assert ids == {"m1"}
            finally:
                await c2.close()


# --- HTML strip / fetch ----------------------------------------


class TestRender:
    def test_strip_html_unescapes(self) -> None:
        assert _strip_html("<p>hi &amp; bye</p>") == "hi & bye"

    async def test_fetch_strips_html_and_carries_metadata(self) -> None:
        msg = _msg(
            "m1",
            body="<p>token=<b>AKIA12345</b>&nbsp;leak</p>",
            content_type="html",
        )
        handler, _ = _make_handler(delta_pages={"C1": [msg]})
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert isinstance(docs[0], Document)
                text = docs[0].text or ""
                assert "<b>" not in text
                assert "AKIA12345" in text
                # HTML entity unescaped to NBSP (\xa0) so the final
                # body has no literal "&nbsp;".
                assert "&nbsp;" not in text
                assert docs[0].extra["from_display_name"] == "Alice"
                assert docs[0].extra["createdDateTime"] == "2025-01-02T03:04:05Z"
            finally:
                await c.close()

    async def test_fetch_unknown_path_yields_nothing(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        async with _client(lambda _r: httpx.Response(404)) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
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

    async def test_render_attachments_and_text_body(self) -> None:
        msg = _msg("m1", body="plain body", content_type="text")
        msg["attachments"] = [
            {"contentUrl": "https://files/example.txt"},
            "garbage-not-mapping",
            {"name": "fallback.bin"},
            {},  # no usable url/name → skipped
        ]
        handler, _ = _make_handler(delta_pages={"C1": [msg]})
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                text = docs[0].text or ""
                assert "attachment=https://files/example.txt" in text
                assert "attachment=fallback.bin" in text
                assert "plain body" in text
                # No crash from the non-mapping attachment.
            finally:
                await c.close()


# --- malformed shapes --------------------------------------------


class TestMalformed:
    async def test_skips_non_mapping_team_and_channel_entries(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                return _token_response()
            path = request.url.path
            if path == "/v1.0/teams":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            "garbage",
                            {"id": ""},  # empty id filtered
                            {"id": "T1", "displayName": "team"},
                        ]
                    },
                )
            if path.endswith("/channels") and "/teams/T1" in path:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            "garbage",
                            {"id": ""},
                            {"id": "C1", "displayName": "g"},
                        ]
                    },
                )
            if "/messages/delta" in path:
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            "non-mapping-msg",
                            {},  # missing id — skipped
                            _msg("m1"),
                        ],
                        "@odata.deltaLink": "https://graph.microsoft.com/v1.0/d",
                    },
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert {r.metadata["message_id"] for r in refs} == {"m1"}
            finally:
                await c.close()

    async def test_reply_missing_id_skipped(self) -> None:
        handler, _ = _make_handler(
            delta_pages={"C1": [_msg("m1")]},
            replies={"m1": [{}, "garbage", _msg("r_ok")]},
        )
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                reply_ids = {
                    r.metadata["message_id"]
                    for r in refs
                    if r.metadata["kind"] == "reply"
                }
                assert reply_ids == {"r_ok"}
            finally:
                await c.close()


# --- cursor encoding ---------------------------------------------


class TestCursor:
    async def test_no_data_returns_no_cursor(self) -> None:
        handler, _ = _make_handler(delta_pages={})

        def handler2(request: httpx.Request) -> httpx.Response:
            if request.url.host == "login.microsoftonline.com":
                return _token_response()
            path = request.url.path
            if path == "/v1.0/teams":
                return httpx.Response(200, json={"value": []})
            return httpx.Response(404)

        async with _client(handler2) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t", client_id="c", client_secret="s"
                ),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert c.cursor_after_run() is None
            finally:
                await c.close()

    async def test_garbage_cursor_falls_back_to_initial(self) -> None:
        handler, _ = _make_handler(
            delta_pages={"C1": [_msg("m1")]},
        )
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                # Not JSON → decoder returns {} → fresh delta walk.
                refs = [
                    r async for r in c.discover(SourceFilter(), "not-json{")
                ]
                assert refs
            finally:
                await c.close()

    async def test_non_dict_cursor_falls_back(self) -> None:
        handler, _ = _make_handler(delta_pages={"C1": [_msg("m1")]})
        async with _client(handler) as client:
            c = MsTeamsConnector(
                MsTeamsConfig(
                    tenant_id="t",
                    client_id="c",
                    client_secret="s",
                    include_replies=False,
                ),
                client=client,
            )
            try:
                refs = [
                    r async for r in c.discover(SourceFilter(), '["a","b"]')
                ]
                assert refs
            finally:
                await c.close()


# --- spec / factory ----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "msteams"
        assert SPEC.version == "0.1.0"
        assert "Group.Read.All" in SPEC.required_scopes
        assert "Channel.ReadBasic.All" in SPEC.required_scopes
        assert "ChannelMessage.Read.All" in SPEC.required_scopes
        assert SPEC.capabilities.incremental is True
        assert SPEC.capabilities.content_hash_delta is True
        assert SPEC.capabilities.max_concurrent_fetches == 4

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create(
            "msteams",
            {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
        )
        assert isinstance(c, MsTeamsConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "msteams",
            {
                "tenant_id": "t",
                "client_id": "c",
                "federated_token": "j",
                "teams": ["T1"],
                "include_replies": False,
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_tenant(self) -> None:
        with pytest.raises(ValueError, match="tenant_id"):
            SPEC.factory({"client_id": "c", "client_secret": "s"})

    def test_factory_rejects_missing_client(self) -> None:
        with pytest.raises(ValueError, match="client_id"):
            SPEC.factory({"tenant_id": "t", "client_secret": "s"})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = MsTeamsConnector(
            MsTeamsConfig(tenant_id="t", client_id="c", client_secret="s")
        )
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = MsTeamsConnector(
            MsTeamsConfig(tenant_id="t", client_id="c", client_secret="s"),
            client=client,
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()
