"""Minimal httpx wrapper for the GitHub REST + GraphQL endpoints we use.

We deliberately avoid PyGithub: it is unmaintained for fine-grained PATs
and has long-standing App-auth bugs (issue #2030, #2123). githubkit is
healthier but pulls Pydantic v2 + a code-generated 6 MB SDK we do not
need — we hit five endpoints. Hand-rolling httpx keeps the dep surface
small and lets us share the `transport` test seam with the rest of the
scanner (`secret_verifiers/providers/_http.py` uses the same pattern).

Rate-limit handling is centralized here so every endpoint surfaces a
`RateLimited` exception to the scheduler when the upstream signals
exhaustion. The AIMD bucket in
`pleno_pii_scanner.scheduler.rate_limit.GlobalRateLimiter` consumes this
exception and shrinks the connector's per-tenant rate.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from pleno_pii_scanner.scheduler.rate_limit import RateLimited


# Default REST + GraphQL endpoints. GHES rewrites these to
# `<host>/api/v3` and `<host>/api/graphql` respectively; the `/api/v3`
# prefix is critical — many third-party libs forget it and silently 404.
DEFAULT_BASE_URL = "https://api.github.com"
DEFAULT_GRAPHQL_URL = "https://api.github.com/graphql"

# `User-Agent` is required by the GitHub API or it returns 403. The
# X-GitHub-Api-Version header pins the response schema so endpoint
# evolution does not silently break our parsing.
_USER_AGENT = "pleno-pii-scanner-github"
_API_VERSION = "2022-11-28"


def graphql_url_for(base_url: str) -> str:
    """Derive the GraphQL endpoint from `base_url`.

    api.github.com  ->  https://api.github.com/graphql
    GHES https://ghe.example.com/api/v3  ->  https://ghe.example.com/api/graphql

    The GHES rewrite drops the `/api/v3` REST prefix and re-attaches
    `/api/graphql` because GitHub's GraphQL endpoint is unversioned.
    """
    base = base_url.rstrip("/")
    if base.endswith("/api/v3"):
        return base[: -len("/api/v3")] + "/api/graphql"
    return base + "/graphql"


class GithubApiError(Exception):
    """Non-retryable upstream failure (4xx other than 401/403/429)."""


class GithubApi:
    """Thin async wrapper around httpx.AsyncClient.

    Owns one client for the connector's lifetime. Tests inject a mock
    transport; production callers pass `transport=None` to get the
    default. The bearer token is mutable so the App auth layer can
    swap installation tokens in/out without rebuilding the client.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._graphql_url = graphql_url_for(self._base_url)
        self._token = token
        kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        self._client = httpx.AsyncClient(**kwargs)

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def graphql_url(self) -> str:
        return self._graphql_url

    def set_token(self, token: str) -> None:
        """Swap the bearer token (used after installation-token refresh)."""
        self._token = token

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, *, token: str | None = None) -> dict[str, str]:
        # Per-request token override lets the App-auth flow mint a JWT
        # for the `/app/installations/.../access_tokens` exchange while
        # the cached installation token continues to drive everything
        # else through `self._token`.
        bearer = token if token is not None else self._token
        h: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if bearer is not None:
            h["Authorization"] = f"Bearer {bearer}"
        return h

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        token: str | None = None,
        accept: str | None = None,
    ) -> httpx.Response:
        """REST GET with optional Accept override (raw blob fetch)."""
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = self._headers(token=token)
        if accept is not None:
            headers["Accept"] = accept
        response = await self._client.get(url, params=params, headers=headers)
        _raise_for_rate_limit(response)
        return response

    async def post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        response = await self._client.post(
            url, json=json, headers=self._headers(token=token)
        )
        _raise_for_rate_limit(response)
        return response

    async def graphql(
        self, query: str, variables: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue a GraphQL request and return the `data` payload.

        Raises `GithubApiError` on `errors` (GraphQL returns 200 even
        when the query is malformed; the error is in the body, not the
        status code).
        """
        body: dict[str, Any] = {"query": query}
        if variables is not None:
            body["variables"] = dict(variables)
        response = await self._client.post(
            self._graphql_url, json=body, headers=self._headers()
        )
        _raise_for_rate_limit(response)
        if response.status_code != 200:
            raise GithubApiError(
                f"graphql {response.status_code}: {response.text[:200]}"
            )
        payload = response.json()
        if "errors" in payload and payload["errors"]:
            raise GithubApiError(f"graphql errors: {payload['errors']!r}")
        return payload.get("data", {})


def _raise_for_rate_limit(response: httpx.Response) -> None:
    """Convert GitHub rate-limit signals to `RateLimited`.

    GitHub uses two distinct mechanisms:

    * Primary: `429 Too Many Requests` (RST'd connection on REST when
      the bucket is empty; rare for App tokens which get 15000/hour).
    * Secondary: `403 Forbidden` with `Retry-After` header (abuse
      detector; triggered by burst patterns even under quota).

    Both must back off. The scheduler's AIMD bucket consumes
    `RateLimited` and halves the per-tenant fill rate.
    """
    status = response.status_code
    if status == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimited(f"github primary 429; retry_after={retry_after!r}")
    if status == 403 and response.headers.get("Retry-After") is not None:
        raise RateLimited(
            f"github secondary 403; retry_after={response.headers['Retry-After']!r}"
        )
    if status == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
        # Quota exhausted with no Retry-After: surface as RateLimited so
        # the scheduler retries after the bucket refills, rather than
        # treating it as auth failure.
        raise RateLimited(
            f"github quota exhausted; reset={response.headers.get('X-RateLimit-Reset')!r}"
        )
