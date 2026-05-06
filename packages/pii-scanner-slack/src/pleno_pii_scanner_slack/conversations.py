"""Per-team conversations.history discover path (xoxb / xoxp tokens).

This is the per-workspace discovery surface — the one the bot/user-token
case must use. We paginate `conversations.list` to enumerate channels
the token can see, then `conversations.history` per channel with `oldest`
set from the resume cursor, then `conversations.replies` for each thread
parent that has `reply_count > 0`.

The xoxa Discovery API path lives in `discovery.py` and is structurally
different (one cursor across all workspaces, no per-channel join).
Splitting the two modules keeps each function readable and lets us hold
each path to a separate test fixture without one mock leaking into the
other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any

from pleno_pii_scanner.sources.base import DocumentRef

from . import _paths, _rate
from ._files import is_text_like


# Channel types we ask Slack to enumerate. Slack accepts a comma-joined
# string here; including IM/MPIM is what makes user tokens reach DMs the
# user has opened. Bot tokens silently ignore IM/MPIM if not invited;
# Slack returns an empty page rather than an error.
_CHANNEL_TYPES = "public_channel,private_channel,im,mpim"

# Conservative page size for conversations.list — Slack docs recommend
# 200 over the default 100 for crawl workloads, and the response shape
# stays under 1MB for any realistic workspace.
_LIST_PAGE = 200

# History page size for conversations.history. Slack's documented max is
# 999 but the response shape gets unwieldy past 200; matching the list
# size keeps the per-page memory bound predictable.
_HISTORY_PAGE = 200


async def _paginate(
    *,
    method: Any,
    base_kwargs: Mapping[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Cursor-paginate `method`, yielding each `messages` / `channels` page.

    The slack-sdk async methods return SlackResponse objects that behave
    like Mappings — `response["channels"]` and `response["response_metadata"]`
    are the standard accessors. We re-call with `cursor=` until the
    server omits a non-empty `next_cursor`.
    """
    cursor: str | None = None
    while True:
        kwargs = dict(base_kwargs)
        if cursor:
            kwargs["cursor"] = cursor
        async with _rate.translate_slack_errors():
            page = await method(**kwargs)
        # slack-sdk's SlackResponse exposes the JSON via __getitem__ and
        # .get(); the test doubles below mirror that surface.
        yield dict(page.data) if hasattr(page, "data") else dict(page)
        next_cursor = ""
        meta = page.get("response_metadata") if hasattr(page, "get") else None
        if meta:
            next_cursor = meta.get("next_cursor", "") or ""
        if not next_cursor:
            return
        cursor = next_cursor


def _build_message_ref(
    *,
    source_id: str,
    team_id: str,
    channel_id: str,
    message: Mapping[str, Any],
    cursor_blob: str,
) -> DocumentRef:
    """Construct a DocumentRef for a single Slack message."""
    ts = str(message["ts"])
    user_id = message.get("user") or message.get("bot_id") or ""
    metadata: dict[str, str] = {
        "team_id": team_id,
        "channel_id": channel_id,
        "ts": ts,
        "_cursor": cursor_blob,
    }
    if user_id:
        metadata["user_id"] = str(user_id)
    if message.get("thread_ts"):
        metadata["thread_ts"] = str(message["thread_ts"])
    return DocumentRef(
        source_id=source_id,
        source_kind="slack",
        path=_paths.message_path(team_id, channel_id, ts),
        native_url=f"https://app.slack.com/client/{team_id}/{channel_id}/p{ts.replace('.', '')}",
        content_type="text/plain",
        size=len(message.get("text", "").encode("utf-8")),
        metadata=metadata,
    )


def _build_file_ref(
    *,
    source_id: str,
    team_id: str,
    channel_id: str,
    parent_ts: str,
    file_obj: Mapping[str, Any],
    cursor_blob: str,
) -> DocumentRef:
    """Construct a DocumentRef for a file attached to a Slack message."""
    file_id = str(file_obj["id"])
    name = file_obj.get("name") or file_obj.get("title") or ""
    mimetype = file_obj.get("mimetype")
    # text-like files get a `text/plain` content_type so the
    # ContentExtractor short-circuits into the text path; everything
    # else stays as the upstream mimetype (or octet-stream if Slack
    # didn't send one).
    if is_text_like(mimetype, name):
        content_type = "text/plain"
    else:
        content_type = mimetype or "application/octet-stream"
    metadata = {
        "team_id": team_id,
        "channel_id": channel_id,
        "file_id": file_id,
        "parent_ts": parent_ts,
        "_cursor": cursor_blob,
    }
    if name:
        metadata["filename"] = str(name)
    if file_obj.get("url_private_download"):
        url_private = str(file_obj["url_private_download"])
    else:
        url_private = str(file_obj.get("url_private", ""))
    if url_private:
        metadata["url_private_download"] = url_private
    return DocumentRef(
        source_id=source_id,
        source_kind="slack",
        path=_paths.file_path(team_id, channel_id, parent_ts, file_id),
        native_url=str(file_obj.get("permalink", "")) or None,
        content_type=content_type,
        size=int(file_obj.get("size", 0)) or None,
        metadata=metadata,
    )


