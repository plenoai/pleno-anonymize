"""Tests for MemoryScheduleStore — the contract reused by SqliteScheduleStore."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pleno_pii_scanner.schedule.base import Schedule
from pleno_pii_scanner.schedule.cron import CronExpression
from pleno_pii_scanner.schedule.memory_store import MemoryScheduleStore


def _sched(
    id: str = "s1",
    next_run_at: datetime | None = None,
    enabled: bool = True,
) -> Schedule:
    return Schedule(
        id=id,
        cron=CronExpression.parse("@hourly"),
        plan_ref=f"plan-{id}",
        next_run_at=next_run_at,
        enabled=enabled,
    )


class TestMemoryScheduleStore:
    async def test_save_and_load(self) -> None:
        store = MemoryScheduleStore()
        s = _sched()
        await store.save(s)
        loaded = await store.load("s1")
        assert loaded == s

    async def test_load_missing_returns_none(self) -> None:
        store = MemoryScheduleStore()
        assert await store.load("missing") is None

    async def test_save_upserts(self) -> None:
        store = MemoryScheduleStore()
        await store.save(_sched(next_run_at=datetime(2026, 1, 1, tzinfo=UTC)))
        await store.save(_sched(next_run_at=datetime(2026, 2, 1, tzinfo=UTC)))
        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.next_run_at == datetime(2026, 2, 1, tzinfo=UTC)

    async def test_list_returns_all(self) -> None:
        store = MemoryScheduleStore()
        await store.save(_sched(id="a"))
        await store.save(_sched(id="b"))
        items = await store.list()
        assert {s.id for s in items} == {"a", "b"}

    async def test_delete_removes(self) -> None:
        store = MemoryScheduleStore()
        await store.save(_sched())
        await store.delete("s1")
        assert await store.load("s1") is None

    async def test_delete_missing_no_op(self) -> None:
        store = MemoryScheduleStore()
        await store.delete("missing")  # no exception

    async def test_due_filters_by_next_run_at(self) -> None:
        store = MemoryScheduleStore()
        past = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        future = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
        await store.save(_sched(id="due", next_run_at=past))
        await store.save(_sched(id="not-due", next_run_at=future))
        result = await store.due(datetime(2026, 5, 4, 13, 0, tzinfo=UTC))
        assert [s.id for s in result] == ["due"]

    async def test_due_skips_disabled(self) -> None:
        store = MemoryScheduleStore()
        past = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        await store.save(_sched(id="disabled", next_run_at=past, enabled=False))
        result = await store.due(datetime(2026, 5, 4, 13, 0, tzinfo=UTC))
        assert result == []

    async def test_due_skips_unscheduled(self) -> None:
        store = MemoryScheduleStore()
        await store.save(_sched(id="never", next_run_at=None))
        result = await store.due(datetime(2026, 5, 4, 13, 0, tzinfo=UTC))
        assert result == []

    async def test_close_is_noop(self) -> None:
        store = MemoryScheduleStore()
        await store.close()  # no-op
