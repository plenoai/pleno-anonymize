"""End-to-end tests for the discovery.* (xoxa) discovery path."""

from __future__ import annotations

import pytest
from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from slack_sdk.errors import SlackApiError

from pleno_pii_scanner_slack.discovery import (
    discover_via_discovery,
    fetch_enterprise_id,
)

from .conftest import FakeAsyncWebClient, FakeResponse


def _list_page(items, next_cursor=""):
    return FakeResponse(
        {
            "channels": items,
            "response_metadata": {"next_cursor": next_cursor},
        }
    )


def _history(messages, next_cursor=""):
    return FakeResponse(
        {
            "messages": messages,
            "response_metadata": {"next_cursor": next_cursor},
        }
    )


class TestEnterpriseInfo:
    async def test_returns_enterprise_id(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_enterprise_info",
            FakeResponse({"enterprise": {"id": "E0123", "name": "Plenoai Inc"}}),
        )
        assert await fetch_enterprise_id(client) == "E0123"

    async def test_falls_back_to_unknown(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_enterprise_info",
            FakeResponse({}),
        )
        assert await fetch_enterprise_id(client) == "unknown"

    async def test_missing_id_falls_back(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_enterprise_info",
            FakeResponse({"enterprise": {"name": "Plenoai Inc"}}),
        )
        assert await fetch_enterprise_id(client) == "unknown"


class TestDiscoveryDiscover:
    async def test_yields_messages_across_workspaces(self) -> None:
        client = FakeAsyncWebClient()
        # Two channels across two different team_ids — the cursor state
        # must key by `team:channel` to keep them separate.
        client.script(
            "discovery_conversations_list",
            _list_page(
                [
                    {"id": "C01", "team": "T1"},
                    {"id": "C01", "team": "T2"},
                ]
            ),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "1.0", "user": "U", "text": "from-T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "2.0", "user": "U", "text": "from-T2"}]),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="slack:org:E1",
                cursor_state=cursor_state,
            )
        ]
        assert [r.path for r in refs] == [
            "slack://T1/C01/1.0",
            "slack://T2/C01/2.0",
        ]
        assert cursor_state == {"T1:C01": "1.0", "T2:C01": "2.0"}
        assert refs[0].metadata["discovery"] == "1"

    async def test_team_id_field_alias(self) -> None:
        # Some discovery responses use `team_id` instead of `team`.
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team_id": "TX"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "1.0", "user": "U", "text": "ok"}]),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="slack:org:E1",
                cursor_state=cursor_state,
            )
        ]
        assert refs[0].metadata["team_id"] == "TX"

    async def test_oldest_passed_from_cursor_state(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script("discovery_conversations_history", _history([]))
        cursor_state = {"T1:C01": "999.0"}
        async for _ in discover_via_discovery(
            client=client,
            source_id="slack:org:E1",
            cursor_state=cursor_state,
        ):
            pass
        history_call = next(
            c for c in client.calls if c[0] == "discovery_conversations_history"
        )
        assert history_call[1]["oldest"] == "999.0"

    async def test_files_yielded(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [
                            {
                                "id": "F1",
                                "name": "x.png",
                                "mimetype": "image/png",
                                "url_private_download": "https://files.slack.com/x",
                            }
                        ],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert refs[1].path == "slack://T1/C01/1.0/files/F1"
        assert refs[1].content_type == "image/png"
        assert refs[1].metadata["url_private_download"] == "https://files.slack.com/x"

    async def test_files_disabled(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "1.0", "user": "U", "text": "x", "files": [{"id": "F"}]}]),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
                include_files=False,
            )
        ]
        assert len(refs) == 1

    async def test_skips_invalid_file_entries(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [None, {"name": "no-id"}, {"id": "F1"}],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert len(refs) == 2  # message + 1 valid file
        assert refs[1].metadata["file_id"] == "F1"

    async def test_text_extension_file_routes_to_text(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [{"id": "F1", "name": "main.py"}],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert refs[1].content_type == "text/plain"

    async def test_pagination_in_list_and_history(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}], next_cursor="next-list"),
        )
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C02", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "1.0", "user": "U", "text": "a"}], next_cursor="next-h"),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "2.0", "user": "U", "text": "b"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "3.0", "user": "U", "text": "c"}]),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert [r.metadata["ts"] for r in refs] == ["1.0", "2.0", "3.0"]

    async def test_response_without_metadata_terminates_pagination(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            FakeResponse({"channels": [{"id": "C1", "team": "T1"}]}),
        )
        client.script(
            "discovery_conversations_history",
            FakeResponse({"messages": [{"ts": "1.0", "user": "U", "text": "x"}]}),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert len(refs) == 1

    async def test_429_propagates_as_rate_limited(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        response = FakeResponse(
            {"ok": False, "error": "ratelimited"},
            status_code=429,
            headers={"Retry-After": "60"},
        )
        client.script(
            "discovery_conversations_history",
            SlackApiError("rate", response),
        )
        cursor_state: dict[str, str] = {}
        with pytest.raises(RateLimited):
            async for _ in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            ):
                pass

    async def test_file_url_private_fallback(self) -> None:
        # discovery file with only `url_private` (no `url_private_download`)
        # — exercises the elif branch in `_build_discovery_file_ref`.
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [
                            {
                                "id": "F1",
                                "name": "x.png",
                                "url_private": "https://files.slack.com/fallback",
                            }
                        ],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert refs[1].metadata["url_private_download"] == "https://files.slack.com/fallback"

    async def test_file_with_no_url_omits_metadata(self) -> None:
        # Neither url_private_download nor url_private — both branches
        # in the if/elif chain skipped.
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [{"id": "F1", "name": "x.txt"}],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert "url_private_download" not in refs[1].metadata

    async def test_file_without_name_omits_filename_metadata(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [{"id": "F1"}],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert "filename" not in refs[1].metadata

    async def test_message_without_user_omits_metadata(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "discovery_conversations_list",
            _list_page([{"id": "C01", "team": "T1"}]),
        )
        client.script(
            "discovery_conversations_history",
            _history([{"ts": "1.0", "text": "system msg"}]),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_discovery(
                client=client,
                source_id="src",
                cursor_state=cursor_state,
            )
        ]
        assert "user_id" not in refs[0].metadata

    async def test_enterprise_info_429_propagates(self) -> None:
        client = FakeAsyncWebClient()
        response = FakeResponse(
            {"ok": False, "error": "ratelimited"},
            status_code=429,
            headers={"Retry-After": "5"},
        )
        client.script(
            "discovery_enterprise_info", SlackApiError("rate", response)
        )
        with pytest.raises(RateLimited):
            await fetch_enterprise_id(client)
