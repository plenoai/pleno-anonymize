"""ScheduleRegistry — drives cron + SLA-driven re-scan (ADR-0007 §12).

Responsibilities:

  * Register / unregister / enable / disable schedules
  * On each `tick(now)`:
      1. Fire every schedule whose next_run_at has passed (with jitter)
      2. Ask the SLA finder for currently-breached findings and re-fire
         the corresponding plan_refs immediately (deduped against the
         scheduled fires already in flight this tick)
      3. Persist updated next_run_at / last_outcome
  * `run_forever(interval)` is a thin loop around `tick`. The CLI / a
    long-lived daemon owns the loop; the registry stays callable from
    one-shot scripts via `tick()` alone.

Concurrency model:

  * Each fire runs in its own asyncio.Task — slow scans never block the
    tick that scheduled them.
  * `_inflight` tracks plan_refs currently running so a SLA breach for
    a plan that is already mid-scan does NOT enqueue a duplicate run.
  * The registry never cancels an in-flight task; if the operator
    disables a schedule mid-run, the existing run completes and only
    future fires are skipped.

What the registry does NOT do:

  * It does not call FindingsStore directly. Caller passes an SLAFinder
    callback that materialises breaches; tests inject a fake.
  * It does not implement persistence — that's `ScheduleStore`.
  * It does not own a Scheduler — `RunFn` is whatever the caller wants.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from pleno_pii_scanner.schedule.base import (
    OnBreachFn,
    RunFn,
    SLABreach,
    SLAFinder,
    SLAPolicy,
    Schedule,
    ScheduleOutcome,
    ScheduleStore,
)
from pleno_pii_scanner.schedule.cron import CronExpression


logger = logging.getLogger(__name__)


# Time / jitter injection points so tests stay deterministic without
# patching `datetime.now` or seeding the global RNG. Production uses
# `_default_now` (UTC) and `random.SystemRandom().uniform`.
NowFn = Callable[[], datetime]
JitterFn = Callable[[float, float], float]


def _default_now() -> datetime:
    return datetime.now(UTC)


def _default_jitter(lo: float, hi: float) -> float:
    return random.SystemRandom().uniform(lo, hi)


class ScheduleRegistry:
    """Manage a collection of recurring schedules + SLA-driven re-scans."""

    def __init__(
        self,
        store: ScheduleStore,
        run_fn: RunFn,
        *,
        sla_policy: SLAPolicy | None = None,
        sla_finder: SLAFinder | None = None,
        on_breach: OnBreachFn | None = None,
        now_fn: NowFn = _default_now,
        jitter_fn: JitterFn = _default_jitter,
    ) -> None:
        self._store = store
        self._run_fn = run_fn
        self._sla_policy = sla_policy
        self._sla_finder = sla_finder
        self._on_breach = on_breach
        self._now_fn = now_fn
        self._jitter_fn = jitter_fn
        self._inflight: set[str] = set()
        self._lock = asyncio.Lock()

    # --- registration -----------------------------------------------------

    async def register(self, schedule: Schedule) -> Schedule:
        """Persist `schedule`; computes next_run_at if not already set.

        Returns the (possibly mutated) Schedule actually stored so the
        caller can read back the computed `next_run_at`.
        """
        if schedule.next_run_at is None:
            schedule = replace(
                schedule, next_run_at=schedule.cron.next_after(self._now_fn())
            )
        await self._store.save(schedule)
        return schedule

    async def unregister(self, schedule_id: str) -> None:
        await self._store.delete(schedule_id)

    async def set_enabled(self, schedule_id: str, enabled: bool) -> None:
        existing = await self._store.load(schedule_id)
        if existing is None:
            raise KeyError(schedule_id)
        await self._store.save(replace(existing, enabled=enabled))

    async def list_schedules(self) -> list[Schedule]:
        return await self._store.list()

    # --- ticking ----------------------------------------------------------

    async def tick(self) -> list[asyncio.Task[None]]:
        """Fire any due cron schedules + any SLA-breach re-scans.

        Returns the list of spawned Tasks so callers (especially tests)
        can `await asyncio.gather(*tasks)` to wait for completion. The
        registry itself never blocks on the spawned work — that would
        defeat the point of running tick() on a fixed cadence.
        """
        now = self._now_fn()
        tasks: list[asyncio.Task[None]] = []

        for sched in await self._store.due(now):
            if not await self._claim(sched.plan_ref):
                # Already running from a previous tick or from an SLA
                # breach earlier in this tick — skip without rescheduling
                # so the in-flight task completes and reschedules itself.
                continue
            tasks.append(asyncio.create_task(self._fire(sched)))

        if self._sla_policy is not None and self._sla_finder is not None:
            for breach in await self._sla_finder(self._sla_policy, now):
                if not await self._claim(breach.plan_ref):
                    continue
                if self._on_breach is not None:
                    # WHY: notify *before* spawning so a notifier crash is
                    # surfaced in the tick caller's error path rather than
                    # swallowed by the spawned task.
                    await self._on_breach(breach)
                tasks.append(asyncio.create_task(self._fire_breach(breach)))

        return tasks

    async def run_forever(
        self,
        interval: float = 30.0,
        *,
        cancel: asyncio.Event | None = None,
    ) -> None:
        """Tick on a fixed cadence until `cancel` is set or the task is cancelled.

        `interval` should be smaller than the smallest cron resolution
        you care about (default 30s comfortably handles `* * * * *`).
        Setting it too small wastes CPU on empty `due()` queries; setting
        it larger than 60s makes minute-resolution schedules late.
        """
        if interval <= 0:
            raise ValueError("interval must be > 0")
        cancel = cancel or asyncio.Event()
        while not cancel.is_set():
            try:
                await self.tick()
            except Exception:
                # WHY: tick() failures must not kill the daemon. Log and
                # carry on — the next tick will retry.
                logger.exception("schedule tick failed; continuing")
            try:
                await asyncio.wait_for(cancel.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    # --- internals --------------------------------------------------------

    async def _claim(self, plan_ref: str) -> bool:
        """Atomically reserve `plan_ref`; returns False if already claimed."""
        async with self._lock:
            if plan_ref in self._inflight:
                return False
            self._inflight.add(plan_ref)
            return True

    async def _release(self, plan_ref: str) -> None:
        async with self._lock:
            self._inflight.discard(plan_ref)

    async def _fire(self, sched: Schedule) -> None:
        """Run `sched` once, persist outcome, schedule the next fire."""
        try:
            await self._apply_jitter(sched)
            ran_at = self._now_fn()
            outcome, error = await self._invoke_run(sched)
            next_run = sched.cron.next_after(ran_at)
            await self._store.save(
                sched.with_run(
                    ran_at=ran_at,
                    next_run_at=next_run,
                    outcome=outcome,
                    error=error,
                )
            )
        finally:
            await self._release(sched.plan_ref)

    async def _fire_breach(self, breach: SLABreach) -> None:
        """Run a synthetic schedule for an SLA-breached plan_ref.

        We synthesize a transient Schedule (no cron, jitter=0) instead of
        looking up the real one so a re-scan still happens even if the
        operator deleted the original schedule but the finding is still
        open. The synthetic run is NOT persisted; only the real schedule
        (if any) carries last_run state, which is fine because the
        finding's status update is the durable record of the re-scan.
        """
        try:
            await self._invoke_run(
                Schedule(
                    id=f"__sla__:{breach.finding_id}",
                    # Use a placeholder cron so callers that introspect
                    # schedule.cron.expr in their RunFn for logging see
                    # an explicit marker.
                    cron=_SLA_PLACEHOLDER_CRON,
                    plan_ref=breach.plan_ref,
                    jitter_seconds=0,
                    enabled=True,
                    tags=("sla-rescan", breach.severity),
                )
            )
        finally:
            await self._release(breach.plan_ref)

    async def _apply_jitter(self, sched: Schedule) -> None:
        if sched.jitter_seconds > 0:
            delay = self._jitter_fn(0.0, float(sched.jitter_seconds))
            await asyncio.sleep(delay)

    async def _invoke_run(self, sched: Schedule) -> tuple[ScheduleOutcome, str | None]:
        try:
            outcome = await self._run_fn(sched)
            return outcome, None
        except Exception as exc:
            logger.exception(
                "schedule run failed: id=%s plan_ref=%s", sched.id, sched.plan_ref
            )
            return ScheduleOutcome.FAILED, f"{type(exc).__name__}: {exc}"


# Placeholder for breach-fire synthetic schedules. We never advance off
# this cron (breach fires are one-shot and never re-saved), but RunFn
# implementations log `sched.cron.expr` and we want them to see a marker
# instead of an arbitrary user expression. `* * * * *` is the cheapest
# always-valid expression to parse.
_SLA_PLACEHOLDER_CRON = CronExpression.parse("* * * * *")


__all__ = ["JitterFn", "NowFn", "ScheduleRegistry"]
