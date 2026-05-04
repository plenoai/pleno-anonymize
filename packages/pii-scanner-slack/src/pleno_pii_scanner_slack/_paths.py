"""Slack canonical path / cursor helpers.

The DocumentRef.path for Slack is the URI form documented in ADR-0007 §1:

    slack://T<team>/C<channel>/<ts>
    slack://T<team>/C<channel>/<ts>/files/F<file>

Cursors are JSON `{channel_id: oldest_ts}`. We keep both forms in one
module so the parsing/serialization is round-trip-tested in one place
and so the connector code reads as `from . import _paths` rather than
sprinkling f-strings throughout.
"""

from __future__ import annotations

import json
from collections.abc import Mapping


def message_path(team_id: str, channel_id: str, ts: str) -> str:
    """Build the canonical slack:// path for a message."""
    return f"slack://{team_id}/{channel_id}/{ts}"


def file_path(team_id: str, channel_id: str, ts: str, file_id: str) -> str:
    """Build the canonical slack:// path for a file attached to a message.

    File-only paths (files outside any message — files.list crawl) reuse
    the same shape with `ts=parent_message_ts` when the file is shared in
    a thread, or `ts="-"` when there is no enclosing message context.
    """
    return f"slack://{team_id}/{channel_id}/{ts}/files/{file_id}"


def dump_cursor(per_channel: Mapping[str, str]) -> str:
    """Serialize a `{channel_id: oldest_ts}` map for CheckpointStore.

    Sorted keys keep the serialization deterministic so a checkpoint
    diff between two scans only changes when actual cursor positions
    change, not because dict iteration order shifted.
    """
    return json.dumps(dict(sorted(per_channel.items())), separators=(",", ":"))


def load_cursor(cursor: str | None) -> dict[str, str]:
    """Inverse of `dump_cursor`. Treats None / empty as a fresh scan.

    Malformed JSON or non-string values raise ValueError — corrupted
    checkpoints must surface loudly so the operator can decide whether
    to drop the checkpoint and re-scan.
    """
    if not cursor:
        return {}
    parsed = json.loads(cursor)
    if not isinstance(parsed, dict):
        raise ValueError(
            f"slack cursor must decode to a JSON object, got {type(parsed).__name__}"
        )
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError("slack cursor entries must be string -> string")
        out[k] = v
    return out


__all__ = ["dump_cursor", "file_path", "load_cursor", "message_path"]
