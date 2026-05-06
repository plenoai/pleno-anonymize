"""Microsoft Teams SourceConnector — Graph delta query + workload identity.

Pipeline:

  1. Acquire bearer via /oauth2/v2.0/token (client_secret OR
     workload-identity jwt-bearer assertion).
  2. Enumerate teams (allowlist or GET /v1.0/teams).
  3. Per team: GET /v1.0/teams/{team-id}/channels.
  4. Per channel: GET /v1.0/teams/{team-id}/channels/{channel-id}/messages/delta
     (initial) or stored @odata.deltaLink (resume).
  5. Yield one DocumentRef per message (optionally per reply too).
  6. Persist per-channel deltaLink to the Cursor JSON map for the
     next run.

The Cursor is `str` (per the core API). We JSON-encode the
`{channel_id: deltaLink}` map; a stale or unparseable cursor falls
back to a fresh delta (i.e. one full re-walk from now), never a
crash. That degrades gracefully across format changes.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401 — referenced in fetch() return type
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


_LOGIN_BASE = "https://login.microsoftonline.com"
_GRAPH_BASE = "https://graph.microsoft.com"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_JWT_BEARER_GRANT = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# Strip HTML tags from message bodies. Teams returns either
# `contentType=text` (plain) or `contentType=html` (HTML body) and
# we want consistent text for downstream PII regex matching.
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Refresh token this many seconds before its declared expiry, so a
# long discover() pass cannot trip a 401 mid-walk.
_TOKEN_REFRESH_LEAD_SECS = 30


@dataclass(frozen=True, slots=True)
class MsTeamsConfig:
    """Construction config for `MsTeamsConnector`.

    Exactly one of `client_secret` / `federated_token` must be set.
    `federated_token` is a signed JWT assertion (e.g. read from the
    AKS workload-identity projected volume, or the GitHub Actions
    OIDC endpoint) and is exchanged via the AAD jwt-bearer grant.
    """

    tenant_id: str
    client_id: str
    client_secret: str | None = None
    federated_token: str | None = None
    teams: tuple[str, ...] = ()
    include_replies: bool = True
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not self.client_id:
            raise ValueError("client_id must be non-empty")
        has_secret = bool(self.client_secret)
        has_federated = bool(self.federated_token)
        if has_secret == has_federated:
            # Both set, or neither set. Either is a configuration
            # mistake we want to surface loudly rather than silently
            # picking one.
            raise ValueError(
                "exactly one of client_secret / federated_token must be set"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Tenant + client are not strictly secret, but we still hash
        # so two configs with the same (tenant, client, teams) collapse
        # to a stable id without any chance of secret leakage.
        import hashlib

        h = hashlib.sha256()
        h.update(self.tenant_id.encode())
        h.update(b"\0")
        h.update(self.client_id.encode())
        for t in sorted(self.teams):
            h.update(b"\0")
            h.update(t.encode())
        return f"msteams:{h.hexdigest()[:16]}"


class MsTeamsConnector:
    """Read-only SourceConnector for Microsoft Teams chat content."""

    kind = "msteams"

    def __init__(
        self,
        config: MsTeamsConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        # Token cache. None until first acquisition; tuple of
        # (access_token, expires_at_monotonic).
        self._token: tuple[str, float] | None = None
        self._token_lock = asyncio.Lock()
        # Cache message bodies discovered during discover() so fetch()
        # does not need to re-issue Graph calls for every yield.
        self._messages: dict[str, dict[str, Any]] = {}
        # Per-channel deltaLink seen during this run; promoted to the
        # next-run cursor when discover() finishes.
        self._delta_links: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        prior = _decode_cursor(cursor)
        teams = await self._list_teams()
        for team in teams:
            team_id = team["id"]
            team_name = team.get("displayName", team_id)
            channels = await self._list_channels(team_id)
            for channel in channels:
                channel_id = channel["id"]
                channel_name = channel.get("displayName", channel_id)
                base_path = f"{team_name}/{channel_name}"
                resume = prior.get(channel_id)
                messages, delta_link = await self._delta_messages(
                    team_id, channel_id, resume_link=resume
                )
                if delta_link:
                    self._delta_links[channel_id] = delta_link
                elif resume:
                    # Server returned no nextLink AND no deltaLink (rare
                    # but documented for empty pages). Preserve the prior
                    # link so the next run does not re-walk history.
                    self._delta_links[channel_id] = resume
                for msg in messages:
                    msg_id = msg.get("id")
                    if not msg_id:
                        continue
                    full = f"{base_path}/{msg_id}"
                    if filter.include and not _matches_any(full, filter.include):
                        continue
                    if filter.exclude and _matches_any(full, filter.exclude):
                        continue
                    self._messages[full] = msg
                    yield self._ref_for(msg, full, team_id, channel_id)
                    if not self._config.include_replies:
                        continue
                    replies = await self._list_replies(team_id, channel_id, msg_id)
                    for reply in replies:
                        reply_id = reply.get("id")
                        if not reply_id:
                            continue
                        full_reply = f"{full}/replies/{reply_id}"
                        if filter.include and not _matches_any(
                            full_reply, filter.include
                        ):
                            continue
                        if filter.exclude and _matches_any(full_reply, filter.exclude):
                            continue
                        self._messages[full_reply] = reply
                        yield self._ref_for(
                            reply,
                            full_reply,
                            team_id,
                            channel_id,
                            parent_message_id=msg_id,
                        )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        msg = self._messages.get(ref.path)
        if msg is None:
            return
        text = _render_message(msg)
        from_obj = msg.get("from") or {}
        user = from_obj.get("user") if isinstance(from_obj, Mapping) else None
        display = ""
        if isinstance(user, Mapping):
            display = str(user.get("displayName", "") or "")
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            content_hash=str(msg.get("etag", msg.get("id", ""))) or None,
            extra={
                "from_display_name": display,
                "createdDateTime": str(msg.get("createdDateTime", "")),
            },
        )

    def cursor_after_run(self) -> Cursor | None:
        """Promoted by the scheduler after a successful discover() pass."""
        if not self._delta_links:
            return None
        return _encode_cursor(self._delta_links)

    async def close(self) -> None:
        self._messages.clear()
        self._delta_links.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- internals ----------------------------------------------------

    async def _bearer(self) -> str:
        """Return a valid bearer token, acquiring or refreshing as needed."""
        now = time.monotonic()
        cached = self._token
        if cached is not None and cached[1] - _TOKEN_REFRESH_LEAD_SECS > now:
            return cached[0]
        async with self._token_lock:
            # Recheck under the lock — another coroutine may have
            # refreshed while we waited.
            cached = self._token
            now = time.monotonic()
            if cached is not None and cached[1] - _TOKEN_REFRESH_LEAD_SECS > now:
                return cached[0]
            access_token, expires_in = await self._acquire_token()
            expires_at = time.monotonic() + float(expires_in)
            self._token = (access_token, expires_at)
            return access_token

    async def _acquire_token(self) -> tuple[str, float]:
        url = f"{_LOGIN_BASE}/{self._config.tenant_id}/oauth2/v2.0/token"
        data: dict[str, str] = {
            "client_id": self._config.client_id,
            "scope": _GRAPH_SCOPE,
            "grant_type": "client_credentials",
        }
        if self._config.client_secret:
            data["client_secret"] = self._config.client_secret
        else:
            # Workload-identity / federated path. The assertion is a
            # JWT signed by the federated IdP (AKS, GH Actions OIDC).
            data["client_assertion_type"] = _JWT_BEARER_GRANT
            data["client_assertion"] = self._config.federated_token or ""
        resp = await self._client.post(url, data=data)
        resp.raise_for_status()
        body = resp.json()
        token = str(body["access_token"])
        # AAD returns expires_in as int seconds. Default 3600 if missing
        # so a malformed-but-2xx response still gets cached briefly.
        expires_in = float(body.get("expires_in", 3600))
        return token, expires_in

    async def _graph_get(
        self,
        url: str,
        *,
        absolute: bool = False,
    ) -> dict[str, Any]:
        token = await self._bearer()
        headers = {"Authorization": f"Bearer {token}"}
        target = url if absolute else f"{_GRAPH_BASE}{url}"
        resp = await self._client.get(target, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _list_teams(self) -> list[dict[str, Any]]:
        if self._config.teams:
            return [{"id": t, "displayName": t} for t in self._config.teams]
        body = await self._graph_get("/v1.0/teams")
        value = body.get("value", []) or []
        return [v for v in value if isinstance(v, Mapping) and v.get("id")]

    async def _list_channels(self, team_id: str) -> list[dict[str, Any]]:
        body = await self._graph_get(f"/v1.0/teams/{team_id}/channels")
        value = body.get("value", []) or []
        return [v for v in value if isinstance(v, Mapping) and v.get("id")]

    async def _delta_messages(
        self,
        team_id: str,
        channel_id: str,
        *,
        resume_link: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Walk the delta query, following @odata.nextLink to the end.

        Returns ``(messages, deltaLink)``. The deltaLink is the URL
        the *next* run uses to ask only for changes since this run.
        """
        if resume_link:
            url = resume_link
            absolute = True
        else:
            url = f"/v1.0/teams/{team_id}/channels/{channel_id}/messages/delta"
            absolute = False
        collected: list[dict[str, Any]] = []
        delta_link: str | None = None
        while True:
            body = await self._graph_get(url, absolute=absolute)
            for item in body.get("value", []) or []:
                if isinstance(item, Mapping):
                    collected.append(dict(item))
            next_link = body.get("@odata.nextLink")
            delta_link = body.get("@odata.deltaLink") or delta_link
            if next_link:
                url = str(next_link)
                absolute = True
                continue
            break
        return collected, delta_link

    async def _list_replies(
        self, team_id: str, channel_id: str, message_id: str
    ) -> list[dict[str, Any]]:
        url = (
            f"/v1.0/teams/{team_id}/channels/{channel_id}/messages/{message_id}/replies"
        )
        absolute = False
        out: list[dict[str, Any]] = []
        while True:
            body = await self._graph_get(url, absolute=absolute)
            for item in body.get("value", []) or []:
                if isinstance(item, Mapping):
                    out.append(dict(item))
            next_link = body.get("@odata.nextLink")
            if not next_link:
                break
            url = str(next_link)
            absolute = True
        return out

    def _ref_for(
        self,
        msg: Mapping[str, Any],
        path: str,
        team_id: str,
        channel_id: str,
        *,
        parent_message_id: str | None = None,
    ) -> DocumentRef:
        body = msg.get("body") or {}
        body_text = ""
        if isinstance(body, Mapping):
            body_text = str(body.get("content", "") or "")
        size = len(body_text)
        meta: dict[str, str] = {
            "team_id": team_id,
            "channel_id": channel_id,
            "message_id": str(msg.get("id", "")),
        }
        if parent_message_id:
            meta["parent_message_id"] = parent_message_id
            meta["kind"] = "reply"
        else:
            meta["kind"] = "message"
        etag = msg.get("etag") or msg.get("id")
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=path,
            content_type="text/plain",
            size=size,
            etag=str(etag) if etag is not None else None,
            metadata=meta,
        )


