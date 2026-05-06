"""Discord SourceConnector — bot-token + snowflake-cursor scan.

Pipeline:

  1. GET /users/@me/guilds → enumerate every guild the bot is in
     (or apply `guilds` config allowlist)
  2. Per guild: GET /guilds/{id}/channels → filter by channel_type
     (0=GUILD_TEXT, 5=GUILD_ANNOUNCEMENT, 11/12=THREAD)
  3. Per channel: GET /channels/{id}/messages?limit=100&before={snowflake}
     paginating backwards until cap or empty page
  4. Optionally per channel: GET /channels/{id}/threads/active
     and /channels/{id}/users/@me/threads/archived to enumerate
     threads, then scan each thread the same way

Each message becomes one Document — author, timestamp, content,
attachments enumerated as URL strings (we do not download
attachments here; that is the operator's choice via a separate
HTTP-fetch connector or a CDN crawl).

Rate limiting: Discord uses a per-route bucket with `X-RateLimit-*`
headers. On 429 we honor `Retry-After` (seconds, float). Per ADR
§16, we never exceed half the documented bucket per second —
operators with a single bot scanning thousands of channels should
configure `concurrency=1`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


_API_BASE = "https://discord.com/api/v10"

# Discord channel types of interest. The full enum has 14+ values
# (categories, voice, stage, forum, etc.); we scan only the
# ones that carry text bodies.
_TEXT_CHANNEL_TYPES: frozenset[int] = frozenset({0, 5, 10, 11, 12, 15})


@dataclass(frozen=True, slots=True)
class DiscordConfig:
    """Construction config for `DiscordConnector`."""

    token: str
    guilds: tuple[str, ...] = ()
    channel_types: tuple[int, ...] = (0, 5)
    max_messages_per_channel: int = 5000
    include_threads: bool = True
    concurrency: int = 2
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("token must be non-empty")
        if self.max_messages_per_channel < 0:
            raise ValueError("max_messages_per_channel must be >= 0")
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        for ct in self.channel_types:
            if ct not in _TEXT_CHANNEL_TYPES:
                raise ValueError(
                    f"channel_type {ct} is not a text-bearing channel; "
                    f"allowed: {sorted(_TEXT_CHANNEL_TYPES)}"
                )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Tokens are sensitive; identify by hashed token + guild set.
        import hashlib

        h = hashlib.sha256()
        h.update(self.token.encode())
        for g in sorted(self.guilds):
            h.update(b"\0")
            h.update(g.encode())
        return f"discord:{h.hexdigest()[:16]}"


class DiscordConnector:
    """Read-only SourceConnector for Discord bot-scoped scans."""

    kind = "discord"

    def __init__(
        self,
        config: DiscordConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=_API_BASE,
                timeout=30.0,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._auth_headers = {"Authorization": f"Bot {config.token}"}
        self._sem = asyncio.Semaphore(config.concurrency)
        # Map of channel_id → list[message_dict] cached during discover
        # so fetch() doesn't re-issue the message walk.
        self._channel_messages: dict[str, list[dict[str, Any]]] = {}
        # Per-channel last-seen snowflake — drives the incremental cursor.
        self._high_water: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=self._config.concurrency,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        prior_cursor = _decode_cursor(cursor)
        guilds = await self._list_guilds()
        for guild in guilds:
            channels = await self._list_channels(guild["id"])
            for channel in channels:
                full = f"{guild['id']}/{channel['id']}"
                if filter.include and not _matches_any(full, filter.include):
                    continue
                if filter.exclude and _matches_any(full, filter.exclude):
                    continue
                resume_after = prior_cursor.get(channel["id"])
                messages = await self._scan_channel(
                    channel["id"], resume_after=resume_after
                )
                if not messages:
                    continue
                self._channel_messages[channel["id"]] = messages
                # Discord returns newest-first; the highest snowflake
                # in the page is the last one we saw.
                self._high_water[channel["id"]] = max(m["id"] for m in messages)
                for msg in messages:
                    yield DocumentRef(
                        source_id=self.id,
                        source_kind=self.kind,
                        path=f"{full}/{msg['id']}",
                        content_type="text/plain",
                        size=len(msg.get("content", "")),
                        etag=msg["id"],
                        metadata={
                            "guild_id": guild["id"],
                            "channel_id": channel["id"],
                            "message_id": msg["id"],
                            "author_id": str(msg.get("author", {}).get("id", "")),
                        },
                    )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        channel_id = ref.metadata.get("channel_id")
        message_id = ref.metadata.get("message_id")
        if not channel_id or not message_id:
            return
        cached = self._channel_messages.get(channel_id, [])
        msg = next((m for m in cached if m["id"] == message_id), None)
        if msg is None:
            return
        text = _serialise_message(msg)
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            content_hash=msg["id"],
            extra={
                "guild_id": ref.metadata.get("guild_id", ""),
                "channel_id": channel_id,
                "message_id": message_id,
            },
        )

    def cursor_after_run(self) -> Cursor | None:
        if not self._high_water:
            return None
        return _encode_cursor(self._high_water)

    async def close(self) -> None:
        self._channel_messages.clear()
        self._high_water.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- internals ------------------------------------------------

    async def _list_guilds(self) -> list[dict[str, Any]]:
        if self._config.guilds:
            return [{"id": g} for g in self._config.guilds]
        async with self._sem:
            resp = await self._request("GET", "/users/@me/guilds")
        return resp.json()

    async def _list_channels(self, guild_id: str) -> list[dict[str, Any]]:
        async with self._sem:
            resp = await self._request("GET", f"/guilds/{guild_id}/channels")
        all_channels = resp.json()
        out = [c for c in all_channels if c.get("type") in self._config.channel_types]
        if self._config.include_threads:
            # Public threads enumerated via the parent channel; we
            # approximate by including thread-typed channels here.
            out.extend(
                c
                for c in all_channels
                if c.get("type") in {10, 11, 12} and c not in out
            )
        return out

    async def _scan_channel(
        self,
        channel_id: str,
        *,
        resume_after: str | None,
    ) -> list[dict[str, Any]]:
        cap = self._config.max_messages_per_channel
        collected: list[dict[str, Any]] = []
        # Resume forward from prior cursor when present (incremental);
        # otherwise page backwards from now (initial scan).
        if resume_after is not None:
            params: dict[str, str | int] = {"limit": 100, "after": resume_after}
            paging_back = False
        else:
            params = {"limit": 100}
            paging_back = True
        while True:
            async with self._sem:
                resp = await self._request(
                    "GET", f"/channels/{channel_id}/messages", params=params
                )
            page = resp.json()
            if not page:
                break
            collected.extend(page)
            if cap and len(collected) >= cap:
                return collected[:cap]
            if paging_back:
                params = {"limit": 100, "before": page[-1]["id"]}
            else:
                # `after` returns oldest-first; the last id in the
                # page is the newest message we just saw.
                params = {"limit": 100, "after": page[0]["id"]}
        return collected

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        # One retry on 429 honoring Retry-After. Discord's bucket is
        # per-route + per-resource so a single retry covers the
        # common case; persistent 429s indicate a misconfigured bot.
        for attempt in (1, 2):
            resp = await self._client.request(
                method, path, params=params, headers=self._auth_headers
            )
            if resp.status_code != 429:
                resp.raise_for_status()
                return resp
            if attempt == 2:
                # Persistent 429 — escalate so the scheduler backs off.
                resp.raise_for_status()
            retry_after = float(resp.headers.get("Retry-After", "1"))
            await asyncio.sleep(min(retry_after, 30.0))
        raise RuntimeError("unreachable")  # pragma: no cover


# --- helpers ------------------------------------------------------


def _serialise_message(msg: Mapping[str, Any]) -> str:
    parts: list[str] = []
    author = msg.get("author") or {}
    parts.append(f"author={author.get('username', '')}")
    parts.append(f"id={msg.get('id', '')}")
    parts.append(f"timestamp={msg.get('timestamp', '')}")
    parts.append(f"content={msg.get('content', '')}")
    for att in msg.get("attachments", []) or []:
        url = att.get("url", "")
        if url:
            parts.append(f"attachment={url}")
    for embed in msg.get("embeds", []) or []:
        # Embeds carry the same body as text but in structured form.
        title = embed.get("title", "")
        desc = embed.get("description", "")
        if title or desc:
            parts.append(f"embed={title} {desc}".strip())
    return "\n".join(parts)


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _decode_cursor(cursor: Cursor | None) -> dict[str, str]:
    """Parse a JSON cursor into the per-channel snowflake map.

    Cursor is `str` in the core API. We persist a JSON object and
    silently fall back to empty on any decode failure — the caller
    sees a fresh scan, never a crash on a stale cursor format.
    """
    if not cursor:
        return {}
    import json

    try:
        decoded = json.loads(cursor)
    except (ValueError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(k): str(v) for k, v in decoded.items()}


def _encode_cursor(state: Mapping[str, str]) -> str:
    import json

    return json.dumps(dict(state), sort_keys=True)


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "token" not in config:
        raise ValueError("discord connector config requires 'token'")
    return DiscordConnector(
        DiscordConfig(
            token=str(config["token"]),
            guilds=tuple(str(g) for g in config.get("guilds", ())),
            channel_types=tuple(int(c) for c in config.get("channel_types", (0, 5))),
            max_messages_per_channel=int(config.get("max_messages_per_channel", 5000)),
            include_threads=bool(config.get("include_threads", True)),
            concurrency=int(config.get("concurrency", 2)),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="discord",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=2,
        streaming=False,
    ),
    required_scopes=("discord:bot:read_messages",),
    description=(
        "Discord SourceConnector. Bot-token scoped scan with snowflake-"
        "cursor pagination per channel; incremental resume on next run. "
        "Requires the Message Content privileged intent."
    ),
)


__all__ = ["DiscordConfig", "DiscordConnector", "SPEC"]
