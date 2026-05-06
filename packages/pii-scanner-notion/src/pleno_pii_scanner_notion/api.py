"""Minimal httpx wrapper for the Notion REST API.

Notion's official `notion-client` SDK is sync only and exposes no
transport-injection seam, so we cannot drive it under
`httpx.MockTransport` for hermetic testing. The endpoint surface we use
is small (5 paths) — owning a thin async wrapper is cheaper than
bridging sync→async and writing parallel test infrastructure.

Rate-limit handling lives here so every endpoint surfaces `RateLimited`
to the scheduler's AIMD bucket on `429 Too Many Requests`. The
`Notion-Version` header is pinned to a specific date — Notion breaks
response shape across versions and an unpinned client will silently
start failing one morning when they roll a new schema.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from pleno_pii_scanner.scheduler.rate_limit import RateLimited


# Public REST host. There is no GHES-equivalent self-hosted Notion, so we
# don't expose `base_url` as a connector option — only the constructor
# accepts it for tests that want to point at a captive mock.
DEFAULT_BASE_URL = "https://api.notion.com/v1"

# Pinned API version. Bumping requires re-validating every block / property
# parser in `markdown.py`; do NOT silently track upstream's "latest".
# 2022-06-28 is Notion's GA version and the schema we parse against.
NOTION_VERSION = "2022-06-28"

_USER_AGENT = "pleno-pii-scanner-notion"

# Notion's `page_size` ceiling on every paginated endpoint. Hard-coded
# instead of exposed because going below 100 multiplies request count
# (= rate-limit pressure) for no benefit, and >100 is rejected by Notion.
PAGE_SIZE = 100


class NotionApiError(Exception):
    """Non-retryable upstream failure (4xx other than 401/403/429)."""


class NotionApi:
    """Thin async wrapper around `httpx.AsyncClient`.

    Owns one HTTP client for the connector's lifetime. Tests inject a
    mock transport; production callers pass `transport=None` for the
    default. The bearer token is immutable per-instance — Notion
    integration tokens never expire so there is no refresh seam.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        notion_version: str = NOTION_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not token:
            raise ValueError("notion api requires a non-empty integration token")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def notion_version(self) -> str:
        return self._notion_version

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        # Authorization MUST come from the constructor token; Notion-Version
        # is pinned because response shape is version-coupled (e.g. the
        # `properties` schema for databases changed between 2021-08-16 and
        # 2022-06-28). Content-Type only matters for POST bodies but we
        # set it unconditionally — Notion ignores it on GET.
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._notion_version,
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        response = await self._client.get(url, params=params, headers=self._headers())
        return self._handle(response)

    async def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        response = await self._client.post(
            url, json=dict(json or {}), headers=self._headers()
        )
        return self._handle(response)

    def _handle(self, response: httpx.Response) -> dict[str, Any]:
        """Translate HTTP status into either a parsed body or an exception."""
        _raise_for_rate_limit(response)
        status = response.status_code
        if status == 404:
            # Notion returns 404 for "this integration cannot see this object"
            # *and* for "this object does not exist". The two are
            # indistinguishable from the API; both must be treated as a
            # silent skip by the connector, not a fatal error. We surface
            # an empty dict so callers can `if not body: return`.
            return {}
        if status >= 400:
            raise NotionApiError(
                f"notion {status} {response.request.method} {response.request.url.path}: "
                f"{response.text[:200]}"
            )
        # Notion always responds with JSON for 2xx; .json() raising means
        # the upstream sent a corrupt body and we want the loud failure.
        return response.json()


def _raise_for_rate_limit(response: httpx.Response) -> None:
    """Convert Notion's 429 signal into `RateLimited`.

    Notion documents only one rate-limit shape: `429 Too Many Requests`
    with a `Retry-After` integer header in seconds. Unlike GitHub there
    is no "secondary" 403; primary 401 means the token is bad and is a
    fatal NotionApiError handled by the generic >=400 branch.
    """
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimited(f"notion 429; retry_after={retry_after!r}")


__all__ = [
    "DEFAULT_BASE_URL",
    "NOTION_VERSION",
    "PAGE_SIZE",
    "NotionApi",
    "NotionApiError",
]
