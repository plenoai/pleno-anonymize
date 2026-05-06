"""SlackConnector — main SourceConnector for Slack workspaces and Enterprise Grid.

Three token modes auto-detected from prefix:

    xoxb-...  → conversations.* path, scoped to the bot's home workspace
    xoxp-...  → conversations.* + files.* path, full user visibility
    xoxa-...  → discovery.*  path, every workspace in the Enterprise Grid

Cursor is JSON `{channel_id: oldest_ts}` (or `{team_id:channel_id: ts}` for
Discovery). `Capabilities.incremental = True`. Each `(team_id, channel_id)`
is a separate BucketKey for fine-grained AIMD throttling.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from pleno_pii_scanner.scheduler.rate_limit import BucketKey, RateLimited
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401  re-exported in fetch return annotation
    DocumentRef,
    Principal,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import _paths, _rate, conversations, discovery
from ._files import download_file
from .tokens import InvalidSlackTokenError, SlackTokenType, classify_token


# Type alias for the test-injection seam: factories can replace the
# AsyncWebClient with an in-memory double without touching the
# connector's real construction path.
ClientFactory = Any


@dataclass(frozen=True, slots=True)
class SlackConfig:
    """Construction config for `SlackConnector`.

    `token` is the only required field; the rest are knobs an operator
    might tweak per scan. `team_id` is optional for xoxb/xoxp tokens
    (auth.test resolves it on first use); xoxa tokens always need an
    `enterprise_id` which the connector fills in via discovery.enterprise.info.
    """

    token: str
    id: str | None = None
    team_id: str | None = None
    enterprise_id: str | None = None
    include_threads: bool = True
    include_files: bool = True
    fetch_user_principal: bool = True
    # The slack-sdk default timeout is 30s; we lower it to 20s because a
    # 30s history call on a busy channel still fits in our scheduler's
    # default per-fetch budget but a 30s file download on a slow CDN
    # blocks other connectors from making progress.
    request_timeout: float = 20.0

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # When the operator doesn't pin an id, derive one from the token's
        # *type* (not the secret itself!) plus team/enterprise hints. This
        # keeps two scans of the same workspace sharing a checkpoint without
        # ever embedding the token in the id.
        kind = classify_token(self.token).value
        scope = self.enterprise_id or self.team_id or "unknown"
        return f"slack:{kind}:{scope}"


class SlackConnector:
    """SourceConnector implementation for Slack.

    The dispatch on token type happens once at construction; both code
    paths reuse the same `_paths.dump_cursor` / `_paths.load_cursor` so
    that a `discover()` started under conversations.* and resumed under
    discovery.* (e.g. operator upgraded their plan to Enterprise Grid)
    is at worst a re-scan, never a corrupted checkpoint.
    """

    kind = "slack"

    def __init__(
        self,
        config: SlackConfig,
        *,
        client_factory: ClientFactory | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        # Surface bad tokens immediately so the registry factory can
        # report a clean error rather than an opaque 401 deep inside an
        # async generator. The classify_token call also normalizes
        # xoxa2-* into the ORG branch.
        self._token_type = classify_token(config.token)
        self.id = config.resolved_id()
        self._client_factory: ClientFactory = client_factory or _default_client_factory
        self._client: AsyncWebClient | None = None
        self._http: httpx.AsyncClient | None = http_client
        self._owned_http = http_client is None
        # Principal cache: user_id -> Principal. Bounded only by the
        # number of distinct authors in the scanned workspace, which is
        # small enough that we don't bother with eviction.
        self._principal_cache: dict[str, Principal] = {}
        # Resolved per-process metadata (team_id / enterprise_id) gets
        # filled in lazily on first discover(). Keeping this mutable but
        # private avoids re-doing auth.test on every call.
        self._resolved_team: str | None = config.team_id
        self._resolved_enterprise: str | None = config.enterprise_id

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=False,
            # Slack's per-(team, channel) Tier 4 budget bounds us more
            # than connector concurrency does; we cap fetch fan-out low
            # to leave the rate limiter visible headroom.
            max_concurrent_fetches=4,
            streaming=False,
        )

    def bucket_key(self, channel_id: str) -> BucketKey:
        """BucketKey for `(team_id, channel_id)` — exposed for the scheduler.

        Each channel is its own bucket because Slack's Tier 4 limit is
        per-method-per-workspace but in practice each channel hits the
        same limit independently. Surfacing the key here lets the
        scheduler attach the right AIMD bucket without parsing path strings.
        """
        team = self._resolved_team or self._config.team_id or "?"
        return BucketKey(
            connector_kind=self.kind,
            tenant_id=f"{team}:{channel_id}",
        )

    async def _ensure_client(self) -> AsyncWebClient:
        if self._client is None:
            self._client = self._client_factory(
                token=self._config.token,
                timeout=self._config.request_timeout,
            )
        return self._client

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._config.request_timeout)
        return self._http

    async def _resolve_team(self, client: AsyncWebClient) -> str:
        """Populate `_resolved_team` via auth.test if not already set."""
        if self._resolved_team:
            return self._resolved_team
        async with _rate.translate_slack_errors():
            info = await client.auth_test()
        team_id = info.get("team_id") if hasattr(info, "get") else None
        self._resolved_team = str(team_id) if team_id else "unknown"
        return self._resolved_team

    async def discover(
        self,
        filter: SourceFilter,  # noqa: ARG002 — server-side filtering is per-method
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate Slack messages + files according to the token type."""
        client = await self._ensure_client()
        cursor_state = _paths.load_cursor(cursor)
        if self._token_type is SlackTokenType.ORG:
            # Discovery API: enterprise.info first to lock down the
            # tenant id used in BucketKeys, then iterate every channel
            # across every workspace.
            if not self._resolved_enterprise:
                self._resolved_enterprise = await discovery.fetch_enterprise_id(client)
            async for ref in discovery.discover_via_discovery(
                client=client,
                source_id=self.id,
                cursor_state=cursor_state,
                include_files=self._config.include_files,
            ):
                yield ref
            return
        # xoxb / xoxp: per-workspace conversations.* path
        team_id = await self._resolve_team(client)
        async for ref in conversations.discover_via_conversations(
            client=client,
            source_id=self.id,
            team_id=team_id,
            cursor_state=cursor_state,
            include_threads=self._config.include_threads,
            include_files=self._config.include_files,
        ):
            yield ref

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Materialize a Document for `ref` — message body or file payload."""
        client = await self._ensure_client()
        meta = ref.metadata
        # File refs carry a `file_id`; everything else is a message ref.
        if "file_id" in meta:
            async for doc in self._fetch_file(client=client, ref=ref):
                yield doc
            return
        async for doc in self._fetch_message(client=client, ref=ref):
            yield doc

    async def _fetch_message(
        self,
        *,
        client: AsyncWebClient,
        ref: DocumentRef,
    ) -> AsyncIterator[Document]:
        meta = ref.metadata
        channel_id = meta["channel_id"]
        ts = meta["ts"]
        # We re-fetch via conversations.history (oldest=ts inclusive,
        # limit=1) rather than store the body on the ref — this keeps
        # discover() cheap (metadata-only as the protocol requires) and
        # lets fetch() pick up edits a user made between discover and
        # fetch. SDK call is wrapped to translate 429s.
        try:
            async with _rate.translate_slack_errors():
                resp = await client.conversations_history(
                    channel=channel_id,
                    oldest=ts,
                    inclusive=True,
                    limit=1,
                )
        except SlackApiError as exc:
            # `not_in_channel` for an archived/private channel is a
            # legitimate empty fetch — emit nothing and let the scan
            # continue. Other errors propagate.
            if _api_error_code(exc) in {"not_in_channel", "channel_not_found"}:
                return
            raise
        messages = resp.get("messages", []) if hasattr(resp, "get") else []
        if not messages:
            return
        message = messages[0]
        text = message.get("text") or ""
        principal = await self._principal_for(client, message.get("user"))
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            created_by=principal,
        )

    async def _fetch_file(
        self,
        *,
        client: AsyncWebClient,
        ref: DocumentRef,
    ) -> AsyncIterator[Document]:
        meta = ref.metadata
        file_id = meta["file_id"]
        # We always re-resolve url_private_download via files.info because
        # the discover-time URL can expire (Slack rotates download tokens
        # every ~24h). The roundtrip costs one Tier 4 call which is
        # cheap relative to the actual file body fetch.
        try:
            async with _rate.translate_slack_errors():
                info = await client.files_info(file=file_id)
        except SlackApiError as exc:
            if _api_error_code(exc) in {"file_not_found", "file_deleted"}:
                return
            raise
        file_obj = info.get("file") if hasattr(info, "get") else None
        if not isinstance(file_obj, Mapping):
            return
        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        if not url:
            return
        http = await self._ensure_http()
        try:
            body = await download_file(
                client=http,
                token=self._config.token,
                url=str(url),
                mimetype=file_obj.get("mimetype"),
                name=file_obj.get("name"),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise RateLimited("slack file download rate limited") from exc
            raise
        principal = await self._principal_for(client, file_obj.get("user"))
        yield Document(
            ref=ref,
            text=body.text,
            binary=body.binary,
            fetched_at=datetime.now(UTC),
            created_by=principal,
        )

    async def _principal_for(
        self,
        client: AsyncWebClient,
        user_id: str | None,
    ) -> Principal | None:
        """Resolve a user_id to a Principal, with in-process caching."""
        if not user_id or not self._config.fetch_user_principal:
            return None
        cached = self._principal_cache.get(user_id)
        if cached is not None:
            return cached
        try:
            async with _rate.translate_slack_errors():
                resp = await client.users_info(user=user_id)
        except SlackApiError as exc:
            # users.info rate limits hit the same Tier 4 bucket as
            # everything else; if we got back a non-rate-limit error
            # (e.g. user_not_found because the user was deleted), fall
            # through to a minimal Principal carrying just the id.
            if _api_error_code(exc) in {"user_not_found", "users_not_found"}:
                principal = Principal(id=user_id)
                self._principal_cache[user_id] = principal
                return principal
            raise
        user_obj = resp.get("user") if hasattr(resp, "get") else None
        if not isinstance(user_obj, Mapping):
            principal = Principal(id=user_id)
        else:
            profile = user_obj.get("profile") or {}
            display_name = (
                user_obj.get("real_name")
                or user_obj.get("name")
                or (
                    profile.get("display_name")
                    if isinstance(profile, Mapping)
                    else None
                )
            )
            email = profile.get("email") if isinstance(profile, Mapping) else None
            principal = Principal(
                id=user_id,
                display_name=str(display_name) if display_name else None,
                email=str(email) if email else None,
            )
        self._principal_cache[user_id] = principal
        return principal

    async def close(self) -> None:
        """Close the AsyncWebClient and (if we opened it) the httpx pool."""
        # AsyncWebClient holds an aiohttp.ClientSession; closing it is
        # required to avoid "Unclosed client session" warnings on shutdown.
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.close()  # type: ignore[func-returns-value]
            self._client = None
        if self._owned_http and self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None


def _api_error_code(exc: SlackApiError) -> str | None:
    """Return the `error` string from a SlackApiError response, if any."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    try:
        return response.get("error") if hasattr(response, "get") else None
    except Exception:  # pragma: no cover — defensive
        return None


