"""Schedule registry — cron + SLA-driven re-scan (ADR-0007 §12).

The schedule layer is intentionally a leaf module: it depends only on
its own `cron` parser and the asyncio standard library. Integration
with `Scheduler` and `FindingsStore` is via caller-supplied callbacks
(`RunFn`, `SLAFinder`) so this package is testable without spinning up
either subsystem.
"""

from pleno_pii_scanner.schedule.base import (
    OnBreachFn,
    RunFn,
    SLABreach,
    SLAFinder,
    SLAPolicy,
    Schedule,
    ScheduleOutcome,
    ScheduleStore,
    Severity,
)
from pleno_pii_scanner.schedule.cron import CronExpression
from pleno_pii_scanner.schedule.memory_store import MemoryScheduleStore
from pleno_pii_scanner.schedule.registry import (
    JitterFn,
    NowFn,
    ScheduleRegistry,
)
from pleno_pii_scanner.schedule.sqlite_store import (
    SqliteScheduleStore,
    default_registry_path,
)

__all__ = [
    "CronExpression",
    "JitterFn",
    "MemoryScheduleStore",
    "NowFn",
    "OnBreachFn",
    "RunFn",
    "SLABreach",
    "SLAFinder",
    "SLAPolicy",
    "Schedule",
    "ScheduleOutcome",
    "ScheduleRegistry",
    "ScheduleStore",
    "Severity",
    "SqliteScheduleStore",
    "default_registry_path",
]
