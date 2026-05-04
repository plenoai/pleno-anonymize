"""ScheduleRegistry types — Schedule / SLAPolicy / ScheduleStore (ADR-0007 §12).

The ScheduleRegistry holds zero direct knowledge of how a SourcePlan
is constructed: schedules carry an opaque `plan_ref` string, and the
caller supplies a `RunFn` that translates `plan_ref` into a real run
(typically `Scheduler.run_one`). This keeps the registry independent
of the connector layer — schedules can target any unit of work that
the operator can name.

SLA-driven re-scan is delegated through the same indirection: the
caller passes an `SLAFinder` callback that returns currently-breached
findings, and the registry decides which `plan_ref`s to re-enqueue.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pleno_pii_scanner.schedule.cron import CronExpression


class ScheduleOutcome(StrEnum):
    """Result of the most recent run for a Schedule."""

    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


# Severity strings mirror findings_store.base.Severity but redeclared as
# a tuple of literals to keep `schedule` a leaf module that does not pull
# the heavy findings_store package into the import graph.
Severity = str  # one of "critical" | "high" | "medium" | "low"


@dataclass(frozen=True, slots=True)
class SLAPolicy:
    """Per-severity max-age before an open finding triggers a re-scan.

    `max_age` of None means "no SLA for this severity" — useful for
    `low`, where re-scanning every few days is wasted budget.
    """

    by_severity: dict[Severity, timedelta | None] = field(default_factory=dict)

    @classmethod
    def default(cls) -> SLAPolicy:
        # ADR-0007 §12: critical=1h, high=24h, medium=7d, low=never.
        return cls(
            by_severity={
                "critical": timedelta(hours=1),
                "high": timedelta(hours=24),
                "medium": timedelta(days=7),
                "low": None,
            }
        )

    def deadline(self, severity: Severity, opened_at: datetime) -> datetime | None:
        """Return when an `opened_at` finding of `severity` breaches SLA.

        `None` means the severity has no SLA configured — caller should
        treat that as "never breaches" rather than "0 second breach".
        """
        max_age = self.by_severity.get(severity)
        if max_age is None:
            return None
        return opened_at + max_age


@dataclass(frozen=True, slots=True)
class SLABreach:
    """One open finding that has aged past its severity SLA."""

    finding_id: str
    severity: Severity
    source_id: str
    plan_ref: str
    opened_at: datetime
    breached_at: datetime


@dataclass(frozen=True, slots=True)
class Schedule:
    """A single recurring scan registration.

    `plan_ref` is opaque: it identifies what to scan, but the registry
    never inspects it. The caller's `RunFn` translates it back to a
    real `SourcePlan` (or whatever the integration uses).

    `jitter_seconds` randomises the actual fire time inside
    `[next_run_at, next_run_at + jitter_seconds]`. Required for any
    schedule that fans out across many tenants — without jitter, every
    `0 * * * *` schedule fires at xx:00:00 simultaneously and overruns
    the GlobalRateLimiter for that connector_kind.

    `tags` and `metadata` are caller-defined annotations; the registry
    only stores and round-trips them.
    """

    id: str
    cron: CronExpression
    plan_ref: str
    jitter_seconds: int = 0
    enabled: bool = True
    tags: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_outcome: ScheduleOutcome | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Schedule.id must be non-empty")
        if not self.plan_ref:
            raise ValueError("Schedule.plan_ref must be non-empty")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds must be >= 0")

    def with_run(
        self,
        *,
        ran_at: datetime,
        next_run_at: datetime,
        outcome: ScheduleOutcome,
        error: str | None,
    ) -> Schedule:
        """Return a copy reflecting the most recent run + the next fire."""
        return replace(
            self,
            last_run_at=ran_at,
            next_run_at=next_run_at,
            last_outcome=outcome,
            last_error=error,
        )


# Caller-supplied translator: `plan_ref` → executes the actual scan.
# Returns the outcome the registry should record. Any exception raised
# is caught by the registry, surfaced as ScheduleOutcome.FAILED with
# `last_error = repr(exc)`.
RunFn = Callable[[Schedule], Awaitable[ScheduleOutcome]]

# Caller-supplied SLA inspector: returns currently-breached findings for
# severities the registry asks about. Decoupling this from FindingsStore
# means tests can inject a tiny fake instead of standing up SQLite.
SLAFinder = Callable[[SLAPolicy, datetime], Awaitable[list[SLABreach]]]

# Optional hook called once per SLA breach the registry acts on. Lets
# the caller emit notifications or audit events without subclassing.
OnBreachFn = Callable[[SLABreach], Awaitable[None]]


@runtime_checkable
class ScheduleStore(Protocol):
    """Persistence contract for Schedules.

    Implementations MUST be safe to call concurrently from multiple
    asyncio tasks. Last-writer-wins for the same `id`; independent ids
    never block each other beyond the implementation's writer
    serialisation.
    """

    async def save(self, schedule: Schedule) -> None:
        """Upsert a schedule."""
        ...

    async def load(self, schedule_id: str) -> Schedule | None:
        """Return the named schedule, or None if absent."""
        ...

    async def list(self) -> list[Schedule]:
        """Return every schedule in the store."""
        ...

    async def delete(self, schedule_id: str) -> None:
        """Remove a schedule. No-op if absent."""
        ...

    async def due(self, now: datetime) -> list[Schedule]:
        """Return enabled schedules whose `next_run_at` <= `now`."""
        ...

    async def close(self) -> None:
        """Release the underlying connection / file handle."""
        ...


__all__ = [
    "OnBreachFn",
    "RunFn",
    "SLABreach",
    "SLAFinder",
    "SLAPolicy",
    "Schedule",
    "ScheduleOutcome",
    "ScheduleStore",
    "Severity",
]
