"""Generic Bearer-token liveness verifier.

For internal APIs whose only signal is HTTP status. Configured per
TOML block; the registry instantiates one verifier per (entity, url)
pair. Defaults match the common "200 = live, 401 = revoked" shape.
"""

from __future__ import annotations

import httpx

from ..base import VerificationResult, VerifyContext
from ._http import build_client


class GenericBearerVerifier:
    name = "generic_bearer"
    entities: frozenset[str]

    def __init__(
        self,
        *,
        url: str = "",
        entity: str = "GENERIC_BEARER_TOKEN",
        success_status: int = 200,
        revoked_status: int = 401,
        method: str = "GET",
        name: str | None = None,
    ) -> None:
        # url="" is a sentinel allowed only for the placeholder default
        # registration; calling verify() in that state raises so an
        # operator can never accidentally probe an unset endpoint.
        self._url = url
        self._success = success_status
        self._revoked = revoked_status
        self._method = method.upper()
        self.entities = frozenset({entity})
        if name is not None:
            self.name = name

    async def verify(
        self, value: str, *, ctx: VerifyContext
    ) -> VerificationResult:
        if not self._url:
            raise RuntimeError(
                "GenericBearerVerifier.url is unset; configure via TOML before use"
            )
        headers = {
            "Authorization": f"Bearer {value}",
            "User-Agent": "pleno-pii-scanner",
        }
        async with build_client(ctx) as client:
            try:
                response = await client.request(
                    self._method, self._url, headers=headers
                )
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
        return self._classify(response)

    def _classify(self, response: httpx.Response) -> VerificationResult:
        status = response.status_code
        if status == self._success:
            return VerificationResult(
                state="live",
                detail=f"{self._method} {self._url} -> {status}",
                metadata={"status": status},
            )
        if status == self._revoked:
            return VerificationResult(
                state="revoked", detail=f"{self._method} {self._url} -> {status}"
            )
        if status == 429:
            return VerificationResult(
                state="rate_limited", detail="rate limited", ttl_seconds=60
            )
        if 500 <= status < 600:
            return VerificationResult(
                state="error", detail=f"upstream {status}", ttl_seconds=60
            )
        return VerificationResult(
            state="unknown", detail=f"unexpected {status}", ttl_seconds=300
        )
