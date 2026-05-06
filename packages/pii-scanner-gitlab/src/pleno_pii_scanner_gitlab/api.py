"""Minimal httpx wrapper for the GitLab REST endpoints we use.

We hit a tiny slice of the GitLab API (projects.list, groups.projects,
projects.get) and python-gitlab — the only mature SDK — is sync-only,
which would force us to dispatch every call onto a thread pool inside
the otherwise async scheduler. Hand-rolling httpx keeps us async-native
and lets us share the `transport` test seam with the rest of the
scanner (see `pii-scanner-github/api.py` for the same pattern).

Two GitLab quirks deserve a comment:

* **Pagination via `Link` header (RFC 5988).** GitLab returns
  `Link: <https://.../?page=2>; rel="next", <...>; rel="last"`. The
  `rel="next"` URL is absolute and includes the cursor — we follow it
  verbatim instead of incrementing `?page=` ourselves, which would race
  with server-side keyset-pagination cutovers (X-Next-Page is not
  populated when keyset is in use).
* **Rate-limit headers are `RateLimit-*` (no `X-` prefix)** since
  GitLab 8.x. The legacy `X-RateLimit-Remaining` pair is still emitted
  on some self-managed deployments < 13.0; we honour both for safety.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from pleno_pii_scanner.scheduler.rate_limit import RateLimited

from pleno_pii_scanner_gitlab.auth import GitlabAuthMode, header_for


# SaaS default. Self-managed instances pass their own `https://gitlab.example.com`.
DEFAULT_BASE_URL = "https://gitlab.com"

# We pin the API version to v4. v3 was sunset in GitLab 11.0 (2018) and v5
# is not announced. Encoding it in the path prefix means a future v5
# rollout cannot silently rebind our calls.
_API_VERSION_PREFIX = "/api/v4"

# `User-Agent` is not strictly required by GitLab but greatly improves
# log triage on self-managed deployments where ops correlate by UA.
_USER_AGENT = "pleno-pii-scanner-gitlab"


class GitlabApiError(Exception):
    """Non-retryable upstream failure (4xx other than 401/403/429)."""


class GitlabApi:
    """Thin async wrapper around httpx.AsyncClient.

    Owns one client for the connector's lifetime. The auth header is
    immutable post-construction — token rotation is the operator's job
    via CredentialBroker re-resolution and a fresh connector instance,
    not a hot swap. This is deliberate: hot-swapping a token mid-scan
    would invalidate in-flight requests racing on `_headers()`.

    `verify` accepts the same value as `httpx.AsyncClient.verify`:
    True (system bundle), False (disable — never use), or a path to a
    PEM bundle for self-managed CAs. We expose it as a constructor arg
    so the connector can wire `ca_bundle_path` straight through.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        auth_mode: GitlabAuthMode,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        verify: bool | str = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_mode = auth_mode
        self._auth_header_name, self._auth_header_value = header_for(auth_mode, token)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            # MockTransport short-circuits the network entirely; passing
            # `verify` alongside it would be wasted (and httpx warns).
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = verify
        self._client = httpx.AsyncClient(**kwargs)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def auth_mode(self) -> GitlabAuthMode:
        return self._auth_mode

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        # Rebuilt per request rather than cached on the instance so a
        # subclass that wants to splice in a per-request header (X-Request-ID
        # for trace correlation, e.g.) can do so without mutating shared state.
        return {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
            self._auth_header_name: self._auth_header_value,
        }

    def _resolve_url(self, path_or_url: str) -> str:
        """Pagination follow-ups arrive as absolute URLs; everything else
        is a relative path under `/api/v4`."""
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            return path_or_url
        return f"{self._base_url}{_API_VERSION_PREFIX}{path_or_url}"

    async def get(
        self,
        path_or_url: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """REST GET; surfaces 429 / quota-exhausted as RateLimited."""
        response = await self._client.get(
            self._resolve_url(path_or_url),
            params=params,
            headers=self._headers(),
        )
        _raise_for_rate_limit(response)
        return response

    @staticmethod
    def parse_next_link(response: httpx.Response) -> str | None:
        """Extract the `rel="next"` URL from a Link header, or None.

        Public so callers can drive pagination loops without re-parsing
        the same string twice. Returns None when:

          * the header is absent (last page), OR
          * the header lacks `rel="next"` (single-page response with
            only a `rel="first"` self-link — rare but legal per RFC 5988).
        """
        link = response.headers.get("Link")
        if not link:
            return None
        # Link header is comma-separated entries:
        #   <url>; rel="next", <url>; rel="prev"
        # We avoid pulling in `requests` just for `parse_header_links`;
        # the format is simple enough to split by hand and the failure
        # mode of an over-eager parser (silently following `rel="prev"`)
        # is much worse than just bailing out on a malformed entry.
        for entry in link.split(","):
            parts = [p.strip() for p in entry.split(";")]
            if len(parts) < 2:
                continue
            url_part = parts[0]
            if not (url_part.startswith("<") and url_part.endswith(">")):
                continue
            url = url_part[1:-1]
            for attr in parts[1:]:
                if attr.replace(" ", "") == 'rel="next"':
                    return url
        return None


def _raise_for_rate_limit(response: httpx.Response) -> None:
    """Convert GitLab rate-limit signals to `RateLimited`.

    GitLab's standard signal is `429 Too Many Requests` with a
    `Retry-After` header (seconds). On older self-managed deployments
    a 403 with `RateLimit-Remaining: 0` is also seen — same semantics
    so we treat it identically.

    The scheduler's AIMD bucket consumes `RateLimited` and halves the
    per-tenant fill rate; an unhandled 429 would otherwise be exposed
    as an opaque GitlabApiError and the bucket would not shrink.
    """
    status = response.status_code
    if status == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimited(f"gitlab 429; retry_after={retry_after!r}")
    if status == 403 and (
        response.headers.get("RateLimit-Remaining") == "0"
        or response.headers.get("X-RateLimit-Remaining") == "0"
    ):
        # Quota exhausted, no Retry-After. Surface as RateLimited so the
        # scheduler treats it as transient (refill in <bucket window>)
        # rather than as a permission failure that needs an operator.
        reset = response.headers.get("RateLimit-Reset") or response.headers.get(
            "X-RateLimit-Reset"
        )
        raise RateLimited(f"gitlab quota exhausted; reset={reset!r}")


__all__ = [
    "DEFAULT_BASE_URL",
    "GitlabApi",
    "GitlabApiError",
]