def _yield_files_for_message(
    message: Mapping[str, Any],
) -> Iterable[Mapping[str, Any]]:
    """Iterate the files: [...] array, defending against nulls."""
    files = message.get("files") or ()
    for f in files:
        if isinstance(f, Mapping) and f.get("id"):
            yield f


async def discover_via_conversations(
    *,
    client: Any,
    source_id: str,
    team_id: str,
    cursor_state: dict[str, str],
    include_threads: bool = True,
    include_files: bool = True,
) -> AsyncIterator[DocumentRef]:
    """Yield DocumentRef for every message (and file) the token can see.

    `cursor_state` is mutated in place so the cursor blob attached to
    each yielded ref reflects the latest known oldest_ts per channel.
    The scheduler stores the most recent ref's `_cursor` and round-trips
    it back via `discover(cursor=...)` on the next scan.
    """
    base_list = {
        "limit": _LIST_PAGE,
        "types": _CHANNEL_TYPES,
        "exclude_archived": True,
    }
    async for list_page in _paginate(
        method=client.conversations_list, base_kwargs=base_list
    ):
        for channel in list_page.get("channels", []):
            channel_id = str(channel["id"])
            oldest = cursor_state.get(channel_id, "0")
            base_history = {
                "channel": channel_id,
                "limit": _HISTORY_PAGE,
                "oldest": oldest,
                "inclusive": False,
            }
            async for hist_page in _paginate(
                method=client.conversations_history, base_kwargs=base_history
            ):
                messages = hist_page.get("messages", [])
                # Slack returns history newest-first. Iterate oldest-first
                # so the cursor advances monotonically and a kill -9
                # mid-page leaves the channel resumable from the last
                # successfully processed ts.
                for message in sorted(messages, key=lambda m: float(m.get("ts", 0))):
                    ts = str(message["ts"])
                    cursor_state[channel_id] = ts
                    cursor_blob = _paths.dump_cursor(cursor_state)
                    yield _build_message_ref(
                        source_id=source_id,
                        team_id=team_id,
                        channel_id=channel_id,
                        message=message,
                        cursor_blob=cursor_blob,
                    )
                    if include_files:
                        for file_obj in _yield_files_for_message(message):
                            yield _build_file_ref(
                                source_id=source_id,
                                team_id=team_id,
                                channel_id=channel_id,
                                parent_ts=ts,
                                file_obj=file_obj,
                                cursor_blob=cursor_blob,
                            )
                    if (
                        include_threads
                        and int(message.get("reply_count", 0) or 0) > 0
                        and message.get("thread_ts") in (None, ts)
                    ):
                        async for reply_ref in _yield_thread_replies(
                            client=client,
                            source_id=source_id,
                            team_id=team_id,
                            channel_id=channel_id,
                            thread_ts=ts,
                            cursor_state=cursor_state,
                            include_files=include_files,
                        ):
                            yield reply_ref


async def _yield_thread_replies(
    *,
    client: Any,
    source_id: str,
    team_id: str,
    channel_id: str,
    thread_ts: str,
    cursor_state: dict[str, str],
    include_files: bool,
) -> AsyncIterator[DocumentRef]:
    """Yield every reply in `thread_ts` (skipping the parent which the caller already emitted)."""
    base = {
        "channel": channel_id,
        "ts": thread_ts,
        "limit": _HISTORY_PAGE,
    }
    async for page in _paginate(method=client.conversations_replies, base_kwargs=base):
        messages = page.get("messages", [])
        for message in sorted(messages, key=lambda m: float(m.get("ts", 0))):
            ts = str(message["ts"])
            if ts == thread_ts:
                # First entry is always the parent — skip; the caller
                # emitted it already and we don't want a duplicate
                # DocumentRef path.
                continue
            cursor_state[channel_id] = ts
            cursor_blob = _paths.dump_cursor(cursor_state)
            yield _build_message_ref(
                source_id=source_id,
                team_id=team_id,
                channel_id=channel_id,
                message=message,
                cursor_blob=cursor_blob,
            )
            if include_files:
                for file_obj in _yield_files_for_message(message):
                    yield _build_file_ref(
                        source_id=source_id,
                        team_id=team_id,
                        channel_id=channel_id,
                        parent_ts=ts,
                        file_obj=file_obj,
                        cursor_blob=cursor_blob,
                    )


__all__ = [
    "discover_via_conversations",
]
