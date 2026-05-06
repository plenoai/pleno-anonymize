"""SQLite-backed ScheduleStore.

Storage layout mirrors `state.sqlite_store` (XDG_STATE_HOME under
`pleno/schedule/registry.sqlite`) so a single workstation install never
collides between scan checkpoint state and schedule registry state.

Why a dedicated file (not a table inside the scan checkpoint DB):
schedules outlive any single scan, while the checkpoint DB is named
per `scan_id` and is typically deleted after a scan completes. Sharing
files would couple their lifecycles in the wrong direction.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from pleno_pii_scanner.schedule.base import Schedule, ScheduleOutcome
from pleno_pii_scanner.schedule.cron import CronExpression


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS schedules (
        id              TEXT PRIMARY KEY,
        cron_expr       TEXT NOT NULL,
        plan_ref        TEXT NOT NULL,
        jitter_seconds  INTEGER NOT NULL,
        enabled         INTEGER NOT NULL,
        tags_json       TEXT NOT NULL,
        metadata_json   TEXT NOT NULL,
        next_run_at     TEXT,
        last_run_at     TEXT,
        last_outcome    TEXT,
        last_error      TEXT
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS schedules_due_idx
        ON schedules(enabled, next_run_at);
    """,
)


def default_registry_path() -> Path:
    """Resolve the default SQLite path for the schedule registry.

    Honors `XDG_STATE_HOME`. Singular path per user — schedules are
    per-operator state, not per-scan.
    """
    base_env = os.environ.get("XDG_STATE_HOME")
    base = Path(base_env) if base_env else Path.home() / ".local" / "state"
    return base / "pleno" / "schedule" / "registry.sqlite"


class SqliteScheduleStore:
    """aiosqlite-backed ScheduleStore. Use `await SqliteScheduleStore.open(...)`."""

    def __init__(self, path: Path, conn: aiosqlite.Connection) -> None:
        self._path = path
        self._conn = conn
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, *, path: Path | None = None) -> SqliteScheduleStore:
        target = path if path is not None else default_registry_path()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = await aiosqlite.connect(target)
        try:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            for stmt in _SCHEMA:
                await conn.execute(stmt)
            await conn.commit()
        except Exception:
            await conn.close()
            raise
        return cls(target, conn)

    @property
    def path(self) -> Path:
        return self._path

    async def __aenter__(self) -> SqliteScheduleStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def save(self, schedule: Schedule) -> None:
        async with self._lock:
            self._raise_if_closed()
            await self._conn.execute(_UPSERT, _to_row(schedule))
            await self._conn.commit()

    async def load(self, schedule_id: str) -> Schedule | None:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(_SELECT_ONE, (schedule_id,))
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
        return _from_row(row) if row is not None else None

    async def list(self) -> list[Schedule]:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(_SELECT_ALL)
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        return [_from_row(r) for r in rows]

    async def delete(self, schedule_id: str) -> None:
        async with self._lock:
            self._raise_if_closed()
            await self._conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
            await self._conn.commit()

    async def due(self, now: datetime) -> list[Schedule]:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(_SELECT_DUE, (now.isoformat(),))
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        return [_from_row(r) for r in rows]

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._conn.close()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("ScheduleStore is closed")


_UPSERT = """
INSERT INTO schedules
    (id, cron_expr, plan_ref, jitter_seconds, enabled,
     tags_json, metadata_json,
     next_run_at, last_run_at, last_outcome, last_error)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    cron_expr      = excluded.cron_expr,
    plan_ref       = excluded.plan_ref,
    jitter_seconds = excluded.jitter_seconds,
    enabled        = excluded.enabled,
    tags_json      = excluded.tags_json,
    metadata_json  = excluded.metadata_json,
    next_run_at    = excluded.next_run_at,
    last_run_at    = excluded.last_run_at,
    last_outcome   = excluded.last_outcome,
    last_error     = excluded.last_error
"""

_COLUMNS = (
    "id, cron_expr, plan_ref, jitter_seconds, enabled, "
    "tags_json, metadata_json, "
    "next_run_at, last_run_at, last_outcome, last_error"
)
_SELECT_ONE = f"SELECT {_COLUMNS} FROM schedules WHERE id=?"
_SELECT_ALL = f"SELECT {_COLUMNS} FROM schedules ORDER BY id ASC"
# enabled flag plus next_run_at <= now; nulls are not due (registry sets
# next_run_at on register so a NULL implies a stale row from before a
# schema migration — skip rather than crash).
_SELECT_DUE = (
    f"SELECT {_COLUMNS} FROM schedules "
    "WHERE enabled=1 AND next_run_at IS NOT NULL AND next_run_at<=? "
    "ORDER BY next_run_at ASC"
)


def _to_row(s: Schedule) -> tuple[Any, ...]:
    return (
        s.id,
        s.cron.expr,
        s.plan_ref,
        s.jitter_seconds,
        1 if s.enabled else 0,
        json.dumps(list(s.tags)),
        json.dumps([list(kv) for kv in s.metadata]),
        s.next_run_at.isoformat() if s.next_run_at else None,
        s.last_run_at.isoformat() if s.last_run_at else None,
        s.last_outcome.value if s.last_outcome else None,
        s.last_error,
    )


def _from_row(row: tuple[Any, ...]) -> Schedule:
    tags = tuple(json.loads(row[5]))
    metadata = tuple(tuple(kv) for kv in json.loads(row[6]))
    return Schedule(
        id=row[0],
        cron=CronExpression.parse(row[1]),
        plan_ref=row[2],
        jitter_seconds=int(row[3]),
        enabled=bool(row[4]),
        tags=tags,
        metadata=metadata,
        next_run_at=_parse_iso(row[7]),
        last_run_at=_parse_iso(row[8]),
        last_outcome=ScheduleOutcome(row[9]) if row[9] else None,
        last_error=row[10],
    )


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


__all__ = ["SqliteScheduleStore", "default_registry_path"]
