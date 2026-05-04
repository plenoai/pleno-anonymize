"""Generic POST-JSON webhook transport with optional HMAC-SHA256 signing.

The signature header is `X-Pleno-Signature: sha256=<hex>` over the
serialized JSON body. Receivers verify with `verify_hmac()` exported
from this module.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Mapping

import httpx

from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    RetryPolicy,
    excerpt,
    retry_call,
    severity_for,
)

SIGNATURE_HEADER = "X-Pleno-Signature"


def _is_retryable(value: object) -> bool:
    if isinstance(value, httpx.Response):
        return value.status_code == 429 or value.status_code >= 500
    if isinstance(value, httpx.HTTPError):
        return True
    return False


def _signature(payload: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_hmac(payload: bytes, secret: str, header_value: str) -> bool:
    """Constant-time HMAC verifier for receivers."""
    expected = _signature(payload, secret)
    return hmac.compare_digest(expected, header_value)


class WebhookNotifier:
    name: str = "webhook"

    def __init__(
        self,
        *,
        url: str,
        secret: str | None = None,
        headers: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._secret = secret
        self._headers = dict(headers or {})
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._retry = retry_policy or RetryPolicy()

    async def send(self, batch: NotificationBatch) -> NotificationResult:
        if not batch.findings:
            return NotificationResult(
                transport=self.name, delivered=True, delivered_count=0
            )
        body_obj = self._serialize_batch(batch)
        body_bytes = json.dumps(
            body_obj, separators=(",", ":"), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        headers = {"content-type": "application/json", **self._headers}
        if self._secret:
            headers[SIGNATURE_HEADER] = _signature(body_bytes, self._secret)

        async def _op() -> httpx.Response:
            return await self._client.post(
                self._url, content=body_bytes, headers=headers
            )

        try:
            response, attempts = await retry_call(
                _op, policy=self._retry, is_retryable=_is_retryable
            )
        except httpx.HTTPError as exc:
            return NotificationResult(
                transport=self.name,
                delivered=False,
                delivered_count=0,
                error=f"transport error after {self._retry.max_attempts} attempts: {exc!r}",
            )

        assert isinstance(response, httpx.Response)
        if 200 <= response.status_code < 300:
            return NotificationResult(
                transport=self.name,
                delivered=True,
                delivered_count=len(batch.findings),
                response_code=response.status_code,
            )
        return NotificationResult(
            transport=self.name,
            delivered=False,
            delivered_count=0,
            error=f"webhook rejected after {attempts} attempts: HTTP {response.status_code}",
            response_code=response.status_code,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _serialize_batch(batch: NotificationBatch) -> dict:
        return {
            "scan_id": batch.scan_id,
            "severity_summary": dict(batch.severity_summary),
            "metadata": dict(batch.metadata),
            "findings": [
                {
                    "entity": f.entity,
                    "file": f.file,
                    "line": f.line,
                    "col": f.col,
                    "score": f.score,
                    "severity": severity_for(f),
                    "verification": f.verification,
                    "pattern_name": f.pattern_name,
                    "fingerprint": f.fingerprint(),
                    "excerpt": excerpt(f),
                }
                for f in batch.findings
            ],
        }
