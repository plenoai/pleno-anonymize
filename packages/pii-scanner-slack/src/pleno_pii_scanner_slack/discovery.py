"""Org-wide Discovery API path (xoxa Enterprise Grid tokens).

Discovery API exposes ALL conversations across every workspace in the
Enterprise Grid behind one auth — the Tier 3 rate-limit avoidance path
called out in ADR-0007 §13. The API surface is parallel-but-not-equal to
conversations.*:

    discovery.enterprise.info       -> org-level metadata
    discovery.conversations.list    -> {team_id, channel_id} for every channel
    discovery.conversations.history -> messages with team_id pre-filled
    discovery.conversations.recent  -> incremental (last N seconds), unused here

We deliberately *do* call enterprise.info up front: scoping refs by the
real `enterprise_id` (rather than guessing from the token's `team_id`,
which on org-wide tokens is often unset) gives the FindingsStore a
stable tenant key for envelope encryption (#11).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from pleno_pii_scanner.sources.base import DocumentRef

from . import _paths, _rate
from ._files import is_text_like


_LIST_PAGE = 1000  # discovery.conversations.list accepts up to 1000
_HISTORY_PAGE = 1000  # discovery.conversations.history caps at 1000


async def _paginate(
    *,
    method: Any,
    base_kwargs: Mapping[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Cursor-paginate a discovery.* method.

    Discovery uses the same `next_cursor` shape as the Web API — the
    pagination helper is duplicated here (rather than imported from
    conversations.py) so the two paths can evolve independently if Slack
    diverges them, and so each module reads top-to-bottom without
    cross-imports for trivial helpers.
    """
    cursor: str | None = None
    while True:
        kwargs = dict(base_kwargs)
        if cursor:
            kwargs["cursor"] = cursor
        async with _rate.translate_slack_errors():
            page = await method(**kwargs)
        yield dict(page.data) if hasattr(page, "data") else dict(page)
        next_cursor = ""
        meta = (page.get("response_metadata") if hasattr(page, "get") else None)
        if meta:
            next_cursor = meta.get("next_cursor", "") or ""
        if not next_cursor:
            return
        cursor = next_cursor


def _build_discovery_message_ref(
    *,
    source_id: str,
    team_id: str,
    channel_id: str,
    message: Mapping[str, Any],
    cursor_blob: str,
) -> DocumentRef:
    ts = str(message["ts"])
    user_id = message.get("user") or ""
    metadata: dict[str, str] = {
        "team_id": team_id,
        "channel_id": channel_id,
        "ts": ts,
        "discovery": "1",
        "_cursor": cursor_blob,
    }
    if user_id:
        metadata["user_id"] = str(user_id)
    return DocumentRef(
        source_id=source_id,
        source_kind="slack",
        path=_paths.message_path(team_id, channel_id, ts),
        native_url=None,  # discovery.* messages have no public Slack URL
        content_type="text/plain",
        size=len(message.get("text", "").encode("utf-8")),
        metadata=metadata,
    )


def _build_discovery_file_ref(
    *,
    source_id: str,
    team_id: str,
    channel_id: str,
    parent_ts: str,
    file_obj: Mapping[str, Any],
    cursor_blob: str,
) -> DocumentRef:
    file_id = str(file_obj["id"])
    name = file_obj.get("name") or file_obj.get("title") or ""
    mimetype = file_obj.get("mimetype")
    if is_text_like(mimetype, name):
        content_type = "text/plain"
    else:
        content_type = mimetype or "application/octet-stream"
    metadata = {
        "team_id": team_id,
        "channel_id": channel_id,
        "file_id": file_id,
        "parent_ts": parent_ts,
        "discovery": "1",
        "_cursor": cursor_blob,
    }
    if name:
        metadata["filename"] = str(name)
    if file_obj.get("url_private_download"):
        metadata["url_private_download"] = str(file_obj["url_private_download"])
    elif file_obj.get("url_private"):
        metadata["url_private_download"] = str(file_obj["url_private"])
    return DocumentRef(
        source_id=source_id,
        source_kind="slack",
        path=_paths.file_path(team_id, channel_id, parent_ts, file_id),
        native_url=str(file_obj.get("permalink", "")) or None,
        content_type=content_type,
        size=int(file_obj.get("size", 0)) or None,
        metadata=metadata,
    )


async def fetch_enterprise_id(client: Any) -> str:
    """Return the enterprise_id for the org behind the xoxa token.

    Falls back to the literal "unknown" if the API hides the field — we
    do not want a None to propagate into DocumentRef metadata where
    downstream code assumes string values.
    """
    async with _rate.translate_slack_errors():
        info = await client.discovery_enterprise_info()
    enterprise = info.get("enterprise") if hasattr(info, "get") else None
    if isinstance(enterprise, Mapping):
        eid = enterprise.get("id")
        if isinstance(eid, str) and eid:
            return eid
    return "unknown"


async def discover_via_discovery(
    *,
    client: Any,
    source_id: str,
    cursor_state: dict[str, str],
    include_files: bool = True,
) -> AsyncIterator[DocumentRef]:
    """Yield DocumentRef for every channel × message in the Enterprise Grid.

    `cursor_state` is keyed `<team_id>:<channel_id>` so a single org with
    150 workspaces and 40k channels stays inside one checkpoint blob.
    The conversations.history path keys by `channel_id` alone; the
    discovery path differs because the same channel_id can be reused
    across separate workspaces in an Enterprise Grid (rare, but legal).
    """
    base_list = {"limit": _LIST_PAGE}
    async for list_page in _paginate(
        method=client.discovery_conversations_list, base_kwargs=base_list
    ):
        for conv in list_page.get("channels", []):
            channel_id = str(conv["id"])
            team_id = str(conv.get("team", "") or conv.get("team_id", ""))
            key = f"{team_id}:{channel_id}"
            oldest = cursor_state.get(key, "0")
            base_history = {
                "team": team_id,
                "channel": channel_id,
                "limit": _HISTORY_PAGE,
                "oldest": oldest,
            }
            async for hist_page in _paginate(
                method=client.discovery_conversations_history,
                base_kwargs=base_history,
            ):
                messages = hist_page.get("messages", [])
                for message in sorted(messages, key=lambda m: float(m.get("ts", 0))):
                    ts = str(message["ts"])
                    cursor_state[key] = ts
                    cursor_blob = _paths.dump_cursor(cursor_state)
                    yield _build_discovery_message_ref(
                        source_id=source_id,
                        team_id=team_id,
                        channel_id=channel_id,
                        message=message,
                        cursor_blob=cursor_blob,
                    )
                    if include_files:
                        for file_obj in message.get("files") or ():
                            if (
                                isinstance(file_obj, Mapping)
                                and file_obj.get("id")
                            ):
                                yield _build_discovery_file_ref(
                                    source_id=source_id,
                                    team_id=team_id,
                                    channel_id=channel_id,
                                    parent_ts=ts,
                                    file_obj=file_obj,
                                    cursor_blob=cursor_blob,
                                )


__all__ = [
    "discover_via_discovery",
    "fetch_enterprise_id",
]
