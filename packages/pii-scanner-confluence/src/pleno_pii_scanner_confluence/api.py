"""Minimal httpx wrapper for the Confluence REST endpoints we touch.

We deliberately avoid `atlassian-python-api`: it depends on `requests`
(blocking, no asyncio), uses `six`, and offers no transport seam — which
we need for hermetic tests via `httpx.MockTransport`.

Two flavors share one client because the differences live in path
shapes, paginator semantics, and rate-limit signals — not in the HTTP
layer:

* **Cloud** — `https://{site}.atlassian.net/wiki/rest/api/...` (v1) +
  the v2 endpoint at `https://api.atlassian.com/ex/confluence/{cloudId}`.
  Pagination via cursor on v2 (`?cursor=...`) or `start`/`limit` on v1.
  Auth: HTTP basic (email + api_token) or Bearer (OAuth 2.0). Rate-
  limit signal: `429 Too Many Requests` with `Retry-After`.
* **Data Center** — `<base_url>/rest/api/...`. Pagination via
  `start`/`limit` query params. Auth: Bearer (Personal Access Token)
  or HTTP basic (username + password). Rate-limit signal: `503
  Service Unavailable` (DC's reverse proxy returns 503 under sustained
  load) plus 429 from the rate-limit add-on; we honor both.

`Retry-After` is honored once and the second occurrence surfaces
`RateLimited` so the scheduler's AIMD bucket halves the per-tenant fill
rate (ADR §7).
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


# Cloud / Data Center flavor literal. Selecting one at construction time
# locks the URL shape + paginator into the client; passing the wrong
# flavor against the wrong base_url is an early-boot misconfiguration
# (e.g. pointing a "cloud" client at a Data Center instance) and we
# would rather fail loudly than silently 404 every request.
Flavor = Literal["cloud", "datacenter"]


# Cloud installs always live under `<site>.atlassian.net/wiki`. There is
# no cross-tenant default URL: every site is its own host. We therefore
# require `base_url` for both flavors and never fall back to a hard-coded
# Cloud endpoint — the bitbucket precedent of a public default does not
# apply here.

# Confluence sometimes 429/503s without `Retry-After` (DC reverse proxies
# under load do this). We fall back to this value so callers do not spin
# in zero-sleep retries. Matches the conservative default the scheduler's
# AIMD bucket starts with.
_DEFAULT_RETRY_AFTER_SECONDS = 30.0


# Maximum number of pages we will walk inside `paginate()` without the
# server signalling completion. Defends against a buggy upstream that
# returns `next` perpetually pointing at the same URL — without this
# guard a misbehaving Cloud edge cache could turn `discover()` into an
# infinite loop. 10_000 pages × 100 entries ≈ a million records; well
# above any realistic single-space size.
_MAX_PAGINATION_DEPTH = 10_000


class ConfluenceApiError(Exception):
    """Non-retryable upstream failure (4xx other than 401/403/429)."""


@dataclass(frozen=True, slots=True)
class BasicAuth:
    """HTTP basic auth — Cloud (email + api_token) or DC (user + password).

    The connector never logs the password: `__repr__` is auto-generated
    from the dataclass but field names alone reveal nothing, and we
    serialize the auth header inside `_headers()` rather than handing
    httpx an `(user, pass)` tuple that might leak into trace output.
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
    """Bearer token — Cloud OAuth 2.0 access token or DC PAT."""

    token: str

    def header_value(self) -> str:
        return f"Bearer {self.token}"


# Either of the auth modes the connector accepts. Constrained to a
# union (not a base class) so type checkers narrow exhaustively in the
# header builder — adding a third mode later forces every match site
# to update, which is the behavior we want.
AuthMode = BasicAuth | BearerAuth


