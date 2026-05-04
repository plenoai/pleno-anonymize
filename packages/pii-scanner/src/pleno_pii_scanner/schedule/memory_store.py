"""In-memory ScheduleStore — primarily for tests and ephemeral installs."""

from __future__ import annotations

import asyncio
from datetime import datetime

from pleno_pii_scanner.schedule.base import Schedule, ScheduleStore


class MemoryScheduleStore(ScheduleStore):
    """Thread-safe (within asyncio) in-memory implementation.

    All mutating operations take a single `asyncio.Lock`. Reads return
    fresh copies of the stored frozen dataclasses, so callers cannot
    accidentally mutate registry state by holding a returned reference.
    """

    def __init__(self) -> None:
        self._items: dict[str, Schedule] = {}
        self._lock = asyncio.Lock()

    async def save(self, schedule: Schedule) -> None:
        async with self._lock:
            self._items[schedule.id] = schedule

    async def load(self, schedule_id: str) -> Schedule | None:
        async with self._lock:
            return self._items.get(schedule_id)

    async def list(self) -> list[Schedule]:
        async with self._lock:
            return list(self._items.values())

    async def delete(self, schedule_id: str) -> None:
        async with self._lock:
            self._items.pop(schedule_id, None)

    async def due(self, now: datetime) -> list[Schedule]:
        async with self._lock:
            return [
                s
                for s in self._items.values()
                if s.enabled
                and s.next_run_at is not None
                and s.next_run_at <= now
            ]

    async def close(self) -> None:
        return None


__all__ = ["MemoryScheduleStore"]
