"""Slack liveness verifier (auth.test).

xoxb (bot), xoxp (user), xoxa (workspace app), xoxs (legacy) all use
the same auth.test endpoint. The HTTP layer always returns 200; the
ok flag in the JSON body is the actual signal.
"""

from __future__ import annotations

import httpx

from ..base import VerificationResult, VerifyContext
from ._http import build_client

_AUTH_TEST_URL = "https://slack.com/api/auth.test"


class SlackVerifier:
    name = "slack"
    entities = frozenset(
        {
            "SLACK_BOT_TOKEN",
            "SLACK_USER_TOKEN",
            "SLACK_APP_TOKEN",
            "SLACK_LEGACY_TOKEN",
        }
    )

    async def verify(
        self, value: str, *, ctx: VerifyContext
    ) -> VerificationResult:
        headers = {
            "Authorization": f"Bearer {value}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "pleno-pii-scanner",
        }
        async with build_client(ctx) as client:
            try:
                response = await client.post(_AUTH_TEST_URL, headers=headers)
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
        if response.status_code >= 500:
            return VerificationResult(
                state="error",
                detail=f"upstream {response.status_code}",
                ttl_seconds=60,
            )
        try:
            payload = response.json()
        except ValueError:
            return VerificationResult(state="error", detail="bad json", ttl_seconds=60)
        if not isinstance(payload, dict):
            return VerificationResult(state="error", detail="bad payload", ttl_seconds=60)
        if payload.get("ok") is True:
            metadata = {
                key: payload[key]
                for key in ("user_id", "team_id", "team", "user", "url")
                if key in payload
            }
            return VerificationResult(
                state="live",
                detail=f"valid token for team {payload.get('team_id', '?')}",
                metadata=metadata,
            )
        error = str(payload.get("error", "")) if payload.get("error") else ""
        if error == "ratelimited":
            return VerificationResult(
                state="rate_limited", detail="slack rate limited", ttl_seconds=60
            )
        if error in {"invalid_auth", "not_authed", "account_inactive", "token_revoked", "token_expired"}:
            return VerificationResult(state="revoked", detail=error)
        return VerificationResult(
            state="unknown", detail=f"slack error: {error}", ttl_seconds=300
        )
