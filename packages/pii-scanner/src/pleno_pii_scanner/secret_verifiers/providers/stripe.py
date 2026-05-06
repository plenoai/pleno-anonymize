"""Stripe liveness verifier (GET /v1/account).

Stripe accepts both Bearer and HTTP basic for the secret key. We use
Bearer for parity with the other providers. sk_test_* keys hit the
test mode account; we mark them live but tag mode=test so notifier
rules can treat them as lower severity.
"""

from __future__ import annotations

import httpx

from ..base import VerificationResult, VerifyContext
from ._http import build_client

_ACCOUNT_URL = "https://api.stripe.com/v1/account"


class StripeVerifier:
    name = "stripe"
    entities = frozenset(
        {
            "STRIPE_LIVE_KEY",
            "STRIPE_RESTRICTED_KEY",
            "STRIPE_TEST_KEY",
        }
    )

    async def verify(self, value: str, *, ctx: VerifyContext) -> VerificationResult:
        headers = {
            "Authorization": f"Bearer {value}",
            "User-Agent": "pleno-pii-scanner",
        }
        async with build_client(ctx) as client:
            try:
                response = await client.get(_ACCOUNT_URL, headers=headers)
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
        return _classify(response, value)


def _mode_for(key: str) -> str:
    if key.startswith("sk_test_") or key.startswith("rk_test_"):
        return "test"
    return "live"


def _classify(response: httpx.Response, key: str) -> VerificationResult:
    status = response.status_code
    if status == 200:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        metadata: dict[str, object] = {"mode": _mode_for(key)}
        if isinstance(payload, dict):
            for field_name in ("id", "country", "default_currency", "business_profile"):
                if field_name in payload:
                    metadata[field_name] = payload[field_name]
        return VerificationResult(
            state="live",
            detail=f"valid {metadata['mode']} key for {metadata.get('id', '?')}",
            metadata=metadata,
        )
    if status == 401:
        return VerificationResult(state="revoked", detail="401 unauthorized")
    if status == 429:
        return VerificationResult(
            state="rate_limited", detail="stripe rate limited", ttl_seconds=60
        )
    if 500 <= status < 600:
        return VerificationResult(
            state="error", detail=f"upstream {status}", ttl_seconds=60
        )
    return VerificationResult(
        state="unknown", detail=f"unexpected {status}", ttl_seconds=300
    )
