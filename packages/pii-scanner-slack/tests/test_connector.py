"""End-to-end tests for the SlackConnector orchestrator."""

from __future__ import annotations

import httpx
import pytest
from pleno_pii_scanner.scheduler.rate_limit import BucketKey, RateLimited
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
)
from slack_sdk.errors import SlackApiError

from pleno_pii_scanner_slack import SPEC, SlackConfig, SlackConnector
from pleno_pii_scanner_slack.connector import _factory

from .conftest import FakeAsyncWebClient, FakeResponse, make_client_factory


# -- helpers ----------------------------------------------------------------


def _list_resp(channels, next_cursor=""):
    return FakeResponse(
        {
            "channels": channels,
            "response_metadata": {"next_cursor": next_cursor},
        }
    )


def _hist_resp(messages, next_cursor=""):
    return FakeResponse(
        {
            "messages": messages,
            "response_metadata": {"next_cursor": next_cursor},
        }
    )


# -- protocol compliance ----------------------------------------------------


class TestProtocol:
    def test_implements_source_connector_protocol(self) -> None:
        # `xoxb-*` is fine; we never actually call the API in this test.
        c = SlackConnector(SlackConfig(token="xoxb-test", team_id="T1"))
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = SlackConnector(SlackConfig(token="xoxb-test", team_id="T1"))
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    def test_resolved_id_default(self) -> None:
        c = SlackConnector(SlackConfig(token="xoxb-test", team_id="T1"))
        assert c.id == "slack:bot:T1"

    def test_resolved_id_org(self) -> None:
        c = SlackConnector(
            SlackConfig(token="xoxa-1", enterprise_id="E1")
        )
        assert c.id == "slack:org:E1"

    def test_resolved_id_unknown_scope(self) -> None:
        c = SlackConnector(SlackConfig(token="xoxp-1"))
        assert c.id == "slack:user:unknown"

    def test_resolved_id_override(self) -> None:
        c = SlackConnector(SlackConfig(token="xoxb-1", id="snap-A"))
        assert c.id == "snap-A"

    def test_invalid_token_rejected_at_construct(self) -> None:
        with pytest.raises(Exception):
            SlackConnector(SlackConfig(token="not-a-real-prefix"))

    def test_bucket_key_per_channel(self) -> None:
        c = SlackConnector(SlackConfig(token="xoxb-1", team_id="T1"))
        key = c.bucket_key("C99")
        assert key == BucketKey(connector_kind="slack", tenant_id="T1:C99")

    def test_bucket_key_unknown_team(self) -> None:
        c = SlackConnector(SlackConfig(token="xoxb-1"))
        key = c.bucket_key("C99")
        assert key.tenant_id == "?:C99"


# -- discover (xoxb / xoxp) -------------------------------------------------