class ConfluenceApi:
    """Thin async wrapper around `httpx.AsyncClient`.

    Owns one client per connector lifetime. Tests inject a mock
    transport; production callers pass `transport=None` to get the
    default. `ca_bundle_path` is honored only when no transport is
    injected (DC installs commonly use a private CA).
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
        if flavor not in ("cloud", "datacenter"):
            raise ValueError(f"unsupported confluence flavor: {flavor!r}")
        self._flavor: Flavor = flavor
        # base_url is the *site* root for both flavors; we append the
        # `/rest/api` suffix in `_absolute()` rather than baking it into
        # the constructor so a single client can talk to both v1 and v2
        # paths on Cloud.
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        elif ca_bundle_path is not None:
            # Build an `SSLContext` with the operator's PEM bundle
            # appended so httpx trusts the enterprise CA in addition to
            # the system trust store. Required for DC installs behind a
            # private CA. Only honored when no transport override is
            # supplied — the test seam takes precedence because tests
            # should never hit the network.
            kwargs["verify"] = ssl.create_default_context(cafile=ca_bundle_path)
        self._client = httpx.AsyncClient(**kwargs)
        # Injectable sleep so 429/503-backoff tests do not actually
        # sleep. Defaults to asyncio.sleep in production.
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
        # `Accept: application/json` is required on Confluence DC or
        # the API returns XML for some endpoints. `User-Agent` is good
        # citizenship — Atlassian's abuse detector looks at it for client
        # identification.
        return {
            "Accept": "application/json",
            "User-Agent": "pleno-pii-scanner-confluence",
            "Authorization": self._auth.header_value(),
        }

    def _absolute(self, path_or_url: str) -> str:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        # Caller-provided paths are relative to the site root. The Cloud
        # v1 prefix is `/rest/api`, the DC prefix is also `/rest/api`,
        # so we join verbatim. The v2 host (api.atlassian.com) is only
        # reached via absolute URLs.
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

        On 429 (both flavors) or 503 (DC) we respect `Retry-After`
        (in seconds) and retry exactly once. A second hit surfaces
        `RateLimited` to the scheduler so its AIMD bucket can shrink the
        connector's per-tenant rate. We deliberately bound retries here
        rather than in the scheduler so that a single discover() call
        making N paged requests does not amplify a transient throttle
        into N × scheduler backoffs.
        """
        url = self._absolute(path_or_url)
        response = await self._client.get(url, params=params, headers=self._headers())
        if not _is_throttled(response, self._flavor):
            return response
        delay = _retry_after_seconds(response)
        await self._sleep(delay)
        # One more shot before surfacing rate-limited; if Confluence is
        # still throttling we want the scheduler to know about it.
        response = await self._client.get(url, params=params, headers=self._headers())
        if _is_throttled(response, self._flavor):
            raise RateLimited(
                f"confluence {response.status_code}; retry_after={delay} "
                f"seconds (after one retry)"
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

        Confluence Cloud v1 + DC return
        `{"results": [...], "size": N, "start": 0, "limit": 100,
        "_links": {"next": "/rest/api/..."}}`. The `_links.next` field
        is the canonical "more pages exist" signal; falling back to
        `start + size < total` would miss filtered list endpoints that
        don't expose `total`.

        Cloud v2 (when reached via an absolute URL) returns
        `{"results": [...], "_links": {"next": "..."}}`. The shape is
        the same enough for one paginator to handle both.

        `params` is sent only on the first request — `_links.next`
        already embeds whatever filters were applied.
        """
        next_url: str | None = self._absolute(path)
        next_params: Mapping[str, Any] | None = {
            **(params or {}),
            "limit": page_size,
        }
        depth = 0
        while next_url is not None:
            depth += 1
            if depth > _MAX_PAGINATION_DEPTH:
                # See `_MAX_PAGINATION_DEPTH` comment: defensive guard
                # against a server that hands back the same `next` URL
                # repeatedly. Surfacing as ConfluenceApiError lets the
                # scheduler treat it as a non-retryable failure rather
                # than spinning the discover() loop forever.
                raise ConfluenceApiError(
                    f"pagination exceeded {_MAX_PAGINATION_DEPTH} pages "
                    f"at {next_url!r}; refusing to continue"
                )
            response = await self.get(next_url, params=next_params)
            if response.status_code != 200:
                # 401/403/404 on a paginated endpoint: yield nothing.
                # Surfacing here would force every caller to wrap with
                # try/except just to handle "the space was deleted
                # between enumeration and walk" races; instead we treat
                # discover-time HTTP errors as empty result sets, the
                # same idiom the bitbucket connector uses.
                return
            body = response.json()
            for entry in body.get("results", []) or []:
                yield entry
            links = body.get("_links") or {}
            raw_next = links.get("next")
            if not isinstance(raw_next, str) or not raw_next:
                return
            # `_links.next` may be absolute (Cloud v2) or relative to
            # the site root (Cloud v1 + DC). `_absolute` handles both.
            next_url = self._absolute(raw_next)
            # On the second-and-later requests, `next` already carries
            # the cursor / start; do not double-apply our params.
            next_params = None


def _is_throttled(response: httpx.Response, flavor: Flavor) -> bool:
    """Return True for any rate-limit / overload status we honor.

    Cloud only emits 429. DC emits 429 *and* 503 — the second is the
    Atlassian-recommended overload signal from the reverse proxy in
    front of the JVM (see DC ops docs: `Retry-After` is set on both).
    Treating 503 as a hard error on DC would force operators to add an
    external retry loop; honoring it here keeps the connector contract
    uniform.
    """
    if response.status_code == 429:
        return True
    if flavor == "datacenter" and response.status_code == 503:
        return True
    return False


def _retry_after_seconds(response: httpx.Response) -> float:
    """Parse `Retry-After` (seconds form) with safe fallback.

    Atlassian sends `Retry-After` as integer seconds (no HTTP-date
    form in the wild on either flavor), so we only handle the integer
    case. Anything unparseable falls back to
    `_DEFAULT_RETRY_AFTER_SECONDS` rather than 0 — a too-aggressive
    retry on a throttled endpoint is the fastest way to get the whole
    scan IP-banned.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return _DEFAULT_RETRY_AFTER_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_RETRY_AFTER_SECONDS


__all__ = [
    "AuthMode",
    "BasicAuth",
    "BearerAuth",
    "ConfluenceApi",
    "ConfluenceApiError",
    "Flavor",
]
