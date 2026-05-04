"""Jira Cloud issue create / dedupe transport.

httpx 直叩き — atlassian-python-api を依存に持ち込まない。
For each finding, we use its `fingerprint()` as a stable identifier in
the issue summary; if a JQL search finds an open issue with that
fingerprint, we add a comment instead of creating a duplicate.
"""

from __future__ import annotations

import base64

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
from pleno_pii_scanner.notify._adf import build_issue_adf, comment_adf

OPEN_STATUS_JQL = '("To Do", "In Progress", "Open", "Reopened")'


class JiraNotifier:
    name: str = "jira"

    def __init__(
        self,
        *,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        issue_type: str = "Task",
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._project_key = project_key
        self._issue_type = issue_type
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._retry = retry_policy or RetryPolicy()
        token = base64.b64encode(f"{email}:{api_token}".encode("utf-8")).decode("ascii")
        self._auth_header = f"Basic {token}"

    async def send(self, batch: NotificationBatch) -> NotificationResult:
        if not batch.findings:
            return NotificationResult(
                transport=self.name, delivered=True, delivered_count=0
            )

        # One issue per finding so dedup keys are 1:1 with fingerprints.
        # Aggregating in a single issue would mask new findings that
        # arrive between scans.
        delivered = 0
        last_status: int | None = None
        for finding in batch.findings:
            try:
                status = await self._upsert_one(finding, batch)
            except httpx.HTTPError as exc:
                return NotificationResult(
                    transport=self.name,
                    delivered=False,
                    delivered_count=delivered,
                    error=f"jira transport error: {exc!r}",
                )
            if status is None:
                return NotificationResult(
                    transport=self.name,
                    delivered=False,
                    delivered_count=delivered,
                    error="jira rejected after retries",
                )
            last_status = status
            delivered += 1
        return NotificationResult(
            transport=self.name,
            delivered=True,
            delivered_count=delivered,
            response_code=last_status,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _upsert_one(
        self, finding: Finding, batch: NotificationBatch
    ) -> int | None:
        existing_key = await self._find_existing(finding)
        if existing_key is not None:
            return await self._comment(existing_key, finding, batch)
        return await self._create(finding, batch)

    async def _find_existing(self, finding: Finding) -> str | None:
        fp = finding.fingerprint()
        jql = (
            f'project = {self._project_key} '
            f'AND status in {OPEN_STATUS_JQL} '
            f'AND summary ~ "{fp}"'
        )

        async def _op() -> httpx.Response:
            return await self._client.get(
                f"{self._base_url}/rest/api/3/search",
                params={"jql": jql, "fields": "summary"},
                headers=self._headers(),
            )

        response, _ = await retry_call(
            _op, policy=self._retry, is_retryable=_is_retryable
        )
        assert isinstance(response, httpx.Response)
        if not (200 <= response.status_code < 300):
            return None
        issues = response.json().get("issues", [])
        if not issues:
            return None
        return issues[0]["key"]

    async def _create(self, finding: Finding, batch: NotificationBatch) -> int | None:
        body = {
            "fields": {
                "project": {"key": self._project_key},
                "issuetype": {"name": self._issue_type},
                "summary": _summary(finding),
                "description": build_issue_adf(
                    scan_id=batch.scan_id,
                    findings=[finding],
                    severity_summary={severity_for(finding): 1},
                    metadata=batch.metadata,
                ),
                "labels": ["pleno-pii", f"severity-{severity_for(finding)}"],
            }
        }

        async def _op() -> httpx.Response:
            return await self._client.post(
                f"{self._base_url}/rest/api/3/issue",
                json=body,
                headers=self._headers(),
            )

        response, _ = await retry_call(
            _op, policy=self._retry, is_retryable=_is_retryable
        )
        assert isinstance(response, httpx.Response)
        if not (200 <= response.status_code < 300):
            return None
        return response.status_code

    async def _comment(
        self, issue_key: str, finding: Finding, batch: NotificationBatch
    ) -> int | None:
        body = {
            "body": comment_adf(
                f"Re-detected in scan {batch.scan_id} at {finding.file}:{finding.line} "
                f"(severity={severity_for(finding)}, excerpt={excerpt(finding)})."
            )
        }

        async def _op() -> httpx.Response:
            return await self._client.post(
                f"{self._base_url}/rest/api/3/issue/{issue_key}/comment",
                json=body,
                headers=self._headers(),
            )

        response, _ = await retry_call(
            _op, policy=self._retry, is_retryable=_is_retryable
        )
        assert isinstance(response, httpx.Response)
        if not (200 <= response.status_code < 300):
            return None
        return response.status_code

    def _headers(self) -> dict:
        return {
            "Authorization": self._auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def _summary(f: Finding) -> str:
    return (
        f"[pleno-pii] {f.entity} in {f.file}:{f.line} "
        f"({severity_for(f)}, fp={f.fingerprint()})"
    )


def _is_retryable(value: object) -> bool:
    if isinstance(value, httpx.Response):
        return value.status_code == 429 or value.status_code >= 500
    if isinstance(value, httpx.HTTPError):
        return True
    return False
