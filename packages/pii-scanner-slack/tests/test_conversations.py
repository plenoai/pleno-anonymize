"""End-to-end tests for the conversations.* discovery path."""

from __future__ import annotations

import pytest
from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from slack_sdk.errors import SlackApiError

from pleno_pii_scanner_slack import _paths
from pleno_pii_scanner_slack.conversations import discover_via_conversations

from .conftest import FakeAsyncWebClient, FakeResponse


def _channels(*ids_with_cursor):
    """Helper: build a FakeResponse for conversations.list."""
    *ids, next_cursor = ids_with_cursor
    return FakeResponse(
        {
            "channels": [{"id": cid} for cid in ids],
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


class TestSingleChannelSinglePage:
    async def test_yields_one_ref_per_message(self) -> None:
        client = FakeAsyncWebClient()
        client.script(
            "conversations_list",
            _channels("C01", ""),
        )
        client.script(
            "conversations_history",
            _history(
                [
                    {"ts": "100.0", "user": "U1", "text": "hello"},
                    {"ts": "101.0", "user": "U2", "text": "world"},
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = []
        async for r in discover_via_conversations(
            client=client,
            source_id="slack:bot:T1",
            team_id="T1",
            cursor_state=cursor_state,
        ):
            refs.append(r)

        # Two messages, no threads, no files.
        assert len(refs) == 2
        assert refs[0].path == "slack://T1/C01/100.0"
        assert refs[1].path == "slack://T1/C01/101.0"
        assert refs[0].metadata["channel_id"] == "C01"
        # Cursor advances to the last yielded ts.
        assert cursor_state == {"C01": "101.0"}
        # And the latest cursor blob is JSON-encoded on the ref.
        assert refs[-1].metadata["_cursor"] == _paths.dump_cursor({"C01": "101.0"})


class TestPagination:
    async def test_history_pagination_follows_next_cursor(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history([{"ts": "1.0", "user": "U", "text": "a"}], next_cursor="page2"),
        )
        client.script(
            "conversations_history",
            _history([{"ts": "2.0", "user": "U", "text": "b"}], next_cursor=""),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert [r.metadata["ts"] for r in refs] == ["1.0", "2.0"]
        # Second history call must have included cursor=page2.
        history_calls = [c for c in client.calls if c[0] == "conversations_history"]
        assert history_calls[1][1].get("cursor") == "page2"

    async def test_list_pagination_follows_next_cursor(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", "more"))
        client.script("conversations_list", _channels("C02", ""))
        client.script(
            "conversations_history", _history([{"ts": "1.0", "user": "U", "text": "a"}])
        )
        client.script(
            "conversations_history", _history([{"ts": "2.0", "user": "U", "text": "b"}])
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert sorted(r.metadata["channel_id"] for r in refs) == ["C01", "C02"]


class TestIncrementalCursor:
    async def test_oldest_passed_from_cursor_state(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script("conversations_history", _history([]))
        cursor_state = {"C01": "999.0"}
        # Drain
        async for _ in discover_via_conversations(
            client=client,
            source_id="src",
            team_id="T",
            cursor_state=cursor_state,
        ):
            pass
        history_call = next(c for c in client.calls if c[0] == "conversations_history")
        assert history_call[1]["oldest"] == "999.0"


class TestThreadReplies:
    async def test_replies_yielded_when_reply_count(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        # Parent message has reply_count=2; we should fan out into replies.
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "100.0",
                        "user": "U",
                        "text": "parent",
                        "reply_count": 2,
                        "thread_ts": "100.0",
                    }
                ]
            ),
        )
        client.script(
            "conversations_replies",
            _history(
                [
                    {"ts": "100.0", "user": "U", "text": "parent"},
                    {"ts": "101.0", "user": "U", "text": "reply1"},
                    {"ts": "102.0", "user": "U", "text": "reply2"},
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        # Parent (1) + 2 replies = 3 refs; the parent must appear once.
        assert [r.metadata["ts"] for r in refs] == ["100.0", "101.0", "102.0"]

    async def test_replies_skipped_when_reply_count_zero(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "no replies",
                        "reply_count": 0,
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert len(refs) == 1
        assert (
            "conversations_replies",
            {"channel": "C01", "ts": "1.0", "limit": 200},
        ) not in [(m, k) for m, k in client.calls]

    async def test_replies_disabled_via_flag(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "p",
                        "reply_count": 5,
                        "thread_ts": "1.0",
                    }
                ]
            ),
        )
        # No replies script registered; if include_threads were honored
        # incorrectly we'd see an AssertionError from the fake client.
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
                include_threads=False,
            )
        ]
        assert len(refs) == 1


class TestFiles:
    async def test_file_ref_yielded_alongside_message(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "see attachment",
                        "files": [
                            {
                                "id": "F123",
                                "name": "secret.txt",
                                "mimetype": "text/plain",
                                "size": 42,
                                "url_private_download": "https://files.slack.com/x",
                                "permalink": "https://files.slack.com/p",
                            }
                        ],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert [r.path for r in refs] == [
            "slack://T/C01/1.0",
            "slack://T/C01/1.0/files/F123",
        ]
        file_ref = refs[1]
        assert file_ref.metadata["file_id"] == "F123"
        assert file_ref.metadata["filename"] == "secret.txt"
        assert file_ref.metadata["url_private_download"] == "https://files.slack.com/x"
        assert file_ref.size == 42
        assert file_ref.content_type == "text/plain"

    async def test_binary_file_gets_mimetype_content_type(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "img",
                        "files": [
                            {
                                "id": "F123",
                                "name": "logo.png",
                                "mimetype": "image/png",
                                "url_private": "https://files.slack.com/i",
                            }
                        ],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        file_ref = refs[1]
        assert file_ref.content_type == "image/png"
        assert file_ref.metadata["url_private_download"] == "https://files.slack.com/i"

    async def test_files_disabled_via_flag(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [{"id": "F", "name": "x.txt"}],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
                include_files=False,
            )
        ]
        assert len(refs) == 1

    async def test_skips_malformed_file_entries(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [
                            None,
                            "not-a-dict",
                            {"name": "missing-id.txt"},
                            {"id": "F1", "name": "ok.txt"},
                        ],
                    }
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        # Message + only the valid file ref.
        assert len(refs) == 2
        assert refs[1].metadata["file_id"] == "F1"


class TestPaginationEdgeCases:
    async def test_response_without_metadata_terminates_pagination(self) -> None:
        # Slack occasionally returns a page with no `response_metadata`
        # at all when there's only one page of results. The paginator
        # must treat this as "no more pages" and not crash.
        client = FakeAsyncWebClient()
        client.script(
            "conversations_list",
            FakeResponse({"channels": [{"id": "C1"}]}),
        )
        client.script(
            "conversations_history",
            FakeResponse({"messages": [{"ts": "1.0", "user": "U", "text": "x"}]}),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert len(refs) == 1


class TestRateLimitPropagation:
    async def test_429_in_history_surfaces_as_rate_limited(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        response = FakeResponse(
            {"ok": False, "error": "ratelimited"},
            status_code=429,
            headers={"Retry-After": "30"},
        )
        client.script(
            "conversations_history",
            SlackApiError("rate", response),
        )
        cursor_state: dict[str, str] = {}
        with pytest.raises(RateLimited, match="30"):
            async for _ in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            ):
                pass


class TestEdgeCases:
    async def test_message_without_user_omits_user_id_metadata(self) -> None:
        # System / app messages can have neither `user` nor `bot_id`.
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history([{"ts": "1.0", "text": "system message"}]),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert "user_id" not in refs[0].metadata

    async def test_file_without_name_omits_filename_metadata(self) -> None:
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "1.0",
                        "user": "U",
                        "text": "x",
                        "files": [
                            {
                                "id": "F1",
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
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert "filename" not in refs[1].metadata

    async def test_thread_replies_with_files_disabled(self) -> None:
        # include_threads=True but include_files=False — branch 279->263.
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "100.0",
                        "user": "U",
                        "text": "p",
                        "reply_count": 1,
                        "thread_ts": "100.0",
                    }
                ]
            ),
        )
        client.script(
            "conversations_replies",
            _history(
                [
                    {"ts": "100.0", "user": "U", "text": "p"},
                    {
                        "ts": "101.0",
                        "user": "U",
                        "text": "reply",
                        "files": [{"id": "F2", "name": "x.txt"}],
                    },
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
                include_files=False,
            )
        ]
        # Parent + reply, no file ref.
        assert [r.path for r in refs] == [
            "slack://T/C01/100.0",
            "slack://T/C01/101.0",
        ]


class TestThreadFiles:
    async def test_files_in_thread_replies(self) -> None:
        # Files attached inside thread replies should also yield file refs.
        client = FakeAsyncWebClient()
        client.script("conversations_list", _channels("C01", ""))
        client.script(
            "conversations_history",
            _history(
                [
                    {
                        "ts": "100.0",
                        "user": "U",
                        "text": "p",
                        "reply_count": 1,
                        "thread_ts": "100.0",
                    }
                ]
            ),
        )
        client.script(
            "conversations_replies",
            _history(
                [
                    {"ts": "100.0", "user": "U", "text": "p"},
                    {
                        "ts": "101.0",
                        "user": "U",
                        "text": "reply",
                        "files": [
                            {
                                "id": "F2",
                                "name": "secret.py",
                                "mimetype": "text/x-python",
                            }
                        ],
                    },
                ]
            ),
        )
        cursor_state: dict[str, str] = {}
        refs = [
            r
            async for r in discover_via_conversations(
                client=client,
                source_id="src",
                team_id="T",
                cursor_state=cursor_state,
            )
        ]
        assert [r.path for r in refs] == [
            "slack://T/C01/100.0",
            "slack://T/C01/101.0",
            "slack://T/C01/101.0/files/F2",
        ]
