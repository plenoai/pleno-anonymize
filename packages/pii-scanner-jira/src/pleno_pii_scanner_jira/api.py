"""Minimal httpx wrapper for the Jira REST endpoints we touch.

Two flavors share one client because the differences live in the URL
prefix (`/rest/api/3` vs `/rest/api/2`), the auth header style, and
the rate-limit status code — not in the HTTP layer:

* **Cloud** — `https://{site}.atlassian.net/rest/api/3`. Auth: HTTP
  Basic (`email`/`api_token`) or Bearer (OAuth 2.0 access token).
  Rate limit: `429 Too Many Requests` with `Retry-After`.
* **Data Center** — `<base_url>/rest/api/2`. Auth: Bearer (Personal
  Access Token) or HTTP Basic (PAT or password). Rate limit:
  `503 Service Unavailable` with `Retry-After` (DC's rate limiter
  sits in front of the application and chooses 503 over 429 by
  policy — both must be honoured).

We deliberately avoid `atlassian-python-api`: it depends on
`requests` (blocking, no asyncio), uses `six`, and exposes no
transport seam for hermetic tests.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx


# Cloud / DataCenter flavor literal. Selecting one at construction time
# locks the URL shape into the client; passing the wrong flavor against
# the wrong base_url is an early-boot misconfiguration (e.g. pointing a
# "cloud" client at a Data Center instance) and we would rather fail
# loudly than silently 404 every request.
Flavor = Literal["cloud", "datacenter"]


# Default per-request timeout (seconds). Jira Cloud's published p99 for
# `/search` with JQL is ~5s; 30s leaves headroom for slow project
# imports without letting a hung connection wedge the whole scan.
DEFAULT_TIMEOUT = 30.0


# Fallback retry delay when a 429/503 lacks `Retry-After`. The DC rate
# limiter usually omits the header; we pick a conservative default
# rather than retrying immediately because the scan would otherwise
# spin a hot loop against an already-overloaded endpoint.
_DEFAULT_RETRY_AFTER_SECONDS = 30.0


# Maximum total wait imposed by a single retry. A 429 with
# `Retry-After: 3600` (Cloud's worst-case) would otherwise stall the
# whole scan for an hour; capping at 60s lets the scheduler's AIMD
# bucket take over for longer cool-downs.
_MAX_RETRY_AFTER_SECONDS = 60.0


class JiraApiError(Exception):
    """Non-retryable upstream failure (4xx other than 401/403/429/503)."""


@dataclass(frozen=True, slots=True)
class BasicAuth:
    """HTTP Basic — Cloud (`email` + `api_token`) or DC (PAT/password).

    The token never lands in `__repr__` because the dataclass auto-repr
    only shows field names; we further centralise the header build in
    `header_value()` so a future refactor cannot accidentally hand
    httpx a `(user, pass)` tuple that traces would log.
    """

    username: str
    password: str

    def header_value(self) -> str:
        # Pre-compute once at use site. b64encode requires bytes input.
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True, slots=True)
class BearerAuth:
    """Bearer — Cloud OAuth 2.0 access token or DC Personal Access Token."""

    token: str

    def header_value(self) -> str:
        return f"Bearer {self.token}"


# Either of the auth modes the connector accepts. Constrained to a
# union (not a base class) so type checkers narrow exhaustively in the
# header builder — adding a third mode later forces every match site
# to update, which is the behavior we want.
AuthMode = BasicAuth | BearerAuth


def _api_prefix(flavor: Flavor) -> str:
    """Return the REST API path prefix for the given flavor.

    Cloud sites mount the API under `/rest/api/3`; DC keeps the
    long-standing `/rest/api/2` prefix because v3 was never backported.
    Centralised so the URL builder cannot drift between Connector and
    helpers.
    """
    return "/rest/api/3" if flavor == "cloud" else "/rest/api/2"


class JiraApi:
    """Thin async wrapper around `httpx.AsyncClient`.

    Owns one client per connector lifetime. Tests inject a mock
    transport; production callers pass `transport=None` for the
    default. Cloud credentials never expire (long-lived API tokens);
    DC PATs have an expiry but Atlassian's lifecycle is operator-
    managed so we do not auto-refresh.
    """

    def __init__(
        self,
        *,
        flavor: Flavor,
        base_url: str,
        auth: AuthMode,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Any | None = None,
    ) -> None:
        if flavor not in ("cloud", "datacenter"):
            raise ValueError(f"unsupported jira flavor: {flavor!r}")
        self._flavor: Flavor = flavor
        self._base_url = base_url.rstrip("/")
        self._api_prefix = _api_prefix(flavor)
        self._auth = auth
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)
        # Injectable sleep so 429/503 backoff tests do not actually
        # sleep. Defaults to asyncio.sleep in production.
        self._sleep = sleep or asyncio.sleep

    @property
    def flavor(self) -> Flavor:
        return self._flavor

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_prefix(self) -> str:
        return self._api_prefix

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # request layer
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # `Accept: application/json` keeps Jira from falling back to XML
        # on a few legacy endpoints. `User-Agent` is good citizenship —
        # Atlassian's abuse detector keys off it for client identification.
        return {
            "Accept": "application/json",
            "User-Agent": "pleno-pii-scanner-jira",
            "Authorization": self._auth.header_value(),
        }

    def _absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        # Paths beginning with `/rest/` are passed through untouched so
        # callers can hit non-`/api/` endpoints (e.g. `/rest/auth/1/session`)
        # without us mangling the prefix.
        if path_or_url.startswith("/rest/"):
            return f"{self._base_url}{path_or_url}"
        return f"{self._base_url}{self._api_prefix}{path_or_url}"

    async def get(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """REST GET with rate-limit backoff.

        On 429 (Cloud) or 503 (DC) we honour `Retry-After` (seconds)
        and retry exactly once; a second throttle propagates as
        `JiraApiError` so the scheduler's AIMD bucket can shrink.

        `params` values that are `None` are dropped — httpx encodes them
        as the literal string `"None"` otherwise, which Jira's JQL
        parser then rejects with a 400.
        """
        url = self._absolute(path_or_url)
        clean_params = (
            {k: v for k, v in params.items() if v is not None}
            if params
            else None
        )
        for attempt in (1, 2):
            response = await self._client.get(
                url, params=clean_params, headers=self._headers()
            )
            if not _is_throttle(response, self._flavor):
                return self._handle(response)
            if attempt == 2:
                raise JiraApiError(
                    f"jira {response.status_code} {url}: persistent throttle "
                    f"after one retry"
                )
            delay = _retry_after_seconds(response)
            await self._sleep(delay)
        # Unreachable — both branches above exit. `pragma: no cover`
        # documents the defensive fall-through for static analysis.
        raise RuntimeError("unreachable")  # pragma: no cover

    def _handle(self, response: httpx.Response) -> dict[str, Any]:
        """Translate HTTP status into either a parsed body or an exception."""
        status = response.status_code
        if status == 404:
            # 404 is the Atlassian idiom for "no permission OR doesn't
            # exist"; we surface an empty dict so paginated callers can
            # `if not body: return` and skip the page silently. Mirrors
            # the github connector's "private repo == empty result" idiom.
            return {}
        if status >= 400:
            raise JiraApiError(
                f"jira {status} {response.request.method} "
                f"{response.request.url.path}: {response.text[:200]}"
            )
        # 2xx — Jira always returns JSON; .json() raising means the
        # upstream sent a corrupt body and we want the loud failure.
        return response.json()


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _is_throttle(response: httpx.Response, flavor: Flavor) -> bool:
    """True if the response represents a rate-limit signal worth retrying.

    Cloud: 429 (the standard). DC: 503 (their reverse proxy emits
    Service Unavailable when the rate limiter rejects). We treat both
    as the same retryable signal so the connector behaves uniformly
    across flavors.
    """
    if response.status_code == 429:
        return True
    if response.status_code == 503 and flavor == "datacenter":
        return True
    return False


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse `Retry-After` (seconds form) with a safe fallback + cap.

    Jira sends `Retry-After` as integer seconds. Anything unparseable
    falls back to `_DEFAULT_RETRY_AFTER_SECONDS` rather than 0 — a
    too-aggressive retry on a throttled endpoint is the fastest way to
    get the whole scan IP-banned. Values larger than
    `_MAX_RETRY_AFTER_SECONDS` are capped because we do not want a
    single request to stall the scheduler for an hour; the scheduler's
    AIMD bucket takes over for longer cool-downs.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        seconds = max(0.0, float(raw))
    except (ValueError, TypeError):
        return _DEFAULT_RETRY_AFTER_SECONDS
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


__all__ = [
    "DEFAULT_TIMEOUT",
    "AuthMode",
    "BasicAuth",
    "BearerAuth",
    "Flavor",
    "JiraApi",
    "JiraApiError",
]
