"""Tests for ScheduleRegistry — cron firing, SLA breaches, concurrency."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from pleno_pii_scanner.schedule.base import (
    SLABreach,
    SLAPolicy,
    Schedule,
    ScheduleOutcome,
)
from pleno_pii_scanner.schedule.cron import CronExpression
from pleno_pii_scanner.schedule.memory_store import MemoryScheduleStore
from pleno_pii_scanner.schedule.registry import ScheduleRegistry


def _frozen_now(t: datetime):
    """Return a now_fn callable that always returns the same instant.

    Tests run in real-time wall-clock terms, but cron arithmetic must be
    deterministic. Mutating a list keeps "advance time" easy.
    """
    box = [t]
    return box, lambda: box[0]


class _Recorder:
    """RunFn double that records each invocation and returns a fixed outcome."""

    def __init__(
        self,
        outcome: ScheduleOutcome = ScheduleOutcome.SUCCESS,
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[Schedule] = []
        self._outcome = outcome
        self._raises = raises

    async def __call__(self, sched: Schedule) -> ScheduleOutcome:
        self.calls.append(sched)
        if self._raises is not None:
            raise self._raises
        return self._outcome


def _hourly_schedule(
    id: str = "s1", plan_ref: str = "plan-1", jitter_seconds: int = 0
) -> Schedule:
    return Schedule(
        id=id,
        cron=CronExpression.parse("@hourly"),
        plan_ref=plan_ref,
        jitter_seconds=jitter_seconds,
    )


# --- registration ----------------------------------------------------------


class TestRegister:
    async def test_register_computes_next_run_at(self) -> None:
        store = MemoryScheduleStore()
        _, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(store, _Recorder(), now_fn=now_fn)
        s = await reg.register(_hourly_schedule())
        assert s.next_run_at == datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        # Round-tripped through the store as well.
        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.next_run_at == s.next_run_at

    async def test_register_preserves_existing_next_run_at(self) -> None:
        store = MemoryScheduleStore()
        _, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(store, _Recorder(), now_fn=now_fn)
        explicit = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
        s = await reg.register(
            Schedule(
                id="s",
                cron=CronExpression.parse("@hourly"),
                plan_ref="p",
                next_run_at=explicit,
            )
        )
        assert s.next_run_at == explicit

    async def test_unregister(self) -> None:
        store = MemoryScheduleStore()
        reg = ScheduleRegistry(store, _Recorder())
        await reg.register(_hourly_schedule())
        await reg.unregister("s1")
        assert await store.load("s1") is None

    async def test_set_enabled_toggles(self) -> None:
        store = MemoryScheduleStore()
        reg = ScheduleRegistry(store, _Recorder())
        await reg.register(_hourly_schedule())
        await reg.set_enabled("s1", False)
        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.enabled is False
        await reg.set_enabled("s1", True)
        loaded2 = await store.load("s1")
        assert loaded2 is not None
        assert loaded2.enabled is True

    async def test_set_enabled_missing_raises(self) -> None:
        reg = ScheduleRegistry(MemoryScheduleStore(), _Recorder())
        with pytest.raises(KeyError):
            await reg.set_enabled("missing", True)

    async def test_list_schedules(self) -> None:
        store = MemoryScheduleStore()
        reg = ScheduleRegistry(store, _Recorder())
        await reg.register(_hourly_schedule(id="a", plan_ref="pa"))
        await reg.register(_hourly_schedule(id="b", plan_ref="pb"))
        ids = {s.id for s in await reg.list_schedules()}
        assert ids == {"a", "b"}


# --- ticking ---------------------------------------------------------------


class TestTickCron:
    async def test_due_schedule_fires(self) -> None:
        store = MemoryScheduleStore()
        runner = _Recorder()
        box, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(store, runner, now_fn=now_fn)
        await reg.register(_hourly_schedule())
        # Advance past the computed next_run_at (13:00).
        box[0] = datetime(2026, 5, 4, 13, 5, tzinfo=UTC)
        await asyncio.gather(*(await reg.tick()))
        assert len(runner.calls) == 1
        assert runner.calls[0].id == "s1"

    async def test_not_yet_due_does_not_fire(self) -> None:
        store = MemoryScheduleStore()
        runner = _Recorder()
        box, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(store, runner, now_fn=now_fn)
        await reg.register(_hourly_schedule())
        # Still before next_run_at.
        box[0] = datetime(2026, 5, 4, 12, 45, tzinfo=UTC)
        tasks = await reg.tick()
        assert tasks == []
        assert runner.calls == []

    async def test_fire_advances_next_run_at(self) -> None:
        store = MemoryScheduleStore()
        runner = _Recorder()
        box, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(store, runner, now_fn=now_fn)
        await reg.register(_hourly_schedule())
        box[0] = datetime(2026, 5, 4, 13, 5, tzinfo=UTC)
        await asyncio.gather(*(await reg.tick()))
        loaded = await store.load("s1")
        assert loaded is not None
        # ran_at = 13:05, next hourly = 14:00.
        assert loaded.last_run_at == datetime(2026, 5, 4, 13, 5, tzinfo=UTC)
        assert loaded.next_run_at == datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
        assert loaded.last_outcome is ScheduleOutcome.SUCCESS
        assert loaded.last_error is None

    async def test_run_failure_recorded(self) -> None:
        store = MemoryScheduleStore()
        runner = _Recorder(raises=RuntimeError("boom"))
        box, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(store, runner, now_fn=now_fn)
        await reg.register(_hourly_schedule())
        box[0] = datetime(2026, 5, 4, 13, 5, tzinfo=UTC)
        await asyncio.gather(*(await reg.tick()))
        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.last_outcome is ScheduleOutcome.FAILED
        assert loaded.last_error is not None
        assert "RuntimeError" in loaded.last_error
        assert "boom" in loaded.last_error

    async def test_jitter_applied(self) -> None:
        store = MemoryScheduleStore()
        runner = _Recorder()
        box, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        # Deterministic jitter — always pick the upper bound so we can
        # assert the call ran without depending on real-time sleep length.
        jitter_calls: list[tuple[float, float]] = []

        def jitter(lo: float, hi: float) -> float:
            jitter_calls.append((lo, hi))
            return 0.001

        reg = ScheduleRegistry(
            store, runner, now_fn=now_fn, jitter_fn=jitter
        )
        await reg.register(_hourly_schedule(jitter_seconds=15))
        box[0] = datetime(2026, 5, 4, 13, 5, tzinfo=UTC)
        await asyncio.gather(*(await reg.tick()))
        assert jitter_calls == [(0.0, 15.0)]
        assert len(runner.calls) == 1

    async def test_inflight_dedup_within_tick(self) -> None:
        # Two due schedules sharing one plan_ref → only one fires this tick.
        store = MemoryScheduleStore()
        runner = _Recorder()
        box, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(store, runner, now_fn=now_fn)
        await reg.register(
            _hourly_schedule(id="a", plan_ref="shared")
        )
        await reg.register(
            _hourly_schedule(id="b", plan_ref="shared")
        )
        box[0] = datetime(2026, 5, 4, 13, 5, tzinfo=UTC)
        await asyncio.gather(*(await reg.tick()))
        # The second due schedule had its plan_ref already claimed.
        assert len(runner.calls) == 1


# --- SLA breaches ----------------------------------------------------------


class TestTickSLA:
    async def _setup_breach(
        self,
        breach: SLABreach,
        *,
        on_breach=None,
    ) -> tuple[ScheduleRegistry, _Recorder, list[SLABreach]]:
        seen_breaches: list[SLABreach] = []

        async def finder(_p: SLAPolicy, _now: datetime) -> list[SLABreach]:
            seen_breaches.append(breach)
            return [breach]

        runner = _Recorder()
        reg = ScheduleRegistry(
            MemoryScheduleStore(),
            runner,
            sla_policy=SLAPolicy.default(),
            sla_finder=finder,
            on_breach=on_breach,
        )
        return reg, runner, seen_breaches

    async def test_breach_triggers_synthetic_run(self) -> None:
        breach = SLABreach(
            finding_id="f-1",
            severity="critical",
            source_id="src-1",
            plan_ref="plan-1",
            opened_at=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            breached_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        )
        reg, runner, _ = await self._setup_breach(breach)
        await asyncio.gather(*(await reg.tick()))
        assert len(runner.calls) == 1
        assert runner.calls[0].id == "__sla__:f-1"
        assert runner.calls[0].plan_ref == "plan-1"
        assert "sla-rescan" in runner.calls[0].tags
        assert "critical" in runner.calls[0].tags
        # Synthetic schedule's cron is the placeholder marker.
        assert runner.calls[0].cron.expr == "* * * * *"

    async def test_on_breach_callback_invoked(self) -> None:
        breach = SLABreach(
            finding_id="f-1",
            severity="high",
            source_id="src-1",
            plan_ref="plan-1",
            opened_at=datetime(2026, 5, 4, 0, 0, tzinfo=UTC),
            breached_at=datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
        )
        notified: list[SLABreach] = []

        async def on_breach(b: SLABreach) -> None:
            notified.append(b)

        reg, _, _ = await self._setup_breach(breach, on_breach=on_breach)
        await asyncio.gather(*(await reg.tick()))
        assert notified == [breach]

    async def test_breach_skipped_when_plan_ref_already_inflight(self) -> None:
        # Cron schedule and SLA breach both target the same plan_ref —
        # the cron fire claims first, so the breach call must skip.
        breach = SLABreach(
            finding_id="f-1",
            severity="critical",
            source_id="src-1",
            plan_ref="shared",
            opened_at=datetime(2026, 5, 4, 11, 0, tzinfo=UTC),
            breached_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        )

        async def finder(_p: SLAPolicy, _now: datetime) -> list[SLABreach]:
            return [breach]

        store = MemoryScheduleStore()
        runner = _Recorder()
        box, now_fn = _frozen_now(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))
        reg = ScheduleRegistry(
            store,
            runner,
            sla_policy=SLAPolicy.default(),
            sla_finder=finder,
            now_fn=now_fn,
        )
        await reg.register(_hourly_schedule(plan_ref="shared"))
        box[0] = datetime(2026, 5, 4, 13, 5, tzinfo=UTC)
        await asyncio.gather(*(await reg.tick()))
        # Only the cron fire ran; breach was deduped.
        assert len(runner.calls) == 1
        assert runner.calls[0].id == "s1"

    async def test_no_sla_finder_skips_breach_phase(self) -> None:
        # sla_policy without sla_finder — registry just doesn't call.
        store = MemoryScheduleStore()
        runner = _Recorder()
        reg = ScheduleRegistry(
            store,
            runner,
            sla_policy=SLAPolicy.default(),
            sla_finder=None,
        )
        tasks = await reg.tick()
        assert tasks == []

    async def test_no_sla_policy_skips_breach_phase(self) -> None:
        async def finder(*_: Any) -> list[SLABreach]:
            raise AssertionError("finder must not be called without policy")

        reg = ScheduleRegistry(
            MemoryScheduleStore(),
            _Recorder(),
            sla_policy=None,
            sla_finder=finder,
        )
        tasks = await reg.tick()
        assert tasks == []


# --- run_forever -----------------------------------------------------------


class TestRunForever:
    async def test_rejects_non_positive_interval(self) -> None:
        reg = ScheduleRegistry(MemoryScheduleStore(), _Recorder())
        with pytest.raises(ValueError, match="interval must be > 0"):
            await reg.run_forever(0)

    async def test_loop_exits_on_cancel_event(self) -> None:
        reg = ScheduleRegistry(MemoryScheduleStore(), _Recorder())
        cancel = asyncio.Event()

        async def trigger_cancel() -> None:
            await asyncio.sleep(0.05)
            cancel.set()

        await asyncio.gather(
            reg.run_forever(interval=0.01, cancel=cancel),
            trigger_cancel(),
        )

    async def test_loop_swallows_tick_failures(self) -> None:
        # Force store.due() to raise on the first call, then succeed.
        store = MemoryScheduleStore()
        original_due = store.due
        calls = {"n": 0}

        async def flaky_due(now: datetime) -> list[Schedule]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            return await original_due(now)

        store.due = flaky_due  # type: ignore[method-assign]
        reg = ScheduleRegistry(store, _Recorder())
        cancel = asyncio.Event()

        async def trigger_cancel() -> None:
            # Wait long enough to guarantee at least 2 ticks.
            await asyncio.sleep(0.05)
            cancel.set()

        await asyncio.gather(
            reg.run_forever(interval=0.01, cancel=cancel),
            trigger_cancel(),
        )
        assert calls["n"] >= 2  # the failing tick did not kill the loop


# --- default factories ----------------------------------------------------


class TestDefaults:
    def test_default_now_returns_utc(self) -> None:
        from pleno_pii_scanner.schedule.registry import _default_now

        t = _default_now()
        assert t.tzinfo is UTC

    def test_default_jitter_within_bounds(self) -> None:
        from pleno_pii_scanner.schedule.registry import _default_jitter

        v = _default_jitter(0.0, 1.0)
        assert 0.0 <= v <= 1.0
