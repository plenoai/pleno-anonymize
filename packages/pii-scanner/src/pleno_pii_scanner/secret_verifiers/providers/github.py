"""GitHub liveness verifier.

Targets PAT (ghp_*), fine-grained PAT (github_pat_*), OAuth user
tokens (gho_*), GitHub App user-to-server (ghu_*), App installation
tokens (ghs_*), and refresh tokens (ghr_*). All hit the same /user
endpoint, which returns 200 + login for any token with read:user.
"""

from __future__ import annotations

import httpx

from ..base import VerificationResult, VerifyContext
from ._http import build_client

_USER_URL = "https://api.github.com/user"


class GitHubVerifier:
    name = "github"
    entities = frozenset(
        {
            "GITHUB_PAT",
            "GITHUB_FINE_GRAINED_PAT",
            "GITHUB_OAUTH_TOKEN",
            "GITHUB_APP_TOKEN",
            "GITHUB_APP_USER_TOKEN",
            "GITHUB_APP_REFRESH_TOKEN",
        }
    )

    async def verify(self, value: str, *, ctx: VerifyContext) -> VerificationResult:
        headers = {
            "Authorization": f"Bearer {value}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "pleno-pii-scanner",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with build_client(ctx) as client:
            try:
                response = await client.get(_USER_URL, headers=headers)
            except httpx.TimeoutException:
                return VerificationResult(
                    state="error", detail="timeout", ttl_seconds=60
                )
            except httpx.HTTPError as exc:
                return VerificationResult(
                    state="error",
                    detail=f"transport: {type(exc).__name__}",
                    ttl_seconds=60,
                )
        return _classify(response)


def _classify(response: httpx.Response) -> VerificationResult:
    status = response.status_code
    if status == 200:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        login = payload.get("login") if isinstance(payload, dict) else None
        scopes = response.headers.get("x-oauth-scopes", "")
        metadata: dict[str, object] = {}
        if isinstance(login, str):
            metadata["login"] = login
        if scopes:
            metadata["scopes"] = scopes
        detail = f"valid token for {login}" if login else "valid token"
        return VerificationResult(state="live", detail=detail, metadata=metadata)
    if status == 401:
        return VerificationResult(state="revoked", detail="401 unauthorized")
    if status == 403:
        # GitHub returns 403 + x-ratelimit-remaining: 0 when the token
        # has been throttled. We treat any 403 as rate_limited so the
        # cache layer skips persistence and the next batch re-tries.
        remaining = response.headers.get("x-ratelimit-remaining")
        return VerificationResult(
            state="rate_limited",
            detail=f"403 forbidden (remaining={remaining})",
            ttl_seconds=60,
        )
    if 500 <= status < 600:
        return VerificationResult(
            state="error", detail=f"upstream {status}", ttl_seconds=60
        )
    return VerificationResult(
        state="unknown", detail=f"unexpected {status}", ttl_seconds=300
    )