def _default_client_factory(*, token: str, timeout: float) -> AsyncWebClient:
    """Default `AsyncWebClient` builder; isolated for test replacement."""
    return AsyncWebClient(token=token, timeout=int(timeout))


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    """Registry factory: dict → SlackConnector.

    Required key: `token` (string, must match a supported xox* prefix).
    Optional knobs match the SlackConfig fields.
    """
    if "token" not in config:
        raise ValueError("slack connector config requires 'token'")
    token = str(config["token"])
    try:
        # Validate the prefix early so a bad config raises ValueError at
        # the registry boundary rather than deep inside discover().
        classify_token(token)
    except InvalidSlackTokenError as exc:
        raise ValueError(str(exc)) from exc
    return SlackConnector(
        SlackConfig(
            token=token,
            id=str(config["id"]) if config.get("id") is not None else None,
            team_id=str(config["team_id"])
            if config.get("team_id") is not None
            else None,
            enterprise_id=(
                str(config["enterprise_id"])
                if config.get("enterprise_id") is not None
                else None
            ),
            include_threads=bool(config.get("include_threads", True)),
            include_files=bool(config.get("include_files", True)),
            fetch_user_principal=bool(config.get("fetch_user_principal", True)),
            request_timeout=float(config.get("request_timeout", 20.0)),
        )
    )


SPEC = ConnectorSpec(
    kind="slack",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=True,
        max_concurrent_fetches=4,
    ),
    required_scopes=(
        # bot/user scopes
        "channels:history",
        "channels:read",
        "groups:history",
        "groups:read",
        "im:history",
        "im:read",
        "mpim:history",
        "mpim:read",
        "files:read",
        "users:read",
        # org-wide scopes (xoxa)
        "discovery:read",
    ),
    description=(
        "Slack workspace + Enterprise Grid connector. Auto-routes between "
        "conversations.* (xoxb/xoxp) and discovery.* (xoxa) based on the "
        "token prefix. Yields one DocumentRef per message and per attached "
        "file with slack://T/C/ts canonical paths and per-(team,channel) "
        "rate-limit buckets."
    ),
)


__all__ = ["SPEC", "SlackConfig", "SlackConnector"]
