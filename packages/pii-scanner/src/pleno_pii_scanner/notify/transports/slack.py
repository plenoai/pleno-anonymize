"""Slack incoming webhook transport.

Block Kit message: header + per-severity section + top-10 finding table.
Critical batches optionally @channel mention (off by default — opt-in).
"""

from __future__ import annotations

import os

import httpx

from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    RetryPolicy,
    SEVERITY_COLOR,
    SEVERITY_ORDER,
    excerpt,
    retry_call,
    severity_for,
)

_TOP_N = 10


def _is_retryable(value: object) -> bool:
    if isinstance(value, httpx.Response):
        return value.status_code == 429 or value.status_code >= 500
    if isinstance(value, httpx.HTTPError):
        return True
    return False


class SlackWebhookNotifier:
    """Notifier wrapping a single incoming webhook URL."""

    name: str = "slack"

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        channel_mention_on_critical: bool = False,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 10.0,
    ) -> None:
        url = webhook_url or os.environ.get("PLENO_SLACK_WEBHOOK_URL")
        if not url:
            raise ValueError(
                "SlackWebhookNotifier requires webhook_url or PLENO_SLACK_WEBHOOK_URL"
            )
        self._url = url
        self._mention = channel_mention_on_critical
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._retry = retry_policy or RetryPolicy()

    async def send(self, batch: NotificationBatch) -> NotificationResult:
        if not batch.findings:
            return NotificationResult(
                transport=self.name, delivered=True, delivered_count=0
            )
        payload = self._build_payload(batch)

        async def _op() -> httpx.Response:
            return await self._client.post(self._url, json=payload)

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
            error=f"slack rejected after {attempts} attempts: HTTP {response.status_code}",
            response_code=response.status_code,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _build_payload(self, batch: NotificationBatch) -> dict:
        blocks: list[dict] = []
        header_text = f"PII scan {batch.scan_id}: {len(batch.findings)} findings"
        if self._mention and batch.severity_summary.get("critical"):
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "<!channel>"}}
            )
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": header_text}}
        )

        summary_lines = []
        for sev in SEVERITY_ORDER:
            count = batch.severity_summary.get(sev, 0)
            if count:
                summary_lines.append(f"• *{sev}*: {count}")
        if summary_lines:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join(summary_lines)},
                }
            )

        top = batch.findings[:_TOP_N]
        rows = []
        for f in top:
            rows.append(
                f"`{severity_for(f)}` `{f.entity}` "
                f"`{f.file}:{f.line}` excerpt=`{excerpt(f)}`"
            )
        if rows:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "\n".join(rows)},
                }
            )
        if len(batch.findings) > _TOP_N:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"… {len(batch.findings) - _TOP_N} more not shown",
                        }
                    ],
                }
            )

        attachments = [
            {
                "color": SEVERITY_COLOR.get(sev, "#888888"),
                "fallback": f"{sev}: {batch.severity_summary.get(sev, 0)}",
            }
            for sev in SEVERITY_ORDER
            if batch.severity_summary.get(sev)
        ]
        return {
            "text": header_text,
            "blocks": blocks,
            "attachments": attachments,
        }
