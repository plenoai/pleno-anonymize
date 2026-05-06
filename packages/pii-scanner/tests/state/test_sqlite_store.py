"""SqliteCheckpointStore-specific tests (durability, XDG path, error paths)."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from pleno_pii_scanner.state import (
    Checkpoint,
    SqliteCheckpointStore,
    default_state_path,
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


class TestDefaultStatePath:
    def test_uses_xdg_state_home_when_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        path = default_state_path("scan-xyz")
        assert path == tmp_path / "pleno" / "scan-xyz" / "checkpoint.sqlite"

    def test_falls_back_to_local_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        path = default_state_path("scan-xyz")
        assert (
            path
            == tmp_path
            / ".local"
            / "state"
            / "pleno"
            / "scan-xyz"
            / "checkpoint.sqlite"
        )

    def test_empty_xdg_state_home_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # WHY: $XDG_STATE_HOME='' should be treated as unset per XDG spec.
        monkeypatch.setenv("XDG_STATE_HOME", "")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        path = default_state_path("scan-xyz")
        assert (
            path
            == tmp_path
            / ".local"
            / "state"
            / "pleno"
            / "scan-xyz"
            / "checkpoint.sqlite"
        )


class TestSqliteOpen:
    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-x", path=nested)
        try:
            assert nested.exists()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_opens_with_xdg_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        store = await SqliteCheckpointStore.open("scan-default")
        try:
            assert (
                store.path == tmp_path / "pleno" / "scan-default" / "checkpoint.sqlite"
            )
            assert store.path.exists()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_open_failure_closes_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # WHY: cover the except branch in open() that releases the
        # connection if initialization (PRAGMA / CREATE TABLE) fails.
        target = tmp_path / "ck.sqlite"

        original_connect = aiosqlite.connect

        class FailingConn:
            def __init__(self, real: aiosqlite.Connection) -> None:
                self._real = real
                self._calls = 0

            async def execute(self, *args: object, **kw: object) -> object:
                self._calls += 1
                if self._calls > 1:
                    raise RuntimeError("simulated migration failure")
                return await self._real.execute(*args, **kw)

            async def commit(self) -> None:
                await self._real.commit()

            async def close(self) -> None:
                await self._real.close()

        async def patched_connect(*args: object, **kw: object) -> object:
            real = await original_connect(*args, **kw)
            return FailingConn(real)

        monkeypatch.setattr(aiosqlite, "connect", patched_connect)
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            await SqliteCheckpointStore.open("scan-x", path=target)

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        target = tmp_path / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-x", path=target)
        try:
            cur = await store._conn.execute("PRAGMA journal_mode;")
            row = await cur.fetchone()
            await cur.close()
            assert row is not None
            assert row[0].lower() == "wal"
        finally:
            await store.close()


class TestSqliteDurability:
    @pytest.mark.asyncio
    async def test_kill_minus_9_resume(self, tmp_path: Path) -> None:
        # WHY: simulate SIGKILL between save() and the next operation by
        # closing the process-local handle and reopening from disk. A
        # store that does not fsync on commit would lose the row here.
        target = tmp_path / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-1", path=target)
        cp = _cp(cursor="checkpoint-after-batch-7")
        await store.save(cp)
        await store.close()

        reopened = await SqliteCheckpointStore.open("scan-1", path=target)
        try:
            loaded = await reopened.load(cp.scan_id, cp.source_id)
            assert loaded == cp
        finally:
            await reopened.close()

    @pytest.mark.asyncio
    async def test_save_many_atomic_after_reopen(self, tmp_path: Path) -> None:
        target = tmp_path / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-1", path=target)
        batch = [_cp(source_id=f"src-{i}", cursor=f"v{i}") for i in range(5)]
        await store.save_many(batch)
        await store.close()

        reopened = await SqliteCheckpointStore.open("scan-1", path=target)
        try:
            collected = [cp async for cp in reopened.list_for_scan("scan-1")]
            assert len(collected) == 5
            assert {cp.cursor for cp in collected} == {f"v{i}" for i in range(5)}
        finally:
            await reopened.close()

    @pytest.mark.asyncio
    async def test_shard_records_persist(self, tmp_path: Path) -> None:
        target = tmp_path / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-1", path=target)
        await store.record_shard("scan-1", "src-a", 0, 11)
        await store.record_shard("scan-1", "src-a", 1, 22)
        await store.close()

        reopened = await SqliteCheckpointStore.open("scan-1", path=target)
        try:
            shards = await reopened.list_shards("scan-1", "src-a")
            assert [(s.shard_index, s.finding_count) for s in shards] == [
                (0, 11),
                (1, 22),
            ]
        finally:
            await reopened.close()


class TestSqliteEdgeCases:
    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        store = await SqliteCheckpointStore.open("scan-1", path=tmp_path / "ck.sqlite")
        await store.close()
        await store.close()

    @pytest.mark.asyncio
    async def test_async_context_manager(self, tmp_path: Path) -> None:
        target = tmp_path / "ck.sqlite"
        async with await SqliteCheckpointStore.open("scan-1", path=target) as store:
            await store.save(_cp())
            assert await store.load("scan-1", "src-a") is not None
        # WHY: file remains on disk even after close, so a fresh open
        # can still read prior state.
        async with await SqliteCheckpointStore.open("scan-1", path=target) as store2:
            assert await store2.load("scan-1", "src-a") is not None

    @pytest.mark.asyncio
    async def test_save_many_empty_short_circuits(self, tmp_path: Path) -> None:
        store = await SqliteCheckpointStore.open("scan-1", path=tmp_path / "ck.sqlite")
        try:
            await store.save_many([])
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_naive_datetime_in_db_is_assumed_utc(self, tmp_path: Path) -> None:
        # WHY: covers the naive-datetime branch of _parse_iso. We can't
        # easily get a naive timestamp through save() (Checkpoint takes a
        # datetime; we always serialize via .isoformat()), so we manually
        # poke the row to simulate a legacy file written by a future
        # migration that forgot the tz suffix.
        target = tmp_path / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-1", path=target)
        try:
            await store._conn.execute(
                "INSERT INTO scan_state(scan_id, source_id, cursor, "
                "last_doc_ref, last_byte_range, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("scan-1", "src-a", None, None, None, "2026-05-04T12:00:00"),
            )
            await store._conn.commit()
            loaded = await store.load("scan-1", "src-a")
            assert loaded is not None
            assert loaded.updated_at.tzinfo is not None
            assert loaded.updated_at == datetime(2026, 5, 4, 12, tzinfo=UTC)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_concurrent_distinct_keys_do_not_deadlock(
        self, tmp_path: Path
    ) -> None:
        store = await SqliteCheckpointStore.open("scan-1", path=tmp_path / "ck.sqlite")
        try:

            async def write_and_read(i: int) -> None:
                cp = _cp(source_id=f"src-{i:02d}", cursor=f"v{i}")
                await store.save(cp)
                got = await store.load(cp.scan_id, cp.source_id)
                assert got is not None and got.cursor == f"v{i}"

            await asyncio.wait_for(
                asyncio.gather(*(write_and_read(i) for i in range(30))),
                timeout=10.0,
            )
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_use_after_close_for_each_method(self, tmp_path: Path) -> None:
        store = await SqliteCheckpointStore.open("scan-1", path=tmp_path / "ck.sqlite")
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.save(_cp())
        with pytest.raises(RuntimeError, match="closed"):
            await store.save_many([_cp()])
        with pytest.raises(RuntimeError, match="closed"):
            await store.load("scan-1", "src-a")
        with pytest.raises(RuntimeError, match="closed"):
            async for _ in store.list_for_scan("scan-1"):
                pass
        with pytest.raises(RuntimeError, match="closed"):
            await store.delete("scan-1", "src-a")
        with pytest.raises(RuntimeError, match="closed"):
            await store.record_shard("scan-1", "src-a", 0, 1)
        with pytest.raises(RuntimeError, match="closed"):
            await store.list_shards("scan-1", "src-a")

    @pytest.mark.asyncio
    async def test_path_property_matches_constructor(self, tmp_path: Path) -> None:
        target = tmp_path / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-1", path=target)
        try:
            assert store.path == target
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_parent_directory_permission_700(self, tmp_path: Path) -> None:
        # WHY: cursor strings can leak access patterns (Slack channel IDs,
        # Confluence space keys). On a multi-user host, the parent dir
        # must not be world-readable.
        if os.name == "nt":
            pytest.skip("POSIX-only permission semantics")
        target = tmp_path / "scoped" / "ck.sqlite"
        store = await SqliteCheckpointStore.open("scan-1", path=target)
        try:
            mode = (target.parent.stat().st_mode) & 0o777
            assert mode == 0o700
        finally:
            await store.close()
