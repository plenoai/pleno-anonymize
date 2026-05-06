"""Tests for DiscordConnector — uses httpx.MockTransport doubles."""

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
from pleno_pii_scanner_discord import DiscordConfig, DiscordConnector, SPEC


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _build_handler(routes: list[tuple[str, Callable[[httpx.Request], httpx.Response]]]):
    """First-match handler. Each route is (path-suffix, responder)."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, responder in routes:
            if request.url.path.endswith(suffix):
                return responder(request)
        return httpx.Response(404, content=f"unmatched: {request.url}".encode())

    return handler


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://discord.com/api/v10",
        transport=httpx.MockTransport(handler),
    )


def _msg(
    msg_id: str, content: str = "hi", author_name: str = "alice"
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "content": content,
        "timestamp": "2026-05-04T00:00:00Z",
        "author": {"id": "100", "username": author_name},
        "attachments": [],
        "embeds": [],
    }


# --- config --------------------------------------------------------


class TestConfig:
    def test_rejects_empty_token(self) -> None:
        with pytest.raises(ValueError, match="token"):
            DiscordConfig(token="")

    def test_rejects_negative_max_messages(self) -> None:
        with pytest.raises(ValueError, match="max_messages_per_channel"):
            DiscordConfig(token="t", max_messages_per_channel=-1)

    def test_rejects_zero_concurrency(self) -> None:
        with pytest.raises(ValueError, match="concurrency"):
            DiscordConfig(token="t", concurrency=0)

    def test_rejects_voice_channel_type(self) -> None:
        with pytest.raises(ValueError, match="channel_type"):
            DiscordConfig(token="t", channel_types=(2,))

    def test_explicit_id(self) -> None:
        cfg = DiscordConfig(token="t", id="x")
        assert cfg.resolved_id() == "x"

    def test_default_id_no_token_leak(self) -> None:
        cfg = DiscordConfig(token="ssssecret", guilds=("123", "456"))
        assert "ssssecret" not in cfg.resolved_id()
        assert cfg.resolved_id().startswith("discord:")

    def test_default_id_order_independent(self) -> None:
        a = DiscordConfig(token="t", guilds=("1", "2"))
        b = DiscordConfig(token="t", guilds=("2", "1"))
        assert a.resolved_id() == b.resolved_id()


# --- protocol ------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self) -> None:
        c = DiscordConnector(DiscordConfig(token="t"))
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = DiscordConnector(DiscordConfig(token="t", concurrency=4))
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- discover ------------------------------------------------------


class TestDiscover:
    async def test_full_pipeline(self) -> None:
        guilds_payload = [{"id": "g1", "name": "Guild One"}]
        channels_payload = [
            {"id": "c1", "type": 0, "name": "general"},
            {"id": "c2", "type": 2, "name": "voice"},  # filtered out
        ]
        page1 = [_msg("3"), _msg("2"), _msg("1")]

        msg_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get("Authorization") == "Bot t"
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=guilds_payload)
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=channels_payload)
            if path.endswith("/channels/c1/messages"):
                msg_calls["n"] += 1
                if msg_calls["n"] == 1:
                    return httpx.Response(200, json=page1)
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 3
                assert all(r.metadata["channel_id"] == "c1" for r in refs)
                # cursor reflects the highest snowflake we saw
                cur = c.cursor_after_run()
                assert cur is not None
                state = json.loads(cur)
                assert state["c1"] == "3"
            finally:
                await c.close()

    async def test_explicit_guilds_skip_users_me(self) -> None:
        # When `guilds` is provided we must NOT call /users/@me/guilds.
        called_users_me = {"hit": False}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                called_users_me["hit"] = True
                return httpx.Response(500)
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "c1", "type": 0}])
            if path.endswith("/channels/c1/messages"):
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(
                DiscordConfig(token="t", guilds=("g1",)), client=client
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert not called_users_me["hit"]
            finally:
                await c.close()

    async def test_max_messages_cap_applied(self) -> None:
        # Channel returns 100 messages per page; cap is 50.
        page = [_msg(str(i)) for i in range(200, 100, -1)]

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "c1", "type": 0}])
            if path.endswith("/channels/c1/messages"):
                return httpx.Response(200, json=page)
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(
                DiscordConfig(token="t", max_messages_per_channel=50),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 50
            finally:
                await c.close()

    async def test_resume_uses_after_param(self) -> None:
        seen_params: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "c1", "type": 0}])
            if path.endswith("/channels/c1/messages"):
                seen_params.append(dict(request.url.params))
                # First call returns 1 new msg, second returns empty.
                if len(seen_params) == 1:
                    return httpx.Response(200, json=[_msg("99")])
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        prior = json.dumps({"c1": "10"})
        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), prior)]
                assert len(refs) == 1
                assert seen_params[0]["after"] == "10"
            finally:
                await c.close()

    async def test_cursor_empty_string_ignored(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "c1", "type": 0}])
            if path.endswith("/channels/c1/messages"):
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), "")]
                assert refs == []
            finally:
                await c.close()

    async def test_cursor_malformed_json_ignored(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), "not-json")]
            finally:
                await c.close()

    async def test_cursor_non_dict_json_ignored(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), '["x"]')]
            finally:
                await c.close()

    async def test_filter_include_exclude(self) -> None:
        c1_calls = {"n": 0}
        c2_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": "c1", "type": 0},
                        {"id": "c2", "type": 0},
                    ],
                )
            if path.endswith("/channels/c1/messages"):
                c1_calls["n"] += 1
                return httpx.Response(
                    200, json=[_msg("1")] if c1_calls["n"] == 1 else []
                )
            if path.endswith("/channels/c2/messages"):
                c2_calls["n"] += 1
                return httpx.Response(
                    200, json=[_msg("2")] if c2_calls["n"] == 1 else []
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                refs = [
                    r async for r in c.discover(SourceFilter(include=("g1/c1",)), None)
                ]
                assert all(r.metadata["channel_id"] == "c1" for r in refs)
            finally:
                await c.close()
        async with _client(handler) as client2:
            c2 = DiscordConnector(DiscordConfig(token="t"), client=client2)
            try:
                refs2 = [
                    r async for r in c2.discover(SourceFilter(exclude=("g1/c2",)), None)
                ]
                assert all(r.metadata["channel_id"] == "c1" for r in refs2)
            finally:
                await c2.close()

    async def test_cursor_returns_none_when_nothing_new(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
                assert c.cursor_after_run() is None
            finally:
                await c.close()


# --- threads ------------------------------------------------------


class TestThreads:
    async def test_thread_channel_included(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": "c1", "type": 0, "name": "general"},
                        {"id": "t1", "type": 11, "name": "thread"},
                    ],
                )
            if path.endswith("/channels/c1/messages"):
                return httpx.Response(200, json=[])
            if path.endswith("/channels/t1/messages"):
                return httpx.Response(200, json=[_msg("9")])
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(
                DiscordConfig(token="t", include_threads=True), client=client
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert any(r.metadata["channel_id"] == "t1" for r in refs)
            finally:
                await c.close()

    async def test_thread_channel_excluded_when_disabled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(
                    200,
                    json=[
                        {"id": "t1", "type": 11},
                    ],
                )
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(
                DiscordConfig(token="t", channel_types=(0,), include_threads=False),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert refs == []
            finally:
                await c.close()


# --- fetch --------------------------------------------------------


class TestFetch:
    async def test_serialises_message_with_attachments_and_embeds(self) -> None:
        msg = {
            "id": "1",
            "content": "leaked AKIA1234567890",
            "timestamp": "2026-05-04T00:00:00Z",
            "author": {"id": "100", "username": "alice"},
            "attachments": [{"url": "https://cdn.discord/foo.png"}],
            "embeds": [{"title": "T", "description": "D"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "c1", "type": 0}])
            if path.endswith("/channels/c1/messages"):
                return httpx.Response(200, json=[msg])
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = [d async for d in c.fetch(refs[0])]
                assert len(docs) == 1
                assert isinstance(docs[0], Document)
                assert "AKIA1234567890" in docs[0].text
                assert "alice" in docs[0].text
                assert "https://cdn.discord/foo.png" in docs[0].text
                assert "embed=T D" in docs[0].text
            finally:
                await c.close()

    async def test_fetch_without_metadata_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="x")
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()

    async def test_fetch_unknown_message_returns_empty(self) -> None:
        from pleno_pii_scanner.sources.base import DocumentRef

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="g/c/m",
                    metadata={
                        "channel_id": "ghost",
                        "message_id": "missing",
                    },
                )
                docs = [d async for d in c.fetch(ref)]
                assert docs == []
            finally:
                await c.close()


# --- 429 backoff --------------------------------------------------


class TestRateLimit:
    async def test_429_then_200(self) -> None:
        attempts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "c1", "type": 0}])
            if path.endswith("/channels/c1/messages"):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return httpx.Response(429, headers={"Retry-After": "0.01"})
                if attempts["n"] == 2:
                    return httpx.Response(200, json=[_msg("1")])
                return httpx.Response(200, json=[])
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                assert len(refs) == 1
                # 1 (initial 429) + 1 (200 with msg) + 1 (subsequent empty page).
                assert attempts["n"] == 3
            finally:
                await c.close()

    async def test_persistent_429_propagates(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/users/@me/guilds"):
                return httpx.Response(200, json=[{"id": "g1"}])
            if path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "c1", "type": 0}])
            if path.endswith("/channels/c1/messages"):
                return httpx.Response(429, headers={"Retry-After": "0.01"})
            return httpx.Response(404)

        async with _client(handler) as client:
            c = DiscordConnector(DiscordConfig(token="t"), client=client)
            try:
                with pytest.raises(httpx.HTTPStatusError):
                    [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()


# --- spec / factory -----------------------------------------------


class TestSpec:
    def test_metadata(self) -> None:
        assert SPEC.kind == "discord"
        assert SPEC.version == "0.1.0"

    def test_factory_minimal(self) -> None:
        register(SPEC)
        c = create("discord", {"token": "t"})
        assert isinstance(c, DiscordConnector)

    def test_factory_full(self) -> None:
        register(SPEC)
        c = create(
            "discord",
            {
                "token": "t",
                "guilds": ["1"],
                "channel_types": [0],
                "max_messages_per_channel": 100,
                "include_threads": False,
                "concurrency": 4,
                "id": "x",
            },
        )
        assert c.id == "x"

    def test_factory_rejects_missing_token(self) -> None:
        with pytest.raises(ValueError, match="token"):
            SPEC.factory({})


# --- close --------------------------------------------------------


class TestClose:
    async def test_close_owns_client(self) -> None:
        c = DiscordConnector(DiscordConfig(token="t"))
        await c.close()

    async def test_close_external_client_not_closed(self) -> None:
        client = httpx.AsyncClient()
        c = DiscordConnector(DiscordConfig(token="t"), client=client)
        await c.close()
        assert not client.is_closed
        await client.aclose()
