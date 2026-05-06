"""Minimal httpx wrapper for the Bitbucket REST endpoints we touch.

We deliberately avoid `atlassian-python-api`: it depends on `requests`
(blocking, no asyncio), uses `six`, has long-standing pagination bugs
on Bitbucket Server (`isLastPage` ignored when `nextPageStart` absent
on the final page), and offers no transport seam — which we rely on for
hermetic tests via `httpx.MockTransport`.

Two flavors share one client because the differences live in path
shapes, paginator semantics, and auth headers — not in the HTTP layer:

* **Cloud** — `https://api.bitbucket.org/2.0`. Pagination via a
  `next` URL field embedded in every page. Auth: HTTP basic
  (`username`/`app_password`) or Bearer (workspace access token).
* **Server / Data Center** — `<base_url>/rest/api/1.0`. Pagination
  via `start` query param + `nextPageStart`/`isLastPage` in the body.
  Auth: Bearer (HTTP access token) or HTTP basic (PAT or password).

`Retry-After` on 429 is surfaced as `RateLimited` so the scheduler's
AIMD bucket halves the per-tenant fill rate (ADR §7).
"""

from __future__ import annotations

import asyncio
import base64
import ssl
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from pleno_pii_scanner.scheduler.rate_limit import RateLimited


# Cloud / Server flavor literal. Selecting one at construction time
# locks the URL shape + paginator into the client; passing the wrong
# flavor against the wrong base_url is an early-boot misconfiguration
# (e.g. pointing a "cloud" client at a Data Center instance) and we
# would rather fail loudly than silently 404 every request.
Flavor = Literal["cloud", "server"]


# Public Cloud endpoint. Server installs override `base_url` to the
# self-managed host; we never default to a Server URL.
DEFAULT_CLOUD_BASE_URL = "https://api.bitbucket.org/2.0"


# Bitbucket sometimes 429s without `Retry-After` (Cloud's free-tier
# burst limiter does this) — we fall back to this value so callers do
# not spin in zero-sleep retries. Matches the conservative default the
# scheduler's AIMD bucket starts with.
_DEFAULT_RETRY_AFTER_SECONDS = 30.0


# Maximum number of pages we will walk inside `paginate()` without the
# server signalling completion. Defends against a buggy upstream that
# returns `next` perpetually pointing at the same URL — without this
# guard a misbehaving Cloud edge cache could turn `discover()` into an
# infinite loop. 10_000 pages × 100 entries ≈ a million records; well
# above any realistic single-workspace size.
_MAX_PAGINATION_DEPTH = 10_000


class BitbucketApiError(Exception):
    """Non-retryable upstream failure (4xx other than 401/403/429)."""


@dataclass(frozen=True, slots=True)
class BasicAuth:
    """HTTP basic auth — Cloud app passwords or Server PAT/password.

    The connector never logs the password: `__repr__` is auto-generated
    from the dataclass but field names alone reveal nothing, and we
    serialize the auth header inside `_headers()` rather than handing
    httpx a `(user, pass)` tuple that might leak into trace output.
    """

    username: str
    password: str

    def header_value(self) -> str:
        # Pre-compute once at use site; avoids re-encoding on every
        # request. b64encode requires bytes input.
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True, slots=True)
class BearerAuth:
    """Bearer token — Cloud workspace access token or Server HTTP access token."""

    token: str

    def header_value(self) -> str:
        return f"Bearer {self.token}"


# Either of the auth modes the connector accepts. Constrained to a
# union (not a base class) so type checkers narrow exhaustively in the
# header builder — adding a third mode later forces every match site
# to update, which is the behavior we want.
AuthMode = BasicAuth | BearerAuth


