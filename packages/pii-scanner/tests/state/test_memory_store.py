"""MemoryCheckpointStore-specific tests (non-shared semantics)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pleno_pii_scanner.state import (
    Checkpoint,
    MemoryCheckpointStore,
)


def _cp(**overrides: object) -> Checkpoint:
    base: dict[str, object] = dict(
        scan_id="scan-1",
        source_id="src-a",
        cursor="ck-1",
        last_doc_fingerprint="fp-1",
        last_byte_range=(0, 1023),
        updated_at=datetime(2026, 5, 4, 12, tzinfo=UTC),
    )
    base.update(overrides)
    return Checkpoint(**base)  # type: ignore[arg-type]


class TestMemoryCheckpointStore:
    @pytest.mark.asyncio
    async def test_async_context_manager_closes(self) -> None:
        async with MemoryCheckpointStore() as store:
            await store.save(_cp())
            assert await store.load("scan-1", "src-a") is not None
        # WHY: __aexit__ should have closed the store; further use raises.
        with pytest.raises(RuntimeError, match="closed"):
            await store.save(_cp())

    @pytest.mark.asyncio
    async def test_close_clears_state(self) -> None:
        store = MemoryCheckpointStore()
        await store.save(_cp())
        await store.record_shard("scan-1", "src-a", 0, 1)
        await store.close()
        assert store._checkpoints == {}
        assert store._shards == {}

    @pytest.mark.asyncio
    async def test_load_after_close_raises(self) -> None:
        store = MemoryCheckpointStore()
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.load("scan-1", "src-a")

    @pytest.mark.asyncio
    async def test_list_for_scan_after_close_raises(self) -> None:
        store = MemoryCheckpointStore()
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            async for _ in store.list_for_scan("scan-1"):
                pass

    @pytest.mark.asyncio
    async def test_record_shard_after_close_raises(self) -> None:
        store = MemoryCheckpointStore()
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.record_shard("scan-1", "src-a", 0, 1)

    @pytest.mark.asyncio
    async def test_list_shards_after_close_raises(self) -> None:
        store = MemoryCheckpointStore()
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.list_shards("scan-1", "src-a")

    @pytest.mark.asyncio
    async def test_delete_after_close_raises(self) -> None:
        store = MemoryCheckpointStore()
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.delete("scan-1", "src-a")

    @pytest.mark.asyncio
    async def test_save_many_after_close_raises(self) -> None:
        store = MemoryCheckpointStore()
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.save_many([_cp()])