class TestDiscoverConversations:
    async def test_resolves_team_via_auth_test(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("auth_test", FakeResponse({"team_id": "T9", "ok": True}))
        fake.script("conversations_list", _list_resp([{"id": "C1"}]))
        fake.script("conversations_history", _hist_resp([{"ts": "1.0", "user": "U", "text": "x"}]))
        c = SlackConnector(
            SlackConfig(token="xoxb-test"),
            client_factory=make_client_factory(fake),
        )
        try:
            refs = [r async for r in c.discover(filter=_filt(), cursor=None)]
        finally:
            await c.close()
        assert refs[0].path == "slack://T9/C1/1.0"

    async def test_uses_configured_team_without_auth_test(self) -> None:
        fake = FakeAsyncWebClient()
        # No auth_test scripted — would AssertionError if called.
        fake.script("conversations_list", _list_resp([{"id": "C1"}]))
        fake.script("conversations_history", _hist_resp([{"ts": "1.0", "user": "U", "text": "x"}]))
        c = SlackConnector(
            SlackConfig(token="xoxb-test", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            refs = [r async for r in c.discover(filter=_filt(), cursor=None)]
        finally:
            await c.close()
        assert refs[0].path == "slack://T1/C1/1.0"

    async def test_cursor_is_round_tripped(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("conversations_list", _list_resp([{"id": "C1"}]))
        fake.script("conversations_history", _hist_resp([]))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            cursor = '{"C1":"500.0"}'
            refs = [r async for r in c.discover(filter=_filt(), cursor=cursor)]
        finally:
            await c.close()
        # The history call must have been made with oldest=500.0.
        history_call = next(call for call in fake.calls if call[0] == "conversations_history")
        assert history_call[1]["oldest"] == "500.0"
        assert refs == []


class TestDiscoverDiscovery:
    async def test_resolves_enterprise_id(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script(
            "discovery_enterprise_info",
            FakeResponse({"enterprise": {"id": "E777"}}),
        )
        fake.script(
            "discovery_conversations_list",
            _list_resp([{"id": "C1", "team": "T1"}]),
        )
        fake.script(
            "discovery_conversations_history",
            _hist_resp([{"ts": "1.0", "user": "U", "text": "x"}]),
        )
        c = SlackConnector(
            SlackConfig(token="xoxa-org"),
            client_factory=make_client_factory(fake),
        )
        try:
            refs = [r async for r in c.discover(filter=_filt(), cursor=None)]
        finally:
            await c.close()
        assert refs[0].path == "slack://T1/C1/1.0"
        assert refs[0].metadata["discovery"] == "1"

    async def test_skips_enterprise_info_if_preconfigured(self) -> None:
        fake = FakeAsyncWebClient()
        # No discovery_enterprise_info scripted — would AssertionError.
        fake.script(
            "discovery_conversations_list",
            _list_resp([{"id": "C1", "team": "T1"}]),
        )
        fake.script(
            "discovery_conversations_history",
            _hist_resp([{"ts": "1.0", "user": "U", "text": "x"}]),
        )
        c = SlackConnector(
            SlackConfig(token="xoxa-org", enterprise_id="E1"),
            client_factory=make_client_factory(fake),
        )
        try:
            refs = [r async for r in c.discover(filter=_filt(), cursor=None)]
        finally:
            await c.close()
        assert refs


# -- fetch ------------------------------------------------------------------


class TestFetchMessage:
    async def test_returns_text_document(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script(
            "conversations_history",
            _hist_resp([{"ts": "1.0", "user": "U1", "text": "the body"}]),
        )
        fake.script(
            "users_info",
            FakeResponse(
                {
                    "user": {
                        "id": "U1",
                        "name": "alice",
                        "real_name": "Alice Example",
                        "profile": {"email": "alice@example.com"},
                    }
                }
            ),
        )
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert len(docs) == 1
        assert isinstance(docs[0], Document)
        assert docs[0].text == "the body"
        assert docs[0].created_by is not None
        assert docs[0].created_by.email == "alice@example.com"
        assert docs[0].created_by.display_name == "Alice Example"

    async def test_principal_cache_hits_once(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("conversations_history", _hist_resp([{"ts": "1.0", "user": "U1", "text": "a"}]))
        fake.script("conversations_history", _hist_resp([{"ts": "2.0", "user": "U1", "text": "b"}]))
        fake.script(
            "users_info",
            FakeResponse({"user": {"id": "U1", "real_name": "Alice"}}),
        )
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref1 = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            ref2 = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/2.0",
                metadata={"channel_id": "C1", "ts": "2.0", "team_id": "T1"},
            )
            d1 = [d async for d in c.fetch(ref1)]
            d2 = [d async for d in c.fetch(ref2)]
        finally:
            await c.close()
        # Only one users_info call across two fetches.
        users_calls = [call for call in fake.calls if call[0] == "users_info"]
        assert len(users_calls) == 1
        assert d1[0].created_by is d2[0].created_by

    async def test_principal_disabled(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("conversations_history", _hist_resp([{"ts": "1.0", "user": "U1", "text": "a"}]))
        c = SlackConnector(
            SlackConfig(
                token="xoxb-x", team_id="T1", fetch_user_principal=False
            ),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs[0].created_by is None
        assert not any(call[0] == "users_info" for call in fake.calls)

    async def test_principal_fallback_when_user_not_found(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("conversations_history", _hist_resp([{"ts": "1.0", "user": "U1", "text": "x"}]))
        err_resp = FakeResponse({"ok": False, "error": "user_not_found"}, status_code=200)
        fake.script("users_info", SlackApiError("nope", err_resp))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs[0].created_by is not None
        assert docs[0].created_by.id == "U1"
        assert docs[0].created_by.display_name is None

    async def test_principal_users_info_unexpected_error_raises(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("conversations_history", _hist_resp([{"ts": "1.0", "user": "U1", "text": "x"}]))
        err_resp = FakeResponse({"ok": False, "error": "internal_error"}, status_code=500)
        fake.script("users_info", SlackApiError("boom", err_resp))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            with pytest.raises(SlackApiError):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()

    async def test_principal_with_no_user_in_message(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script(
            "conversations_history", _hist_resp([{"ts": "1.0", "text": "system"}])
        )
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs[0].created_by is None

    async def test_principal_minimal_when_user_payload_missing(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("conversations_history", _hist_resp([{"ts": "1.0", "user": "U2", "text": "x"}]))
        # users.info returns ok=true but with no user object — strange,
        # but Slack has been observed to return this on permission denial.
        fake.script("users_info", FakeResponse({"ok": True}))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs[0].created_by is not None
        assert docs[0].created_by.id == "U2"

    async def test_message_not_in_channel_returns_empty(self) -> None:
        fake = FakeAsyncWebClient()
        err_resp = FakeResponse({"ok": False, "error": "not_in_channel"}, status_code=200)
        fake.script("conversations_history", SlackApiError("nope", err_resp))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs == []

    async def test_message_429_propagates(self) -> None:
        fake = FakeAsyncWebClient()
        rate_resp = FakeResponse(
            {"ok": False, "error": "ratelimited"},
            status_code=429,
            headers={"Retry-After": "10"},
        )
        fake.script("conversations_history", SlackApiError("rate", rate_resp))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            with pytest.raises(RateLimited):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()

    async def test_other_slack_error_propagates(self) -> None:
        fake = FakeAsyncWebClient()
        err_resp = FakeResponse({"ok": False, "error": "fatal_error"}, status_code=200)
        fake.script("conversations_history", SlackApiError("boom", err_resp))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            with pytest.raises(SlackApiError):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()

    async def test_message_empty_history(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("conversations_history", _hist_resp([]))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0",
                metadata={"channel_id": "C1", "ts": "1.0", "team_id": "T1"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs == []


# -- fetch (file) -----------------------------------------------------------


class TestFetchFile:
    async def test_text_file_downloaded(self) -> None:
        # httpx MockTransport handles the file body; the connector still
        # uses the fake AsyncWebClient for files.info + users.info.
        fake = FakeAsyncWebClient()
        fake.script(
            "files_info",
            FakeResponse(
                {
                    "file": {
                        "id": "F1",
                        "name": "secret.txt",
                        "mimetype": "text/plain",
                        "url_private_download": "https://files.slack.com/x",
                        "user": "U1",
                    }
                }
            ),
        )
        fake.script(
            "users_info",
            FakeResponse({"user": {"id": "U1", "real_name": "Alice"}}),
        )

        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(
                200,
                content=b"the secret payload",
                headers={"Content-Type": "text/plain"},
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c = SlackConnector(
            SlackConfig(token="xoxb-zzz", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=http,
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={
                    "channel_id": "C1",
                    "team_id": "T1",
                    "ts": "1.0",
                    "file_id": "F1",
                    "parent_ts": "1.0",
                },
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert len(docs) == 1
        assert docs[0].text == "the secret payload"
        assert captured["auth"] == "Bearer xoxb-zzz"
        assert docs[0].created_by is not None

    async def test_binary_file_downloaded(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script(
            "files_info",
            FakeResponse(
                {
                    "file": {
                        "id": "F1",
                        "name": "logo.png",
                        "mimetype": "image/png",
                        "url_private_download": "https://files.slack.com/img",
                    }
                }
            ),
        )

        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(
                200,
                content=b"\x89PNG\r\n",
                headers={"Content-Type": "image/png"},
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        c = SlackConnector(
            SlackConfig(token="xoxb-zzz", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=http,
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={
                    "channel_id": "C1",
                    "team_id": "T1",
                    "ts": "1.0",
                    "file_id": "F1",
                    "parent_ts": "1.0",
                },
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs[0].binary == b"\x89PNG\r\n"
        assert docs[0].text is None

    async def test_file_not_found_returns_empty(self) -> None:
        fake = FakeAsyncWebClient()
        err = FakeResponse({"ok": False, "error": "file_not_found"}, status_code=200)
        fake.script("files_info", SlackApiError("missing", err))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs == []

    async def test_files_info_429_propagates(self) -> None:
        fake = FakeAsyncWebClient()
        rate = FakeResponse(
            {"ok": False, "error": "ratelimited"},
            status_code=429,
            headers={"Retry-After": "20"},
        )
        fake.script("files_info", SlackApiError("rate", rate))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            with pytest.raises(RateLimited):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()

    async def test_files_info_other_error_propagates(self) -> None:
        fake = FakeAsyncWebClient()
        err = FakeResponse({"ok": False, "error": "fatal_error"}, status_code=200)
        fake.script("files_info", SlackApiError("boom", err))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            with pytest.raises(SlackApiError):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()

    async def test_file_with_no_url_returns_empty(self) -> None:
        fake = FakeAsyncWebClient()
        # files_info returns a file object with neither url_private_download
        # nor url_private (extremely degraded but conceivable).
        fake.script(
            "files_info",
            FakeResponse({"file": {"id": "F1", "name": "x.txt"}}),
        )
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs == []

    async def test_file_with_no_file_object_returns_empty(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script("files_info", FakeResponse({"ok": True}))
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert docs == []

    async def test_file_url_uses_url_private_when_no_download_url(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script(
            "files_info",
            FakeResponse(
                {
                    "file": {
                        "id": "F1",
                        "name": "x.txt",
                        "url_private": "https://files.slack.com/fallback",
                    }
                }
            ),
        )

        called: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            called["url"] = str(request.url)
            return httpx.Response(200, content=b"ok", headers={"Content-Type": "text/plain"})

        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            docs = [d async for d in c.fetch(ref)]
        finally:
            await c.close()
        assert called["url"] == "https://files.slack.com/fallback"
        assert docs[0].text == "ok"

    async def test_file_download_429_translates_to_rate_limited(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script(
            "files_info",
            FakeResponse(
                {
                    "file": {
                        "id": "F1",
                        "name": "x.txt",
                        "url_private_download": "https://files.slack.com/x",
                    }
                }
            ),
        )

        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(429, headers={"Retry-After": "5"})

        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            with pytest.raises(RateLimited):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()

    async def test_file_download_other_http_error_propagates(self) -> None:
        fake = FakeAsyncWebClient()
        fake.script(
            "files_info",
            FakeResponse(
                {
                    "file": {
                        "id": "F1",
                        "name": "x.txt",
                        "url_private_download": "https://files.slack.com/x",
                    }
                }
            ),
        )

        def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
            return httpx.Response(500)

        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind="slack",
                path="slack://T1/C1/1.0/files/F1",
                metadata={"channel_id": "C1", "team_id": "T1", "ts": "1.0", "file_id": "F1", "parent_ts": "1.0"},
            )
            with pytest.raises(httpx.HTTPStatusError):
                [d async for d in c.fetch(ref)]
        finally:
            await c.close()


# -- close ------------------------------------------------------------------


class TestClose:
    async def test_close_closes_owned_http(self) -> None:
        fake = FakeAsyncWebClient()
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        # Force both clients open so close() actually exercises both paths.
        await c._ensure_client()
        await c._ensure_http()
        await c.close()
        assert c._http is None
        assert c._client is None
        assert fake.closed is True

    async def test_close_idempotent(self) -> None:
        fake = FakeAsyncWebClient()
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
        )
        await c.close()
        await c.close()  # second call must not raise

    async def test_close_does_not_close_external_http(self) -> None:
        fake = FakeAsyncWebClient()
        external = httpx.AsyncClient()
        c = SlackConnector(
            SlackConfig(token="xoxb-x", team_id="T1"),
            client_factory=make_client_factory(fake),
            http_client=external,
        )
        await c.close()
        # External http client is left open; caller owns it.
        assert not external.is_closed
        await external.aclose()


# -- factory + SPEC ---------------------------------------------------------


class TestFactory:
    def test_factory_builds_connector(self) -> None:
        c = _factory({"token": "xoxb-z", "team_id": "T1"})
        assert isinstance(c, SlackConnector)
        assert c.id == "slack:bot:T1"

    def test_factory_requires_token(self) -> None:
        with pytest.raises(ValueError, match="token"):
            _factory({})

    def test_factory_rejects_bad_token(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            _factory({"token": "not-real-prefix"})

    def test_factory_propagates_optional_fields(self) -> None:
        c = _factory(
            {
                "token": "xoxa-1",
                "id": "snap-A",
                "enterprise_id": "E1",
                "include_threads": False,
                "include_files": False,
                "fetch_user_principal": False,
                "request_timeout": 5.5,
            }
        )
        assert isinstance(c, SlackConnector)
        assert c.id == "snap-A"

    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "slack"
        assert SPEC.capabilities.incremental is True
        assert SPEC.capabilities.binary is True
        assert "discovery:read" in SPEC.required_scopes


class TestApiErrorCodeHelper:
    def test_returns_none_when_no_response(self) -> None:
        from pleno_pii_scanner_slack.connector import _api_error_code

        exc = SlackApiError("orphan", None)
        assert _api_error_code(exc) is None

    def test_returns_error_string(self) -> None:
        from pleno_pii_scanner_slack.connector import _api_error_code

        response = FakeResponse({"ok": False, "error": "channel_not_found"})
        exc = SlackApiError("missing", response)
        assert _api_error_code(exc) == "channel_not_found"


class TestDefaultClientFactory:
    def test_returns_real_async_web_client(self) -> None:
        # Smoke test only — we don't make any network calls. The default
        # factory must produce something walking-and-talking like the SDK
        # client so tests aren't the only path that constructs it.
        from slack_sdk.web.async_client import AsyncWebClient

        from pleno_pii_scanner_slack.connector import _default_client_factory

        client = _default_client_factory(token="xoxb-test", timeout=10.0)
        assert isinstance(client, AsyncWebClient)


class TestDefaultClientFactoryViaConnector:
    async def test_connector_uses_default_factory(self) -> None:
        # When no client_factory is passed, the connector must lazily
        # construct via _default_client_factory. We don't run discover
        # (that would need network) — just verify _ensure_client returns
        # a real AsyncWebClient instance.
        from slack_sdk.web.async_client import AsyncWebClient

        c = SlackConnector(SlackConfig(token="xoxb-x", team_id="T1"))
        try:
            client = await c._ensure_client()
            assert isinstance(client, AsyncWebClient)
        finally:
            await c.close()


# -- helpers ----------------------------------------------------------------


def _filt():
    from pleno_pii_scanner.sources.base import SourceFilter

    return SourceFilter()
