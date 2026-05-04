"""Notifier protocol, batch / result dataclasses, and shared retry helper.

ADR-0007 §9 — multi-transport delivery layer. The Router (`router.py`)
selects which transports a finding goes to; the transports themselves
implement the `Notifier` protocol.

Severity / verification badge math is centralized here so transports
never re-derive it. Anything that touches the wire belongs in a
transport module; this file is pure data + control flow so it can be
unit tested without httpx.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable

from pleno_pii_scanner.models import Finding

# Severity ordering / palette is canonical — transports import from here.
SEVERITY_ORDER: tuple[str, ...] = ("critical", "high", "medium", "low", "info")

# Hex palette doubles as Slack attachment color and Splunk severity tag.
SEVERITY_COLOR: Mapping[str, str] = {
    "critical": "#d62728",
    "high": "#ff7f0e",
    "medium": "#f1c40f",
    "low": "#7f7f7f",
    "info": "#1f77b4",
}

# OTLP severity_number per spec: ERROR=17, WARN=13, INFO=9, DEBUG=5.
SEVERITY_OTEL_NUMBER: Mapping[str, int] = {
    "critical": 21,
    "high": 17,
    "medium": 13,
    "low": 9,
    "info": 5,
}


def severity_for(finding: Finding) -> str:
    """Project Finding -> notification severity bucket.

    verification=passed bumps to critical (ADR §7), failed downgrades to
    info (likely false positive). Rest fall back to medium so a transport
    never sees an unknown bucket.
    """
    if finding.verification == "passed":
        return "critical"
    if finding.verification == "failed":
        return "info"
    if finding.score >= 0.9:
        return "high"
    if finding.score >= 0.6:
        return "medium"
    return "low"


def excerpt(finding: Finding, *, max_len: int = 64) -> str:
    """Masked excerpt safe for notification bodies.

    Raw `matched` value MUST NEVER cross the wire; this is the only
    function that handles that data and it always masks.
    """
    raw = finding.matched
    if len(raw) <= 4:
        return "*" * len(raw)
    keep = min(2, len(raw) // 4)
    body = "*" * max(1, len(raw) - keep * 2)
    masked = f"{raw[:keep]}{body}{raw[-keep:]}"
    if len(masked) > max_len:
        masked = masked[: max_len - 1] + "…"
    return masked


@dataclass(frozen=True, slots=True)
class NotificationBatch:
    """One batch sent atomically per delivery (transport-dependent grouping)."""

    scan_id: str
    findings: tuple[Finding, ...]
    severity_summary: Mapping[str, int]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def filtered(self, predicate) -> "NotificationBatch":
        """Return a new batch with `findings` reduced by `predicate(finding)`.

        severity_summary is recomputed from the filtered findings so
        downstream transports never see counts that disagree with the
        list they were handed.
        """
        kept = tuple(f for f in self.findings if predicate(f))
        summary: dict[str, int] = {}
        for f in kept:
            summary[severity_for(f)] = summary.get(severity_for(f), 0) + 1
        return NotificationBatch(
            scan_id=self.scan_id,
            findings=kept,
            severity_summary=summary,
            metadata=self.metadata,
        )


@dataclass(frozen=True, slots=True)
class NotificationResult:
    transport: str
    delivered: bool
    delivered_count: int
    error: str | None = None
    response_code: int | None = None


@runtime_checkable
class Notifier(Protocol):
    name: str

    async def send(self, batch: NotificationBatch) -> NotificationResult: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with jitter; shared by Slack / webhook / Splunk."""

    max_attempts: int = 3
    initial_seconds: float = 1.0
    factor: float = 2.0
    max_seconds: float = 30.0
    jitter: float = 0.1

    def delay(self, attempt: int) -> float:
        # attempt is 1-indexed; first retry waits initial_seconds.
        base = min(
            self.max_seconds,
            self.initial_seconds * (self.factor ** (attempt - 1)),
        )
        jitter_span = base * self.jitter
        return max(0.0, base + random.uniform(-jitter_span, jitter_span))


async def retry_call(
    op,
    *,
    policy: RetryPolicy,
    is_retryable,
    sleep=asyncio.sleep,
):
    """Execute `op()` with retry. Returns (last_result, attempts).

    `is_retryable(result_or_exc)` decides retry. Raises only the final
    exception if all attempts raise; otherwise returns the last result
    even if not delivered, so the caller can still build a
    NotificationResult.
    """
    last_exc: BaseException | None = None
    last_result = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            last_result = await op()
            last_exc = None
            if not is_retryable(last_result):
                return last_result, attempt
        except Exception as exc:
            last_exc = exc
            if not is_retryable(exc):
                raise
        if attempt < policy.max_attempts:
            await sleep(policy.delay(attempt))
    if last_exc is not None and last_result is None:
        raise last_exc
    return last_result, policy.max_attempts
