"""Splunk HTTP Event Collector transport.

POST /services/collector/event with newline-separated JSON events.
Splits a batch into chunks honoring Splunk's defaults (5 MB body /
1000 events per request).
"""

from __future__ import annotations

import json
import socket
from typing import Iterable, Sequence

import httpx

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    RetryPolicy,
    excerpt,
    retry_call,
    severity_for,
)

DEFAULT_MAX_EVENTS = 1000
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_SOURCETYPE = "pleno:finding"


def _is_retryable(value: object) -> bool:
    if isinstance(value, httpx.Response):
        return value.status_code == 429 or value.status_code >= 500
    if isinstance(value, httpx.HTTPError):
        return True
    return False


class SplunkHECNotifier:
    name: str = "splunk"

    def __init__(
        self,
        *,
        url: str,
        token: str,
        host: str | None = None,
        sourcetype: str = DEFAULT_SOURCETYPE,
        index: str | None = None,
        max_events_per_request: int = DEFAULT_MAX_EVENTS,
        max_bytes_per_request: int = DEFAULT_MAX_BYTES,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._host = host or socket.gethostname()
        self._sourcetype = sourcetype
        self._index = index
        self._max_events = max_events_per_request
        self._max_bytes = max_bytes_per_request
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._retry = retry_policy or RetryPolicy()

    async def send(self, batch: NotificationBatch) -> NotificationResult:
        if not batch.findings:
            return NotificationResult(
                transport=self.name, delivered=True, delivered_count=0
            )
        chunks = list(self._chunks(batch.findings, batch))
        delivered_count = 0
        last_status: int | None = None
        for chunk_payload in chunks:

            async def _op(payload: bytes = chunk_payload) -> httpx.Response:
                return await self._client.post(
                    f"{self._url}/services/collector/event",
                    content=payload,
                    headers={
                        "Authorization": f"Splunk {self._token}",
                        "content-type": "application/json",
                    },
                )

            try:
                response, attempts = await retry_call(
                    _op, policy=self._retry, is_retryable=_is_retryable
                )
            except httpx.HTTPError as exc:
                return NotificationResult(
                    transport=self.name,
                    delivered=False,
                    delivered_count=delivered_count,
                    error=f"transport error after {self._retry.max_attempts} attempts: {exc!r}",
                )
            assert isinstance(response, httpx.Response)
            last_status = response.status_code
            if not (200 <= response.status_code < 300):
                return NotificationResult(
                    transport=self.name,
                    delivered=False,
                    delivered_count=delivered_count,
                    error=f"splunk rejected after {attempts} attempts: HTTP {response.status_code}",
                    response_code=response.status_code,
                )
            # Each chunk corresponds to a known slice of findings.
            delivered_count += chunk_payload.count(b"\n{") + 1
        return NotificationResult(
            transport=self.name,
            delivered=True,
            delivered_count=len(batch.findings),
            response_code=last_status,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _chunks(
        self, findings: Sequence[Finding], batch: NotificationBatch
    ) -> Iterable[bytes]:
        buf: list[bytes] = []
        size = 0
        for f in findings:
            event = self._event(f, batch)
            line = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
            line_size = len(line) + 1  # newline
            full_by_count = len(buf) >= self._max_events
            full_by_bytes = buf and (size + line_size) > self._max_bytes
            if full_by_count or full_by_bytes:
                yield b"\n".join(buf)
                buf = []
                size = 0
            buf.append(line)
            size += line_size
        if buf:
            yield b"\n".join(buf)

    def _event(self, f: Finding, batch: NotificationBatch) -> dict:
        event_body = {
            "scan_id": batch.scan_id,
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
            "metadata": dict(batch.metadata),
        }
        out = {
            "event": event_body,
            "sourcetype": self._sourcetype,
            "host": self._host,
        }
        if self._index:
            out["index"] = self._index
        return out