# --- helpers ---------------------------------------------------------


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _strip_html(value: str) -> str:
    """Strip HTML tags and unescape entities for clean PII matching."""
    return html.unescape(_HTML_TAG_RE.sub("", value))


def _render_message(msg: Mapping[str, Any]) -> str:
    parts: list[str] = []
    from_obj = msg.get("from") or {}
    user = from_obj.get("user") if isinstance(from_obj, Mapping) else None
    if isinstance(user, Mapping):
        parts.append(f"from={user.get('displayName', '')}")
    parts.append(f"id={msg.get('id', '')}")
    parts.append(f"createdDateTime={msg.get('createdDateTime', '')}")
    body = msg.get("body") or {}
    if isinstance(body, Mapping):
        content = str(body.get("content", "") or "")
        ctype = str(body.get("contentType", "text") or "text").lower()
        if ctype == "html":
            content = _strip_html(content)
        parts.append(f"content={content}")
    for att in msg.get("attachments", []) or []:
        if not isinstance(att, Mapping):
            continue
        url = att.get("contentUrl") or att.get("name") or ""
        if url:
            parts.append(f"attachment={url}")
    return "\n".join(parts)


def _decode_cursor(cursor: Cursor | None) -> dict[str, str]:
    if not cursor:
        return {}
    try:
        decoded = json.loads(cursor)
    except (ValueError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(k): str(v) for k, v in decoded.items()}


