"""ScanCache tests — Memory + SQLite parity, fingerprint/schema gating, XDG path.

Mirrors `test_memory_store.py` / `test_sqlite_store.py` so the same
correctness bar applies to both implementations of the new cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pleno_pii_scanner.state import (
    CacheLookup,
    MemoryScanCache,
    ScanCache,
    SqliteScanCache,
    default_cache_path,
)


@pytest.fixture
async def memory() -> ScanCache:
    return MemoryScanCache()


@pytest.fixture
async def sqlite(tmp_path: Path) -> ScanCache:
    return await SqliteScanCache.open(path=tmp_path / "cache.sqlite")


@pytest.fixture(params=["memory", "sqlite"])
async def cache(request: pytest.FixtureRequest, tmp_path: Path) -> ScanCache:
    if request.param == "memory":
        return MemoryScanCache()
    return await SqliteScanCache.open(path=tmp_path / "cache.sqlite")


class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_put_then_get_returns_value(self, cache: ScanCache) -> None:
        try:
            await cache.put(
                "k", fingerprint="fp", schema_version="sv", value=b"payload"
            )
            assert (
                await cache.get("k", fingerprint="fp", schema_version="sv")
            ) == b"payload"
        finally:
            await cache.close()

    @pytest.mark.asyncio
    async def test_get_missing_key_returns_none(self, cache: ScanCache) -> None:
        try:
            assert (await cache.get("k", fingerprint="fp", schema_version="sv")) is None
        finally:
            await cache.close()

    @pytest.mark.asyncio
    async def test_put_replaces_existing_entry(self, cache: ScanCache) -> None:
        try:
            await cache.put("k", fingerprint="fp", schema_version="sv", value=b"a")
            await cache.put("k", fingerprint="fp", schema_version="sv", value=b"b")
            assert (await cache.get("k", fingerprint="fp", schema_version="sv")) == b"b"
        finally:
            await cache.close()


class TestFingerprintGating:
    @pytest.mark.asyncio
    async def test_stale_fingerprint_returns_none(self, cache: ScanCache) -> None:
        try:
            await cache.put("k", fingerprint="old", schema_version="sv", value=b"v")
            assert (
                await cache.get("k", fingerprint="new", schema_version="sv")
            ) is None
        finally:
            await cache.close()

    @pytest.mark.asyncio
    async def test_stale_schema_returns_none(self, cache: ScanCache) -> None:
        try:
            await cache.put("k", fingerprint="fp", schema_version="v1", value=b"v")
            assert (await cache.get("k", fingerprint="fp", schema_version="v2")) is None
        finally:
            await cache.close()


class TestGetMany:
    @pytest.mark.asyncio
    async def test_returns_only_matching_entries(self, cache: ScanCache) -> None:
        try:
            await cache.put("a", fingerprint="f1", schema_version="s", value=b"A")
            await cache.put("b", fingerprint="f2", schema_version="s", value=b"B")
            await cache.put("c", fingerprint="f3", schema_version="s", value=b"C")
            result = await cache.get_many(
                [
                    CacheLookup(key="a", fingerprint="f1", schema_version="s"),
                    CacheLookup(key="b", fingerprint="STALE", schema_version="s"),
                    CacheLookup(key="c", fingerprint="f3", schema_version="OLD"),
                    CacheLookup(key="missing", fingerprint="x", schema_version="s"),
                ]
            )
            assert result == {"a": b"A"}
        finally:
            await cache.close()

    @pytest.mark.asyncio
    async def test_empty_lookups_returns_empty(self, cache: ScanCache) -> None:
        try:
            assert await cache.get_many([]) == {}
        finally:
            await cache.close()

    @pytest.mark.asyncio
    async def test_large_batch(self, cache: ScanCache) -> None:
        try:
            for i in range(50):
                await cache.put(
                    f"k{i}", fingerprint="f", schema_version="s", value=str(i).encode()
                )
            result = await cache.get_many(
                [
                    CacheLookup(key=f"k{i}", fingerprint="f", schema_version="s")
                    for i in range(50)
                ]
            )
            assert len(result) == 50
            assert result["k0"] == b"0"
            assert result["k49"] == b"49"
        finally:
            await cache.close()


class TestPurgeOtherSchemas:
    @pytest.mark.asyncio
    async def test_keeps_current_drops_other(self, cache: ScanCache) -> None:
        try:
            await cache.put("a", fingerprint="x", schema_version="cur", value=b"1")
            await cache.put("b", fingerprint="y", schema_version="old", value=b"2")
            removed = await cache.purge_other_schemas("cur")
            assert removed == 1
            assert (await cache.get("a", fingerprint="x", schema_version="cur")) == b"1"
            assert (await cache.get("b", fingerprint="y", schema_version="old")) is None
        finally:
            await cache.close()


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_entry(self, cache: ScanCache) -> None:
        try:
            await cache.put("k", fingerprint="f", schema_version="s", value=b"v")
            await cache.delete("k")
            assert (await cache.get("k", fingerprint="f", schema_version="s")) is None
        finally:
            await cache.close()

    @pytest.mark.asyncio
    async def test_delete_missing_is_noop(self, cache: ScanCache) -> None:
        try:
            await cache.delete("nope")
        finally:
            await cache.close()


class TestIterEntries:
    @pytest.mark.asyncio
    async def test_iter_yields_all_stored(self, cache: ScanCache) -> None:
        try:
            await cache.put("a", fingerprint="x", schema_version="s", value=b"1")
            await cache.put("b", fingerprint="y", schema_version="s", value=b"2")
            keys = sorted([e.key async for e in cache.iter_entries()])
            assert keys == ["a", "b"]
        finally:
            await cache.close()


class TestClosedSemantics:
    @pytest.mark.asyncio
    async def test_use_after_close_raises(self, cache: ScanCache) -> None:
        await cache.close()
        with pytest.raises(RuntimeError):
            await cache.put("k", fingerprint="f", schema_version="s", value=b"v")

    @pytest.mark.asyncio
    async def test_double_close_is_noop(self, cache: ScanCache) -> None:
        await cache.close()
        await cache.close()


class TestDefaultCachePath:
    def test_uses_xdg_state_home_when_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert default_cache_path() == (
            tmp_path / "pleno" / "cache" / "scan_cache.sqlite"
        )

    def test_falls_back_to_local_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_cache_path() == (
            tmp_path / ".local" / "state" / "pleno" / "cache" / "scan_cache.sqlite"
        )

    def test_empty_xdg_state_home_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", "")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_cache_path() == (
            tmp_path / ".local" / "state" / "pleno" / "cache" / "scan_cache.sqlite"
        )


class TestSqlitePersistence:
    @pytest.mark.asyncio
    async def test_survives_close_and_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "c.sqlite"
        first = await SqliteScanCache.open(path=path)
        await first.put("k", fingerprint="f", schema_version="s", value=b"v")
        await first.close()
        second = await SqliteScanCache.open(path=path)
        try:
            assert (await second.get("k", fingerprint="f", schema_version="s")) == b"v"
        finally:
            await second.close()

    @pytest.mark.asyncio
    async def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c.sqlite"
        cache = await SqliteScanCache.open(path=nested)
        try:
            assert nested.exists()
        finally:
            await cache.close()
