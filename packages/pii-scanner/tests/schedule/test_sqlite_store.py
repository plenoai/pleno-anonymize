"""Tests for SqliteScheduleStore — round-trip + persistence semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pleno_pii_scanner.schedule.base import Schedule, ScheduleOutcome
from pleno_pii_scanner.schedule.cron import CronExpression
from pleno_pii_scanner.schedule.sqlite_store import (
    SqliteScheduleStore,
    default_registry_path,
)


def _sched(
    id: str = "s1",
    next_run_at: datetime | None = datetime(2026, 5, 4, 13, 0, tzinfo=UTC),
    last_outcome: ScheduleOutcome | None = None,
    last_error: str | None = None,
    enabled: bool = True,
) -> Schedule:
    return Schedule(
        id=id,
        cron=CronExpression.parse("@hourly"),
        plan_ref=f"plan-{id}",
        jitter_seconds=15,
        enabled=enabled,
        tags=("repo:foo", "team:sec"),
        metadata=(("owner", "alice"), ("env", "prod")),
        next_run_at=next_run_at,
        last_run_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC) if last_outcome else None,
        last_outcome=last_outcome,
        last_error=last_error,
    )


@pytest.fixture
async def store(tmp_path: Path):
    db = tmp_path / "registry.sqlite"
    s = await SqliteScheduleStore.open(path=db)
    try:
        yield s
    finally:
        await s.close()


class TestSqliteScheduleStore:
    async def test_round_trip_full_schedule(self, store: SqliteScheduleStore) -> None:
        s = _sched(last_outcome=ScheduleOutcome.SUCCESS)
        await store.save(s)
        loaded = await store.load("s1")
        assert loaded == s

    async def test_load_missing_returns_none(
        self, store: SqliteScheduleStore
    ) -> None:
        assert await store.load("missing") is None

    async def test_save_upserts(self, store: SqliteScheduleStore) -> None:
        await store.save(_sched(next_run_at=datetime(2026, 1, 1, tzinfo=UTC)))
        await store.save(_sched(next_run_at=datetime(2026, 2, 1, tzinfo=UTC)))
        loaded = await store.load("s1")
        assert loaded is not None
        assert loaded.next_run_at == datetime(2026, 2, 1, tzinfo=UTC)

    async def test_list_orders_by_id(self, store: SqliteScheduleStore) -> None:
        await store.save(_sched(id="b"))
        await store.save(_sched(id="a"))
        result = await store.list()
        assert [s.id for s in result] == ["a", "b"]

    async def test_delete_removes(self, store: SqliteScheduleStore) -> None:
        await store.save(_sched())
        await store.delete("s1")
        assert await store.load("s1") is None

    async def test_delete_missing_no_op(self, store: SqliteScheduleStore) -> None:
        await store.delete("missing")  # no exception

    async def test_due_filters_enabled_and_overdue(
        self, store: SqliteScheduleStore
    ) -> None:
        past = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        future = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)
        await store.save(_sched(id="due", next_run_at=past))
        await store.save(_sched(id="not-due", next_run_at=future))
        await store.save(_sched(id="disabled", next_run_at=past, enabled=False))
        await store.save(_sched(id="never", next_run_at=None))
        result = await store.due(datetime(2026, 5, 4, 13, 0, tzinfo=UTC))
        assert [s.id for s in result] == ["due"]

    async def test_close_idempotent(self, store: SqliteScheduleStore) -> None:
        await store.close()
        await store.close()  # second call must be a no-op

    async def test_operations_after_close_raise(
        self, store: SqliteScheduleStore
    ) -> None:
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.save(_sched())

    async def test_list_after_close_raises(
        self, store: SqliteScheduleStore
    ) -> None:
        await store.save(_sched())
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.list()

    async def test_delete_after_close_raises(
        self, store: SqliteScheduleStore
    ) -> None:
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.delete("x")

    async def test_load_after_close_raises(
        self, store: SqliteScheduleStore
    ) -> None:
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.load("x")

    async def test_due_after_close_raises(
        self, store: SqliteScheduleStore
    ) -> None:
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.due(datetime(2026, 5, 4, tzinfo=UTC))

    async def test_path_property(self, store: SqliteScheduleStore) -> None:
        assert store.path.exists()

    async def test_aenter_aexit(self, tmp_path: Path) -> None:
        db = tmp_path / "ctx.sqlite"
        async with await SqliteScheduleStore.open(path=db) as s:
            await s.save(_sched())
        # File closed but data persists; reopen.
        s2 = await SqliteScheduleStore.open(path=db)
        try:
            assert await s2.load("s1") is not None
        finally:
            await s2.close()

    async def test_naive_datetime_in_db_is_normalised_to_utc(
        self, store: SqliteScheduleStore, tmp_path: Path
    ) -> None:
        # Defensive: a naive datetime restored from a backup or written by
        # an older version must come back UTC-aware so callers never
        # accidentally compare aware vs naive datetimes downstream.
        from pleno_pii_scanner.schedule.sqlite_store import _parse_iso

        result = _parse_iso("2026-05-04T12:00:00")
        assert result is not None
        assert result.tzinfo is UTC

    async def test_open_failure_closes_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If schema creation crashes mid-flight the connection must close.
        from pleno_pii_scanner.schedule import sqlite_store as mod

        original = mod._SCHEMA
        monkeypatch.setattr(mod, "_SCHEMA", original + ("BAD SQL;",))
        with pytest.raises(Exception):
            await SqliteScheduleStore.open(path=tmp_path / "boom.sqlite")


class TestDefaultRegistryPath:
    def test_uses_xdg_state_home_when_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert (
            default_registry_path()
            == tmp_path / "pleno" / "schedule" / "registry.sqlite"
        )

    def test_falls_back_to_local_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert (
            default_registry_path()
            == tmp_path / ".local" / "state" / "pleno" / "schedule" / "registry.sqlite"
        )

    async def test_open_without_path_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        s = await SqliteScheduleStore.open()
        try:
            assert s.path == tmp_path / "pleno" / "schedule" / "registry.sqlite"
        finally:
            await s.close()