def _encode_cursor(state: Mapping[str, str]) -> str:
    return json.dumps(dict(state), sort_keys=True)


# --- factory / spec --------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "tenant_id" not in config:
        raise ValueError("msteams connector config requires 'tenant_id'")
    if "client_id" not in config:
        raise ValueError("msteams connector config requires 'client_id'")
    return MsTeamsConnector(
        MsTeamsConfig(
            tenant_id=str(config["tenant_id"]),
            client_id=str(config["client_id"]),
            client_secret=(
                str(config["client_secret"])
                if config.get("client_secret") is not None
                else None
            ),
            federated_token=(
                str(config["federated_token"])
                if config.get("federated_token") is not None
                else None
            ),
            teams=tuple(str(t) for t in config.get("teams", ())),
            include_replies=bool(config.get("include_replies", True)),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="msteams",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=True,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=(
        "Group.Read.All",
        "Channel.ReadBasic.All",
        "ChannelMessage.Read.All",
    ),
    description=(
        "Microsoft Teams SourceConnector. Walks teams + channels via "
        "Graph delta query for incremental scans; supports both client-"
        "secret and workload-identity (federated jwt-bearer) credential "
        "modes. Optionally enumerates per-message replies."
    ),
)


__all__ = ["MsTeamsConfig", "MsTeamsConnector", "SPEC"]
