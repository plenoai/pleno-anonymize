"""In-memory CheckpointStore for tests and CI.

Pure dict storage. Mirrors `SqliteCheckpointStore` semantics so the same
parametrized test suite verifies both implementations.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from .checkpoint import Checkpoint, ShardRecord


class MemoryCheckpointStore:
    """Process-local checkpoint store; state is lost on `close()`."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._checkpoints: dict[tuple[str, str], Checkpoint] = {}
        self._shards: dict[tuple[str, str], dict[int, ShardRecord]] = {}
        self._closed = False

    async def __aenter__(self) -> MemoryCheckpointStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def save(self, cp: Checkpoint) -> None:
        async with self._lock:
            self._raise_if_closed()
            self._checkpoints[(cp.scan_id, cp.source_id)] = cp

    async def save_many(self, cps: list[Checkpoint]) -> None:
        # WHY: stage to a local dict first so a duplicate key inside the
        # batch resolves to the last entry, matching SQLite's `ON CONFLICT
        # DO UPDATE` last-wins behavior on a single statement set.
        staged: dict[tuple[str, str], Checkpoint] = {}
        for cp in cps:
            staged[(cp.scan_id, cp.source_id)] = cp
        async with self._lock:
            self._raise_if_closed()
            self._checkpoints.update(staged)

    async def load(
        self, scan_id: str, source_id: str
    ) -> Checkpoint | None:
        async with self._lock:
            self._raise_if_closed()
            return self._checkpoints.get((scan_id, source_id))

    async def list_for_scan(
        self, scan_id: str
    ) -> AsyncIterator[Checkpoint]:
        async with self._lock:
            self._raise_if_closed()
            # WHY: snapshot under the lock so the caller can iterate
            # without racing concurrent saves.
            snapshot = [
                cp
                for (sid, _), cp in self._checkpoints.items()
                if sid == scan_id
            ]
        for cp in snapshot:
            yield cp

    async def delete(self, scan_id: str, source_id: str) -> None:
        async with self._lock:
            self._raise_if_closed()
            self._checkpoints.pop((scan_id, source_id), None)
            self._shards.pop((scan_id, source_id), None)

    async def record_shard(
        self,
        scan_id: str,
        source_id: str,
        shard_index: int,
        finding_count: int,
    ) -> None:
        from datetime import UTC, datetime

        async with self._lock:
            self._raise_if_closed()
            bucket = self._shards.setdefault((scan_id, source_id), {})
            bucket[shard_index] = ShardRecord(
                scan_id=scan_id,
                source_id=source_id,
                shard_index=shard_index,
                finding_count=finding_count,
                written_at=datetime.now(UTC),
            )

    async def list_shards(
        self, scan_id: str, source_id: str
    ) -> list[ShardRecord]:
        async with self._lock:
            self._raise_if_closed()
            bucket = self._shards.get((scan_id, source_id), {})
            return sorted(bucket.values(), key=lambda r: r.shard_index)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._checkpoints.clear()
            self._shards.clear()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("CheckpointStore is closed")


__all__ = ["MemoryCheckpointStore"]
