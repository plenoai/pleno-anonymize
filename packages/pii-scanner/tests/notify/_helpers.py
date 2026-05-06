"""Shared test helpers for the notify subsystem."""

from __future__ import annotations

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    severity_for,
)


def make_finding(
    *,
    entity: str = "EMAIL",
    file: str = "src/app.py",
    line: int = 10,
    col: int = 1,
    score: float = 0.5,
    snippet: str = "user@example.com appears here",
    matched: str = "user@example.com",
    pattern_name: str = "email",
    verification: str = "unverified",
) -> Finding:
    return Finding(
        entity=entity,
        file=file,
        line=line,
        col=col,
        score=score,
        snippet=snippet,
        matched=matched,
        pattern_name=pattern_name,
        verification=verification,
    )


def make_batch(
    *findings: Finding, scan_id: str = "scan-1", **metadata
) -> NotificationBatch:
    summary: dict[str, int] = {}
    for f in findings:
        bucket = severity_for(f)
        summary[bucket] = summary.get(bucket, 0) + 1
    return NotificationBatch(
        scan_id=scan_id,
        findings=tuple(findings),
        severity_summary=summary,
        metadata=metadata,
    )
