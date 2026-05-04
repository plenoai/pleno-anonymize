"""OpenAI liveness verifier (GET /v1/models).

Covers classic sk-..., project sk-proj-..., and admin sk-admin-...
keys. /v1/models returns 200 + a list for any key with model.read.
"""

from __future__ import annotations

import httpx

from ..base import VerificationResult, VerifyContext
from ._http import build_client

_MODELS_URL = "https://api.openai.com/v1/models"


class OpenAiVerifier:
    name = "openai"
    entities = frozenset(
        {
            "OPENAI_API_KEY",
            "OPENAI_PROJECT_KEY",
            "OPENAI_ADMIN_KEY",
        }
    )

    async def verify(
        self, value: str, *, ctx: VerifyContext
    ) -> VerificationResult:
        headers = {
            "Authorization": f"Bearer {value}",
            "User-Agent": "pleno-pii-scanner",
        }
        async with build_client(ctx) as client:
            try:
                response = await client.get(_MODELS_URL, headers=headers)
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
        metadata: dict[str, object] = {}
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                metadata["model_count"] = len(data)
        return VerificationResult(
            state="live", detail="valid api key", metadata=metadata
        )
    if status == 401:
        return VerificationResult(state="revoked", detail="401 unauthorized")
    if status == 429:
        return VerificationResult(
            state="rate_limited", detail="openai rate limited", ttl_seconds=60
        )
    if 500 <= status < 600:
        return VerificationResult(
            state="error", detail=f"upstream {status}", ttl_seconds=60
        )
    return VerificationResult(
        state="unknown", detail=f"unexpected {status}", ttl_seconds=300
    )
