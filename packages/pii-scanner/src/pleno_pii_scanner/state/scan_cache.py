"""ScanCache — content-fingerprinted result cache for incremental scans.

The CheckpointStore (#6) lets a single scan resume after a kill -9. The
ScanCache complements it by letting *separate* scan invocations skip work
that produced the same output last time. The cache survives across
`scan_id`s on purpose: an org-scan that reruns nightly should not re-walk
the 95% of repos that did not move since yesterday.

The store is intentionally content-agnostic (`value: bytes`). Callers
decide the wire format — typically a JSON-serialized findings list, or a
small marker that says "this sub-source emitted zero findings". Two
fingerprints gate a hit:

  * `fingerprint` — opaque snapshot key for the scanned content
    (commit SHA for a repo, ETag for a doc, version vector for a row).
  * `schema_version` — fingerprint of the detector pipeline so a regex
    pack update or NER model bump auto-invalidates every cached entry
    without requiring the operator to wipe state.

Both must match exactly for `get()` to return the stored value.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiosqlite


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One cached scan result.

    `value` is opaque bytes — the cache layer never parses it. `stored_at`
    is for diagnostics and TTL eviction (which the operator runs out of
    band; the store itself does not garbage-collect on access).
    """

    key: str
    fingerprint: str
    schema_version: str
    value: bytes
    stored_at: datetime


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """Single batched-lookup request: same `(fingerprint, schema_version)`
    semantics as `ScanCache.get`, just bundled so a caller can hand a
    list to `get_many` instead of awaiting `get` once per key.
    """

    key: str
    fingerprint: str
    schema_version: str


@runtime_checkable
class ScanCache(Protocol):
    """Persistence contract for content-fingerprinted scan results.

    Implementations must be safe to call concurrently from multiple
    asyncio tasks. Last-writer-wins on the `key` column.
    """

    async def get(
        self,
        key: str,
        *,
        fingerprint: str,
        schema_version: str,
    ) -> bytes | None:
        """Return the cached value iff `(key, fingerprint, schema_version)`
        all match a stored entry. Otherwise `None` — including the case
        where a row exists for `key` but with a stale `fingerprint` or
        `schema_version`. The caller never has to defend against stale
        hits.
        """
        ...

    async def get_many(self, lookups: Sequence[CacheLookup]) -> dict[str, bytes]:
        """Resolve every lookup against the store in a single round-trip.

        Returns a dict mapping `lookup.key` → `value` for every entry
        whose stored fingerprint AND schema_version match the request.
        Misses (absent key, stale fingerprint, stale schema) are simply
        absent from the result. The on-disk SQLite path collapses to
        one SELECT regardless of `len(lookups)`, which dominates the
        sub-source pre-pass at org-scan scale.
        """
        ...

    async def put(
        self,
        key: str,
        *,
        fingerprint: str,
        schema_version: str,
        value: bytes,
    ) -> None:
        """Upsert a cache entry. Replaces any prior row for `key`."""
        ...

    async def delete(self, key: str) -> None:
        """Remove a single cache entry. No-op if absent."""
        ...

    async def purge_other_schemas(self, schema_version: str) -> int:
        """Drop every entry whose `schema_version` differs from the given
        one. Returns the number of rows removed. Called periodically by
        the runner so the cache file does not grow without bound when the
        detector pipeline is upgraded.
        """
        ...

    def iter_entries(self) -> AsyncIterator[CacheEntry]:
        """Yield every entry. Used by tests and `pleno-pii-scanner cache
        ls` style diagnostics. Implementations may snapshot under their
        internal lock and yield outside it.
        """
        ...

    async def close(self) -> None:
        """Release the underlying connection / file handle."""
        ...


def default_cache_path() -> Path:
    """Resolve the default SQLite path for the shared scan cache.

    Unlike CheckpointStore (`~/.local/state/pleno/<scan_id>/...`), the
    scan cache is **shared across scan_ids** — the whole point of the
    cache is to amortize work across separate invocations. So we put it
    at `~/.local/state/pleno/cache/scan_cache.sqlite` (XDG state, no
    per-scan subdir).
    """
    base_env = os.environ.get("XDG_STATE_HOME")
    if base_env:
        base = Path(base_env)
    else:
        base = Path.home() / ".local" / "state"
    return base / "pleno" / "cache" / "scan_cache.sqlite"


