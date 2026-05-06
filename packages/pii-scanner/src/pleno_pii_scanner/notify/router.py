"""RoutingRule + Router — fan-out NotificationBatch to selected transports.

Severity / verification / entity / source_kind glob filters per rule.
A finding may match multiple rules (fan-out, no warning). Findings that
match no rule fall through to `default_transport` if configured, or are
dropped with a stderr warning.
"""

from __future__ import annotations

import asyncio
import fnmatch
import sys
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    Notifier,
    severity_for,
)


@dataclass(frozen=True, slots=True)
class RoutingRule:
    name: str
    severity_in: frozenset[str] = frozenset()
    verification_in: frozenset[str] = frozenset()
    entity_pattern: str | None = None
    source_kind_pattern: str | None = None
    transports: tuple[str, ...] = ()

    def matches(self, finding: Finding, source_kind: str | None) -> bool:
        if self.severity_in and severity_for(finding) not in self.severity_in:
            return False
        if self.verification_in and finding.verification not in self.verification_in:
            return False
        if self.entity_pattern and not fnmatch.fnmatchcase(
            finding.entity, self.entity_pattern
        ):
            return False
        if self.source_kind_pattern:
            if source_kind is None:
                return False
            if not fnmatch.fnmatchcase(source_kind, self.source_kind_pattern):
                return False
        return True


@dataclass(frozen=True, slots=True)
class _Bucket:
    transport: str
    findings: list[Finding] = field(default_factory=list)


class Router:
    """Fan-out batch over configured rules and notifiers.

    Routing is deterministic: rules evaluated in declaration order; per
    transport the finding order matches batch order. Async fan-out is
    via `asyncio.gather` so a slow Slack does not block fast SMTP.
    """

    def __init__(
        self,
        rules: Sequence[RoutingRule],
        notifiers: Mapping[str, Notifier],
        *,
        default_transport: str | None = None,
    ) -> None:
        self._rules = tuple(rules)
        self._notifiers = dict(notifiers)
        self._default_transport = default_transport
        unknown_in_rules: list[str] = []
        for rule in self._rules:
            for t in rule.transports:
                if t not in self._notifiers:
                    unknown_in_rules.append(f"{rule.name}->{t}")
        if unknown_in_rules:
            raise ValueError(
                f"Routing rule references unknown transport(s): {', '.join(unknown_in_rules)}"
            )
        if default_transport is not None and default_transport not in self._notifiers:
            raise ValueError(
                f"Default transport {default_transport!r} not in notifiers"
            )

    @property
    def rules(self) -> tuple[RoutingRule, ...]:
        return self._rules

    @property
    def notifiers(self) -> Mapping[str, Notifier]:
        return dict(self._notifiers)

    async def route(self, batch: NotificationBatch) -> list[NotificationResult]:
        if not batch.findings:
            return []
        source_kind = batch.metadata.get("source_kind")

        # Per-transport bucket of findings; preserves rule fan-out
        # (one finding may sit in multiple buckets).
        buckets: dict[str, list[Finding]] = {}
        unmatched: list[Finding] = []
        for finding in batch.findings:
            matched_any = False
            already_in: set[str] = set()
            for rule in self._rules:
                if not rule.matches(finding, source_kind):
                    continue
                matched_any = True
                for t in rule.transports:
                    if t in already_in:
                        continue
                    already_in.add(t)
                    buckets.setdefault(t, []).append(finding)
            if not matched_any:
                unmatched.append(finding)

        if unmatched:
            if self._default_transport is not None:
                buckets.setdefault(self._default_transport, []).extend(unmatched)
            else:
                sys.stderr.write(
                    f"[notify.Router] dropping {len(unmatched)} finding(s) "
                    f"for scan_id={batch.scan_id}: no matching rule and no default_transport\n"
                )

        if not buckets:
            return []

        async def _send(transport: str, findings: list[Finding]) -> NotificationResult:
            sub = NotificationBatch(
                scan_id=batch.scan_id,
                findings=tuple(findings),
                severity_summary=_summarise(findings),
                metadata=batch.metadata,
            )
            try:
                return await self._notifiers[transport].send(sub)
            except Exception as exc:
                # Transport contract: never raise; degrade to NotificationResult.
                # We still defend here so a buggy transport cannot poison fan-out.
                return NotificationResult(
                    transport=transport,
                    delivered=False,
                    delivered_count=0,
                    error=f"unhandled transport error: {exc!r}",
                )

        results = await asyncio.gather(*[_send(t, fs) for t, fs in buckets.items()])
        return list(results)

    async def close(self) -> None:
        await asyncio.gather(
            *[n.close() for n in self._notifiers.values()],
            return_exceptions=True,
        )


def _summarise(findings: Sequence[Finding]) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        bucket = severity_for(f)
        out[bucket] = out.get(bucket, 0) + 1
    return out