class BitbucketApi:
    """Thin async wrapper around `httpx.AsyncClient`.

    Owns one client per connector lifetime. Tests inject a mock
    transport; production callers pass `transport=None` to get the
    default. `ca_bundle_path` is honored only when no transport is
    injected (Server installs commonly use a private CA).
    """

    def __init__(
        self,
        *,
        flavor: Flavor,
        base_url: str,
        auth: AuthMode,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        ca_bundle_path: str | None = None,
        sleep: "Any | None" = None,
    ) -> None:
        if flavor not in ("cloud", "server"):
            raise ValueError(f"unsupported bitbucket flavor: {flavor!r}")
        self._flavor: Flavor = flavor
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        elif ca_bundle_path is not None:
            # Build an `SSLContext` with the operator's PEM bundle
            # appended so httpx trusts the enterprise CA in addition to
            # the system trust store. Required for Server installs
            # behind a private CA. Only honored when no transport
            # override is supplied — the test seam takes precedence
            # because tests should never hit the network.
            kwargs["verify"] = ssl.create_default_context(cafile=ca_bundle_path)
        self._client = httpx.AsyncClient(**kwargs)
        # Injectable sleep so 429-backoff tests do not actually sleep.
        # Defaults to asyncio.sleep in production.
        self._sleep = sleep or asyncio.sleep

    @property
    def flavor(self) -> Flavor:
        return self._flavor

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # request layer
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # `Accept: application/json` is required on Bitbucket Server or
        # the API returns XML for some endpoints (`/branches/default`).
        # `User-Agent` is good citizenship — Bitbucket Cloud's abuse
        # detector looks at it for client identification.
        return {
            "Accept": "application/json",
            "User-Agent": "pleno-pii-scanner-bitbucket",
            "Authorization": self._auth.header_value(),
        }

    def _absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        # Cloud paths look like `/repositories/{ws}`; Server paths look
        # like `/projects/{key}/repos`. The base URL already encodes the
        # `/2.0` (Cloud) or `/rest/api/1.0` (Server) prefix, so we just
        # need to ensure the leading slash.
        if not path_or_url.startswith("/"):
            path_or_url = "/" + path_or_url
        return f"{self._base_url}{path_or_url}"

    async def get(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """REST GET with rate-limit backoff.

        On 429 we respect the `Retry-After` header (in seconds) and
        retry exactly once. A second 429 surfaces `RateLimited` to the
        scheduler so its AIMD bucket can shrink the connector's per-
        tenant rate. We deliberately bound retries here rather than in
        the scheduler so that a single discover() call making N paged
        requests does not amplify a transient throttle into N × scheduler
        backoffs.
        """
        url = self._absolute(path_or_url)
        response = await self._client.get(url, params=params, headers=self._headers())
        if response.status_code != 429:
            return response
        delay = _retry_after_seconds(response)
        await self._sleep(delay)
        # One more shot before surfacing rate-limited; if Bitbucket is
        # still throttling we want the scheduler to know about it.
        response = await self._client.get(url, params=params, headers=self._headers())
        if response.status_code == 429:
            raise RateLimited(
                f"bitbucket 429; retry_after={delay} seconds (after one retry)"
            )
        return response

    # ------------------------------------------------------------------
    # pagination
    # ------------------------------------------------------------------

    async def paginate(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_size: int = 100,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Yield each entry across every page of a paginated endpoint.

        Cloud responses look like `{"values": [...], "next": "<url>"}`;
        Server responses look like `{"values": [...], "size": N,
        "isLastPage": false, "nextPageStart": 25}`. We unify both into
        a single async iterator of `values` entries.

        `params` is sent only on the first request — Cloud's `next` URL
        already embeds whatever filters were applied; Server uses the
        explicit `start` pointer we substitute on each iteration.
        """
        next_url: str | None = self._absolute(path)
        next_params: Mapping[str, Any] | None = {
            **(params or {}),
            "pagelen" if self._flavor == "cloud" else "limit": page_size,
        }
        next_start: int | None = 0 if self._flavor == "server" else None
        depth = 0
        while next_url is not None:
            depth += 1
            if depth > _MAX_PAGINATION_DEPTH:
                # See `_MAX_PAGINATION_DEPTH` comment: defensive guard
                # against a server that hands back the same `next` URL
                # repeatedly. Surfacing as BitbucketApiError lets the
                # scheduler treat it as a non-retryable failure rather
                # than spinning the discover() loop forever.
                raise BitbucketApiError(
                    f"pagination exceeded {_MAX_PAGINATION_DEPTH} pages "
                    f"at {next_url!r}; refusing to continue"
                )
            request_params = next_params
            if self._flavor == "server" and next_start is not None and next_start > 0:
                request_params = {
                    **(params or {}),
                    "limit": page_size,
                    "start": next_start,
                }
            response = await self.get(next_url, params=request_params)
            if response.status_code != 200:
                # 401/403/404 on a paginated endpoint: yield nothing.
                # Surfacing here would force every caller to wrap with
                # try/except just to handle "the project was deleted
                # between enumeration and walk" races; instead we treat
                # discover-time HTTP errors as empty result sets, the
                # same idiom the github connector uses.
                return
            body = response.json()
            for entry in body.get("values", []) or []:
                yield entry
            if self._flavor == "cloud":
                # Cloud: `next` is a fully-qualified URL; absent → done.
                next_url = body.get("next")
                next_params = None  # Cloud's `next` already carries query
            else:
                # Server: explicit `isLastPage` flag. Defensive against
                # `nextPageStart` missing on the last page (the bug
                # behind atlassian-python-api's worst pagination issue).
                if body.get("isLastPage", True):
                    return
                next_start = body.get("nextPageStart")
                if next_start is None:
                    return
                next_url = self._absolute(path)


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse `Retry-After` (seconds form) with safe fallback.

    Bitbucket sends `Retry-After` as integer seconds (no HTTP-date form
    in the wild), so we only handle the integer case. Anything
    unparseable falls back to `_DEFAULT_RETRY_AFTER_SECONDS` rather
    than 0 — a too-aggressive retry on a throttled endpoint is the
    fastest way to get the whole scan IP-banned.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_RETRY_AFTER_SECONDS


__all__ = [
    "DEFAULT_CLOUD_BASE_URL",
    "AuthMode",
    "BasicAuth",
    "BearerAuth",
    "BitbucketApi",
    "BitbucketApiError",
    "Flavor",
]