class MemoryScanCache:
    """In-process ScanCache. State is dropped on `close()`. Used by tests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[str, CacheEntry] = {}
        self._closed = False

    async def __aenter__(self) -> MemoryScanCache:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def get(
        self,
        key: str,
        *,
        fingerprint: str,
        schema_version: str,
    ) -> bytes | None:
        async with self._lock:
            self._raise_if_closed()
            entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.fingerprint != fingerprint:
            return None
        if entry.schema_version != schema_version:
            return None
        return entry.value

    async def get_many(self, lookups: Sequence[CacheLookup]) -> dict[str, bytes]:
        if not lookups:
            return {}
        async with self._lock:
            self._raise_if_closed()
            snapshot = {lk.key: self._entries.get(lk.key) for lk in lookups}
        out: dict[str, bytes] = {}
        for lk in lookups:
            entry = snapshot.get(lk.key)
            if entry is None:
                continue
            if entry.fingerprint != lk.fingerprint:
                continue
            if entry.schema_version != lk.schema_version:
                continue
            out[lk.key] = entry.value
        return out

    async def put(
        self,
        key: str,
        *,
        fingerprint: str,
        schema_version: str,
        value: bytes,
    ) -> None:
        async with self._lock:
            self._raise_if_closed()
            self._entries[key] = CacheEntry(
                key=key,
                fingerprint=fingerprint,
                schema_version=schema_version,
                value=value,
                stored_at=datetime.now(UTC),
            )

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._raise_if_closed()
            self._entries.pop(key, None)

    async def purge_other_schemas(self, schema_version: str) -> int:
        async with self._lock:
            self._raise_if_closed()
            stale = [
                k
                for k, e in self._entries.items()
                if e.schema_version != schema_version
            ]
            for k in stale:
                del self._entries[k]
            return len(stale)

    async def iter_entries(self) -> AsyncIterator[CacheEntry]:
        async with self._lock:
            self._raise_if_closed()
            snapshot = list(self._entries.values())
        for entry in snapshot:
            yield entry

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._entries.clear()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("ScanCache is closed")


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS scan_cache (
        key             TEXT PRIMARY KEY,
        fingerprint     TEXT NOT NULL,
        schema_version  TEXT NOT NULL,
        value           BLOB NOT NULL,
        stored_at       TEXT NOT NULL
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS scan_cache_schema_idx
        ON scan_cache(schema_version);
    """,
)


class SqliteScanCache:
    """aiosqlite-backed ScanCache. Use `await SqliteScanCache.open(...)`.

    WAL journal mode + an asyncio.Lock around the single writable
    connection mirrors `SqliteCheckpointStore` so the two stores share
    the same crash-durability and concurrency story.
    """

    def __init__(self, path: Path, conn: aiosqlite.Connection) -> None:
        self._path = path
        self._conn = conn
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, *, path: Path | None = None) -> SqliteScanCache:
        """Open (or create) the shared scan cache database."""
        target = path if path is not None else default_cache_path()
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

    async def __aenter__(self) -> SqliteScanCache:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def get(
        self,
        key: str,
        *,
        fingerprint: str,
        schema_version: str,
    ) -> bytes | None:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                "SELECT fingerprint, schema_version, value FROM scan_cache WHERE key=?",
                (key,),
            )
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
        if row is None:
            return None
        stored_fp, stored_sv, value = row
        if stored_fp != fingerprint or stored_sv != schema_version:
            return None
        return bytes(value)

    async def get_many(self, lookups: Sequence[CacheLookup]) -> dict[str, bytes]:
        if not lookups:
            return {}
        # Single SELECT … WHERE key IN (?, ?, ...) so a sub-source pre-
        # pass for an org with 10**3 repos pays one round-trip instead
        # of 10**3. The fingerprint / schema check happens in Python
        # because SQLite cannot easily compare columns to per-row
        # constants.
        keys = [lk.key for lk in lookups]
        placeholders = ",".join("?" * len(keys))
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                f"SELECT key, fingerprint, schema_version, value "
                f"FROM scan_cache WHERE key IN ({placeholders})",
                keys,
            )
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        rows_by_key = {row[0]: (row[1], row[2], bytes(row[3])) for row in rows}
        out: dict[str, bytes] = {}
        for lk in lookups:
            row = rows_by_key.get(lk.key)
            if row is None:
                continue
            stored_fp, stored_sv, value = row
            if stored_fp != lk.fingerprint or stored_sv != lk.schema_version:
                continue
            out[lk.key] = value
        return out

    async def put(
        self,
        key: str,
        *,
        fingerprint: str,
        schema_version: str,
        value: bytes,
    ) -> None:
        async with self._lock:
            self._raise_if_closed()
            await self._conn.execute(
                _UPSERT,
                (
                    key,
                    fingerprint,
                    schema_version,
                    value,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await self._conn.commit()

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._raise_if_closed()
            await self._conn.execute("DELETE FROM scan_cache WHERE key=?", (key,))
            await self._conn.commit()

    async def purge_other_schemas(self, schema_version: str) -> int:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                "DELETE FROM scan_cache WHERE schema_version<>?",
                (schema_version,),
            )
            try:
                removed = cur.rowcount or 0
            finally:
                await cur.close()
            await self._conn.commit()
        return int(removed)

    async def iter_entries(self) -> AsyncIterator[CacheEntry]:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                "SELECT key, fingerprint, schema_version, value, stored_at "
                "FROM scan_cache ORDER BY key ASC"
            )
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        for row in rows:
            yield CacheEntry(
                key=row[0],
                fingerprint=row[1],
                schema_version=row[2],
                value=bytes(row[3]),
                stored_at=_parse_iso(row[4]),
            )

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._conn.close()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("ScanCache is closed")


_UPSERT = """
INSERT INTO scan_cache (key, fingerprint, schema_version, value, stored_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(key) DO UPDATE SET
    fingerprint    = excluded.fingerprint,
    schema_version = excluded.schema_version,
    value          = excluded.value,
    stored_at      = excluded.stored_at
"""


def _parse_iso(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


__all__ = [
    "CacheEntry",
    "CacheLookup",
    "MemoryScanCache",
    "ScanCache",
    "SqliteScanCache",
    "default_cache_path",
]
