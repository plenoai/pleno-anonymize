"""SQLite-backed CheckpointStore (default persistence for incremental scan).

Storage layout (ADR-0007 §5):

  ~/$XDG_STATE_HOME/pleno/<scan_id>/checkpoint.sqlite

WAL journal mode + an asyncio.Lock around the single writable connection
gives us:

  * Crash durability after each `save()` (WAL fsync on commit).
  * Concurrent reads from other in-process tasks while a writer holds the
    lock (WAL allows readers to see the last consistent snapshot).
  * A single writer per process, which matches SQLite's threading model
    and avoids `database is locked` retry loops under contention.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .checkpoint import (
    Checkpoint,
    ShardRecord,
    decode_byte_range,
    encode_byte_range,
)


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS scan_state (
        scan_id          TEXT NOT NULL,
        source_id        TEXT NOT NULL,
        cursor           TEXT,
        last_doc_ref     TEXT,
        last_byte_range  TEXT,
        updated_at       TEXT NOT NULL,
        PRIMARY KEY (scan_id, source_id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_findings_shard (
        scan_id          TEXT NOT NULL,
        source_id        TEXT NOT NULL,
        shard_index      INTEGER NOT NULL,
        finding_count    INTEGER NOT NULL,
        written_at       TEXT NOT NULL,
        PRIMARY KEY (scan_id, source_id, shard_index)
    );
    """,
    """
    CREATE INDEX IF NOT EXISTS scan_state_updated_at_idx
        ON scan_state(updated_at);
    """,
)


def default_state_path(scan_id: str) -> Path:
    """Resolve the default SQLite path for a scan, honoring XDG_STATE_HOME.

    XDG Base Directory Specification places per-user persistent state under
    `$XDG_STATE_HOME` (default `~/.local/state`). We add a `pleno/<scan_id>/`
    subdirectory so multiple concurrent scans never share a database file.
    """
    base_env = os.environ.get("XDG_STATE_HOME")
    if base_env:
        base = Path(base_env)
    else:
        base = Path.home() / ".local" / "state"
    return base / "pleno" / scan_id / "checkpoint.sqlite"


class SqliteCheckpointStore:
    """aiosqlite-backed CheckpointStore. Use `await SqliteCheckpointStore.open(...)`."""

    def __init__(self, path: Path, conn: aiosqlite.Connection) -> None:
        # WHY: __init__ takes the already-opened connection so the public
        # `open()` classmethod can perform async initialization (PRAGMA +
        # CREATE TABLE) before any user gets a handle. Direct construction
        # is reserved for tests.
        self._path = path
        self._conn = conn
        self._lock = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls, scan_id: str, *, path: Path | None = None
    ) -> SqliteCheckpointStore:
        """Open (or create) the SQLite store for `scan_id`.

        `path` overrides the default XDG location. Parent directories are
        created with `0o700` so an unprivileged user on a multi-tenant box
        cannot read another tenant's resume cursors (cursors leak access
        patterns even though raw secrets are not stored here).
        """
        target = path if path is not None else default_state_path(scan_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = await aiosqlite.connect(target)
        try:
            await conn.execute("PRAGMA journal_mode=WAL;")
            await conn.execute("PRAGMA synchronous=NORMAL;")
            await conn.execute("PRAGMA foreign_keys=ON;")
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

    async def __aenter__(self) -> SqliteCheckpointStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def save(self, cp: Checkpoint) -> None:
        async with self._lock:
            self._raise_if_closed()
            await self._conn.execute(_UPSERT_CP, _cp_row(cp))
            await self._conn.commit()

    async def save_many(self, cps: list[Checkpoint]) -> None:
        if not cps:
            return
        async with self._lock:
            self._raise_if_closed()
            # WHY: executemany inside an explicit transaction means a crash
            # mid-batch leaves the database at the prior commit point. The
            # whole batch is atomic from the caller's perspective.
            await self._conn.executemany(_UPSERT_CP, [_cp_row(cp) for cp in cps])
            await self._conn.commit()

    async def load(
        self, scan_id: str, source_id: str
    ) -> Checkpoint | None:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                _SELECT_CP, (scan_id, source_id)
            )
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
        if row is None:
            return None
        return _row_to_cp(row)

    async def list_for_scan(
        self, scan_id: str
    ) -> AsyncIterator[Checkpoint]:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(_SELECT_BY_SCAN, (scan_id,))
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        for row in rows:
            yield _row_to_cp(row)

    async def delete(self, scan_id: str, source_id: str) -> None:
        async with self._lock:
            self._raise_if_closed()
            await self._conn.execute(
                "DELETE FROM scan_state WHERE scan_id=? AND source_id=?",
                (scan_id, source_id),
            )
            await self._conn.execute(
                "DELETE FROM scan_findings_shard "
                "WHERE scan_id=? AND source_id=?",
                (scan_id, source_id),
            )
            await self._conn.commit()

    async def record_shard(
        self,
        scan_id: str,
        source_id: str,
        shard_index: int,
        finding_count: int,
    ) -> None:
        async with self._lock:
            self._raise_if_closed()
            await self._conn.execute(
                _UPSERT_SHARD,
                (
                    scan_id,
                    source_id,
                    shard_index,
                    finding_count,
                    datetime.now(UTC).isoformat(),
                ),
            )
            await self._conn.commit()

    async def list_shards(
        self, scan_id: str, source_id: str
    ) -> list[ShardRecord]:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                _SELECT_SHARDS, (scan_id, source_id)
            )
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        return [
            ShardRecord(
                scan_id=row[0],
                source_id=row[1],
                shard_index=int(row[2]),
                finding_count=int(row[3]),
                written_at=_parse_iso(row[4]),
            )
            for row in rows
        ]

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._conn.close()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("CheckpointStore is closed")


