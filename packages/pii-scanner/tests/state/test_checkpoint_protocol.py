"""Protocol-level tests for CheckpointStore (parametrized over implementations).

Both `MemoryCheckpointStore` and `SqliteCheckpointStore` must satisfy the
same observable contract. Adding a new store implementation only requires
extending the `store_factory` parametrization.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pleno_pii_scanner.state import (
    Checkpoint,
    CheckpointStore,
    MemoryCheckpointStore,
    ShardRecord,
    SqliteCheckpointStore,
    decode_byte_range,
    encode_byte_range,
)


StoreFactory = Callable[[], Awaitable[CheckpointStore]]


@pytest.fixture
def memory_factory() -> StoreFactory:
    async def _make() -> CheckpointStore:
        return MemoryCheckpointStore()

    return _make


@pytest.fixture
def sqlite_factory(tmp_path: Path) -> StoreFactory:
    counter = {"i": 0}

    async def _make() -> CheckpointStore:
        counter["i"] += 1
        target = tmp_path / f"store-{counter['i']}.sqlite"
        return await SqliteCheckpointStore.open(
            "scan-test", path=target
        )

    return _make


@pytest.fixture(params=["memory", "sqlite"])
def store_factory(
    request: pytest.FixtureRequest,
    memory_factory: StoreFactory,
    sqlite_factory: StoreFactory,
) -> StoreFactory:
    return memory_factory if request.param == "memory" else sqlite_factory


def _cp(
    *,
    scan_id: str = "scan-1",
    source_id: str = "src-a",
    cursor: str | None = "ck-1",
    last_doc_fingerprint: str | None = "fp-1",
    last_byte_range: tuple[int, int] | None = (0, 1023),
    updated_at: datetime | None = None,
) -> Checkpoint:
    return Checkpoint(
        scan_id=scan_id,
        source_id=source_id,
        cursor=cursor,
        last_doc_fingerprint=last_doc_fingerprint,
        last_byte_range=last_byte_range,
        updated_at=updated_at or datetime(2026, 5, 4, 12, tzinfo=UTC),
    )


class TestCheckpointDataclass:
    def test_is_frozen(self) -> None:
        cp = _cp()
        with pytest.raises((AttributeError, TypeError)):
            cp.cursor = "other"  # type: ignore[misc]

    def test_uses_slots(self) -> None:
        cp = _cp()
        assert not hasattr(cp, "__dict__")

    def test_equality_is_value_based(self) -> None:
        assert _cp() == _cp()

    def test_inequality_on_cursor(self) -> None:
        assert _cp(cursor="a") != _cp(cursor="b")


class TestShardRecord:
    def test_is_frozen(self) -> None:
        rec = ShardRecord(
            scan_id="s",
            source_id="src",
            shard_index=0,
            finding_count=10,
            written_at=datetime.now(UTC),
        )
        with pytest.raises((AttributeError, TypeError)):
            rec.shard_index = 1  # type: ignore[misc]


class TestByteRangeCodec:
    def test_round_trip(self) -> None:
        assert decode_byte_range(encode_byte_range((0, 1023))) == (0, 1023)

    def test_none_passes_through(self) -> None:
        assert encode_byte_range(None) is None
        assert decode_byte_range(None) is None

    def test_encode_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            encode_byte_range((-1, 10))

    def test_encode_rejects_inverted(self) -> None:
        with pytest.raises(ValueError):
            encode_byte_range((10, 5))

    def test_decode_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            decode_byte_range("not-a-range")

    def test_decode_rejects_non_integer(self) -> None:
        with pytest.raises(ValueError):
            decode_byte_range("a:b")

    def test_decode_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            decode_byte_range("-1:5")

    def test_decode_rejects_inverted(self) -> None:
        with pytest.raises(ValueError):
            decode_byte_range("10:5")


class TestCheckpointStoreContract:
    @pytest.mark.asyncio
    async def test_isinstance_protocol(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            assert isinstance(store, CheckpointStore)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_load_missing_returns_none(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            assert await store.load("scan-x", "src-x") is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_save_then_load_round_trip(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            cp = _cp()
            await store.save(cp)
            loaded = await store.load(cp.scan_id, cp.source_id)
            assert loaded == cp
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_save_with_null_cursor_and_byte_range(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            cp = _cp(cursor=None, last_doc_fingerprint=None, last_byte_range=None)
            await store.save(cp)
            assert await store.load(cp.scan_id, cp.source_id) == cp
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_save_overwrites_same_key(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            t0 = datetime(2026, 5, 4, 12, tzinfo=UTC)
            t1 = t0 + timedelta(seconds=30)
            await store.save(_cp(cursor="v1", updated_at=t0))
            await store.save(_cp(cursor="v2", updated_at=t1))
            loaded = await store.load("scan-1", "src-a")
            assert loaded is not None
            assert loaded.cursor == "v2"
            assert loaded.updated_at == t1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_save_many_atomic(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            batch = [
                _cp(source_id="src-a", cursor="a"),
                _cp(source_id="src-b", cursor="b"),
                _cp(source_id="src-c", cursor="c"),
            ]
            await store.save_many(batch)
            for cp in batch:
                loaded = await store.load(cp.scan_id, cp.source_id)
                assert loaded == cp
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_save_many_empty_is_noop(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.save_many([])
            assert await store.load("scan-1", "src-a") is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_save_many_dedups_same_key(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.save_many(
                [
                    _cp(source_id="src-a", cursor="first"),
                    _cp(source_id="src-a", cursor="second"),
                ]
            )
            loaded = await store.load("scan-1", "src-a")
            assert loaded is not None
            assert loaded.cursor == "second"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_list_for_scan_yields_only_matching(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.save(_cp(scan_id="scan-1", source_id="src-a"))
            await store.save(_cp(scan_id="scan-1", source_id="src-b"))
            await store.save(_cp(scan_id="scan-2", source_id="src-a"))
            collected = [cp async for cp in store.list_for_scan("scan-1")]
            assert {cp.source_id for cp in collected} == {"src-a", "src-b"}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_list_for_scan_empty(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            collected = [cp async for cp in store.list_for_scan("scan-empty")]
            assert collected == []
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_delete_removes_checkpoint(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.save(_cp())
            await store.delete("scan-1", "src-a")
            assert await store.load("scan-1", "src-a") is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.delete("scan-x", "src-x")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_record_and_list_shards(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.record_shard("scan-1", "src-a", 0, 5)
            await store.record_shard("scan-1", "src-a", 1, 7)
            shards = await store.list_shards("scan-1", "src-a")
            assert [s.shard_index for s in shards] == [0, 1]
            assert [s.finding_count for s in shards] == [5, 7]
            assert all(s.written_at.tzinfo is not None for s in shards)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_record_shard_overwrites_same_index(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.record_shard("scan-1", "src-a", 0, 5)
            await store.record_shard("scan-1", "src-a", 0, 12)
            shards = await store.list_shards("scan-1", "src-a")
            assert len(shards) == 1
            assert shards[0].finding_count == 12
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_list_shards_empty(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            assert await store.list_shards("scan-1", "src-a") == []
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_delete_purges_shards(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            await store.save(_cp())
            await store.record_shard("scan-1", "src-a", 0, 5)
            await store.delete("scan-1", "src-a")
            assert await store.list_shards("scan-1", "src-a") == []
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_concurrent_save_same_key_last_wins(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            base_ts = datetime(2026, 5, 4, 12, tzinfo=UTC)

            async def write(i: int) -> None:
                await store.save(
                    _cp(
                        cursor=f"v{i}",
                        updated_at=base_ts + timedelta(microseconds=i),
                    )
                )

            await asyncio.gather(*(write(i) for i in range(50)))
            loaded = await store.load("scan-1", "src-a")
            assert loaded is not None
            # WHY: with 50 concurrent writers any cursor v0..v49 may have
            # arrived last under cooperative scheduling. The contract is
            # only "some writer's value persists, not partial state".
            assert loaded.cursor in {f"v{i}" for i in range(50)}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_concurrent_save_distinct_keys_no_loss(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        try:
            n = 20

            async def write(i: int) -> None:
                await store.save(
                    _cp(source_id=f"src-{i:02d}", cursor=f"v{i}")
                )

            await asyncio.gather(*(write(i) for i in range(n)))
            collected: list[Checkpoint] = [
                cp async for cp in store.list_for_scan("scan-1")
            ]
            assert len(collected) == n
            assert {cp.source_id for cp in collected} == {
                f"src-{i:02d}" for i in range(n)
            }
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        await store.close()
        await store.close()

    @pytest.mark.asyncio
    async def test_use_after_close_raises(
        self, store_factory: StoreFactory
    ) -> None:
        store = await store_factory()
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.save(_cp())