_UPSERT_CP = """
INSERT INTO scan_state
    (scan_id, source_id, cursor, last_doc_ref, last_byte_range, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(scan_id, source_id) DO UPDATE SET
    cursor          = excluded.cursor,
    last_doc_ref    = excluded.last_doc_ref,
    last_byte_range = excluded.last_byte_range,
    updated_at      = excluded.updated_at
"""

_SELECT_CP = """
SELECT scan_id, source_id, cursor, last_doc_ref, last_byte_range, updated_at
FROM scan_state
WHERE scan_id=? AND source_id=?
"""

_SELECT_BY_SCAN = """
SELECT scan_id, source_id, cursor, last_doc_ref, last_byte_range, updated_at
FROM scan_state
WHERE scan_id=?
ORDER BY source_id ASC
"""

_UPSERT_SHARD = """
INSERT INTO scan_findings_shard
    (scan_id, source_id, shard_index, finding_count, written_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(scan_id, source_id, shard_index) DO UPDATE SET
    finding_count = excluded.finding_count,
    written_at    = excluded.written_at
"""

_SELECT_SHARDS = """
SELECT scan_id, source_id, shard_index, finding_count, written_at
FROM scan_findings_shard
WHERE scan_id=? AND source_id=?
ORDER BY shard_index ASC
"""


def _cp_row(cp: Checkpoint) -> tuple[Any, ...]:
    return (
        cp.scan_id,
        cp.source_id,
        cp.cursor,
        cp.last_doc_fingerprint,
        encode_byte_range(cp.last_byte_range),
        cp.updated_at.isoformat(),
    )


def _row_to_cp(row: tuple[Any, ...]) -> Checkpoint:
    return Checkpoint(
        scan_id=row[0],
        source_id=row[1],
        cursor=row[2],
        last_doc_fingerprint=row[3],
        last_byte_range=decode_byte_range(row[4]),
        updated_at=_parse_iso(row[5]),
    )


def _parse_iso(value: str) -> datetime:
    # WHY: SQLite stores TEXT verbatim; fromisoformat round-trips datetimes
    # serialized via .isoformat() including timezone for both 3.11+ behavior
    # and naive datetimes (we always emit UTC-aware).
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


__all__ = ["SqliteCheckpointStore", "default_state_path"]
