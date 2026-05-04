"""SQLite-backed FindingsStore (default queryable index, ADR-0007 §11).

The schema is intentionally Postgres-compatible: column names + types
map 1:1 onto a future `pleno-pii-scanner-postgres` extra. Swapping the
backend is a matter of replacing `aiosqlite.connect(path)` with a
Postgres connection pool and updating the SQL dialect quirks (`?`→`$1`,
`ON CONFLICT … DO UPDATE`→same syntax, `INTEGER PRIMARY KEY`→`SERIAL`).

Tables:

  * `tenant_keys` — per-tenant wrapped DEK + KEK provider name. The
    plaintext DEK only ever exists in `_dek_cache` in process memory.
  * `findings` — one row per fingerprint within a scan. Plaintext fields
    are limited to fingerprint, entity, file path, masked excerpt, and
    operational state (severity, status, owner, sla).
  * `shards` — receipts that map (scan_id, source_id, shard_index) to a
    file path so `reveal_value` knows which file to read.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from ..models import Finding
from .base import (
    AuditHook,
    FindingRecord,
    Severity,
    SeverityClassifier,
    ShardRef,
    Status,
    Verification,
    default_severity,
    derive_finding_id,
    fingerprint_value,
    mask_excerpt,
)
from .encryption import (
    EncryptionError,
    KekProvider,
    decrypt_payload,
    encrypt_payload,
    generate_dek,
)
from .jsonl_shard import JsonlShardWriter, default_shard_base, read_shard, shard_path


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS tenant_keys (
        tenant_id     TEXT PRIMARY KEY,
        kek_name      TEXT NOT NULL,
        wrapped_dek   BLOB NOT NULL,
        created_at    TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS findings (
        finding_id    TEXT PRIMARY KEY,
        fingerprint   TEXT NOT NULL,
        scan_id       TEXT NOT NULL,
        source_id     TEXT NOT NULL,
        source_kind   TEXT NOT NULL,
        tenant_id     TEXT NOT NULL,
        entity        TEXT NOT NULL,
        pattern_name  TEXT NOT NULL DEFAULT '',
        file_path     TEXT NOT NULL,
        line          INTEGER NOT NULL,
        col           INTEGER NOT NULL,
        score         REAL NOT NULL,
        verification  TEXT NOT NULL,
        severity      TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'open',
        value_excerpt TEXT NOT NULL,
        shard_index   INTEGER NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        owner         TEXT,
        sla_due_at    TEXT,
        UNIQUE (scan_id, source_id, fingerprint)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS shards (
        scan_id       TEXT NOT NULL,
        source_id     TEXT NOT NULL,
        shard_index   INTEGER NOT NULL,
        path          TEXT NOT NULL,
        finding_count INTEGER NOT NULL,
        created_at    TEXT NOT NULL,
        PRIMARY KEY (scan_id, source_id, shard_index)
    );
    """,
    "CREATE INDEX IF NOT EXISTS findings_scan_idx ON findings(scan_id);",
    "CREATE INDEX IF NOT EXISTS findings_entity_idx ON findings(entity);",
    "CREATE INDEX IF NOT EXISTS findings_status_idx ON findings(status);",
    "CREATE INDEX IF NOT EXISTS findings_severity_idx ON findings(severity);",
)


def default_index_path(scan_id: str) -> Path:
    """XDG-aware index file path; mirrors `state.sqlite_store`."""
    base_env = os.environ.get("XDG_STATE_HOME")
    base = Path(base_env) if base_env else Path.home() / ".local" / "state"
    return base / "pleno" / scan_id / "findings.sqlite"


class SqliteFindingsStore:
    """aiosqlite-backed FindingsStore. Use `await SqliteFindingsStore.open(...)`."""

    def __init__(
        self,
        index_path: Path,
        shard_base: Path,
        conn: aiosqlite.Connection,
        kek: KekProvider,
        tenant_id: str,
        source_kind: str,
        audit_hook: AuditHook | None,
        severity_classifier: SeverityClassifier,
    ) -> None:
        self._index_path = index_path
        self._shard_base = shard_base
        self._conn = conn
        self._kek = kek
        self._tenant_id = tenant_id
        self._source_kind = source_kind
        self._audit_hook = audit_hook
        self._severity_classifier = severity_classifier
        self._lock = asyncio.Lock()
        self._writers: dict[tuple[str, str, int], JsonlShardWriter] = {}
        # WHY: cache unwrapped DEKs in process memory only. Never persist.
        self._dek_cache: dict[str, bytes] = {}
        self._closed = False

    @classmethod
    async def open(
        cls,
        scan_id: str,
        *,
        kek: KekProvider,
        tenant_id: str = "default",
        source_kind: str = "unknown",
        index_path: Path | None = None,
        shard_base: Path | None = None,
        audit_hook: AuditHook | None = None,
        severity_classifier: SeverityClassifier | None = None,
    ) -> SqliteFindingsStore:
        """Open (or create) the index DB and prepare the shard directory."""
        index_target = index_path if index_path is not None else default_index_path(
            scan_id
        )
        shard_target = shard_base if shard_base is not None else default_shard_base()
        index_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shard_target.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = await aiosqlite.connect(index_target)
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
        return cls(
            index_path=index_target,
            shard_base=shard_target,
            conn=conn,
            kek=kek,
            tenant_id=tenant_id,
            source_kind=source_kind,
            audit_hook=audit_hook,
            severity_classifier=severity_classifier or default_severity,
        )

    @property
    def index_path(self) -> Path:
        return self._index_path

    @property
    def shard_base(self) -> Path:
        return self._shard_base

    async def __aenter__(self) -> SqliteFindingsStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _ensure_dek(self) -> bytes:
        """Load (or initialize) the per-tenant DEK; cache in process memory."""
        if self._tenant_id in self._dek_cache:
            return self._dek_cache[self._tenant_id]
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                "SELECT wrapped_dek FROM tenant_keys WHERE tenant_id=?",
                (self._tenant_id,),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                dek = generate_dek()
                wrapped = await self._kek.wrap_dek(self._tenant_id, dek)
                await self._conn.execute(
                    "INSERT INTO tenant_keys "
                    "(tenant_id, kek_name, wrapped_dek, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        self._tenant_id,
                        self._kek.name,
                        wrapped,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                await self._conn.commit()
            else:
                dek = await self._kek.unwrap_dek(
                    self._tenant_id, bytes(row[0])
                )
        self._dek_cache[self._tenant_id] = dek
        return dek

    async def save_findings(
        self, scan_id: str, source_id: str, findings: list[Finding]
    ) -> ShardRef:
        """Encrypt + append shard, UPSERT index rows. Empty list still returns a ShardRef."""
        self._raise_if_closed()
        dek = await self._ensure_dek()
        shard_index = await self._next_shard_index(scan_id, source_id)
        path = shard_path(self._shard_base, scan_id, source_id, shard_index)
        writer = self._writers.get((scan_id, source_id, shard_index))
        if writer is None:
            writer = JsonlShardWriter(path)
            self._writers[(scan_id, source_id, shard_index)] = writer

        finding_ids: list[str] = []
        fingerprints: list[str] = []
        encrypted: list[Any] = []
        index_rows: list[tuple[Any, ...]] = []
        now_iso = datetime.now(UTC).isoformat()

        for finding in findings:
            fingerprint = fingerprint_value(finding.matched)
            finding_id = derive_finding_id(scan_id, source_id, fingerprint)
            severity = self._severity_classifier(finding)
            payload = encrypt_payload(
                dek,
                self._tenant_id,
                {
                    "matched": finding.matched,
                    "snippet": finding.snippet,
                    "extra": {
                        "commit": finding.commit,
                        "author": finding.author,
                        "date": finding.date,
                    },
                },
            )
            finding_ids.append(finding_id)
            fingerprints.append(fingerprint)
            encrypted.append(payload)
            index_rows.append(
                (
                    finding_id,
                    fingerprint,
                    scan_id,
                    source_id,
                    self._source_kind,
                    self._tenant_id,
                    finding.entity,
                    finding.pattern_name,
                    finding.file,
                    finding.line,
                    finding.col,
                    finding.score,
                    finding.verification,
                    severity,
                    "open",
                    mask_excerpt(finding.matched),
                    shard_index,
                    now_iso,
                    now_iso,
                    None,
                    None,
                )
            )

        # WHY: shard write happens BEFORE the index commit so a crash
        # between the two leaves the shard intact (replayable) but no
        # index row pointing at a non-existent shard. The shard itself is
        # additive and idempotent at the (finding_id, fingerprint) level.
        count = await writer.write_batch(finding_ids, fingerprints, encrypted)

        async with self._lock:
            self._raise_if_closed()
            await self._conn.executemany(_UPSERT_FINDING, index_rows)
            await self._conn.execute(
                _UPSERT_SHARD,
                (
                    scan_id,
                    source_id,
                    shard_index,
                    str(path),
                    count,
                    now_iso,
                ),
            )
            await self._conn.commit()

        return ShardRef(
            scan_id=scan_id,
            source_id=source_id,
            shard_index=shard_index,
            path=path,
            finding_count=count,
            created_at=datetime.fromisoformat(now_iso),
        )

    async def _next_shard_index(self, scan_id: str, source_id: str) -> int:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                "SELECT COALESCE(MAX(shard_index), -1) + 1 FROM shards "
                "WHERE scan_id=? AND source_id=?",
                (scan_id, source_id),
            )
            row = await cur.fetchone()
            await cur.close()
        return int(row[0]) if row is not None else 0

    async def query(
        self,
        *,
        scan_id: str | None = None,
        source_kind: str | None = None,
        entity: str | None = None,
        status: Status | None = None,
        verification: Verification | None = None,
        severity: Severity | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FindingRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if scan_id is not None:
            clauses.append("scan_id = ?")
            params.append(scan_id)
        if source_kind is not None:
            clauses.append("source_kind = ?")
            params.append(source_kind)
        if entity is not None:
            clauses.append("entity = ?")
            params.append(entity)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if verification is not None:
            clauses.append("verification = ?")
            params.append(verification)
        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT {_FINDING_COLS} FROM findings{where} "
            "ORDER BY created_at ASC, finding_id ASC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(sql, tuple(params))
            try:
                rows = await cur.fetchall()
            finally:
                await cur.close()
        return [_row_to_record(row) for row in rows]

    async def get(self, finding_id: str) -> FindingRecord | None:
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                f"SELECT {_FINDING_COLS} FROM findings WHERE finding_id=?",
                (finding_id,),
            )
            try:
                row = await cur.fetchone()
            finally:
                await cur.close()
        return _row_to_record(row) if row is not None else None

    async def reveal_value(
        self, finding_id: str, *, audit_principal: str
    ) -> str:
        """Decrypt the raw matched value for one finding.

        Every call — success or failure — fires the audit hook so the
        operator's intent is recorded even when the lookup misses.
        """
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                "SELECT scan_id, source_id, shard_index, fingerprint, tenant_id "
                "FROM findings WHERE finding_id=?",
                (finding_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        await self._emit_audit(finding_id, audit_principal)
        if row is None:
            raise KeyError(f"finding_id not found: {finding_id}")
        scan_id, source_id, shard_index, fingerprint, tenant_id = row
        path = shard_path(
            self._shard_base, scan_id, source_id, int(shard_index)
        )
        entries = await asyncio.to_thread(read_shard, path)
        for fid, fp, payload in entries:
            if fid == finding_id and fp == fingerprint:
                if payload.tenant_id != tenant_id:
                    raise EncryptionError(
                        "shard tenant_id does not match index tenant_id"
                    )
                dek = await self._unwrap_for_tenant(tenant_id)
                obj = decrypt_payload(dek, payload)
                matched = obj.get("matched")
                if not isinstance(matched, str):
                    raise EncryptionError(
                        "decrypted payload missing 'matched' string"
                    )
                return matched
        raise EncryptionError(
            f"finding {finding_id} indexed but absent from shard {path}"
        )

    async def _unwrap_for_tenant(self, tenant_id: str) -> bytes:
        if tenant_id in self._dek_cache:
            return self._dek_cache[tenant_id]
        async with self._lock:
            self._raise_if_closed()
            cur = await self._conn.execute(
                "SELECT wrapped_dek FROM tenant_keys WHERE tenant_id=?",
                (tenant_id,),
            )
            row = await cur.fetchone()
            await cur.close()
        if row is None:
            raise EncryptionError(f"no wrapped DEK for tenant {tenant_id!r}")
        dek = await self._kek.unwrap_dek(tenant_id, bytes(row[0]))
        self._dek_cache[tenant_id] = dek
        return dek

    async def _emit_audit(
        self, finding_id: str, audit_principal: str
    ) -> None:
        if self._audit_hook is None:
            return
        result = self._audit_hook(finding_id, audit_principal)
        if isinstance(result, Awaitable):
            await result

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            for writer in self._writers.values():
                await writer.close()
            self._writers.clear()
            # WHY: zero out DEKs before dropping the dict so a heap dump
            # immediately after shutdown does not still contain key bytes.
            for tenant in list(self._dek_cache):
                self._dek_cache[tenant] = b"\0" * len(self._dek_cache[tenant])
            self._dek_cache.clear()
            await self._conn.close()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("FindingsStore is closed")


_FINDING_COLS = (
    "finding_id, fingerprint, scan_id, source_id, source_kind, entity, "
    "pattern_name, file_path, line, col, score, verification, severity, "
    "status, value_excerpt, shard_index, created_at, updated_at, owner, "
    "sla_due_at"
)


_UPSERT_FINDING = """
INSERT INTO findings
    (finding_id, fingerprint, scan_id, source_id, source_kind, tenant_id,
     entity, pattern_name, file_path, line, col, score, verification,
     severity, status, value_excerpt, shard_index, created_at, updated_at,
     owner, sla_due_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(scan_id, source_id, fingerprint) DO UPDATE SET
    entity        = excluded.entity,
    pattern_name  = excluded.pattern_name,
    file_path     = excluded.file_path,
    line          = excluded.line,
    col           = excluded.col,
    score         = excluded.score,
    verification  = excluded.verification,
    severity      = excluded.severity,
    value_excerpt = excluded.value_excerpt,
    shard_index   = excluded.shard_index,
    updated_at    = excluded.updated_at
"""


_UPSERT_SHARD = """
INSERT INTO shards
    (scan_id, source_id, shard_index, path, finding_count, created_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(scan_id, source_id, shard_index) DO UPDATE SET
    path          = excluded.path,
    finding_count = excluded.finding_count,
    created_at    = excluded.created_at
"""


def _row_to_record(row: tuple[Any, ...]) -> FindingRecord:
    return FindingRecord(
        finding_id=row[0],
        fingerprint=row[1],
        scan_id=row[2],
        source_id=row[3],
        source_kind=row[4],
        entity=row[5],
        pattern_name=row[6] or "",
        file_path=row[7],
        line=int(row[8]),
        col=int(row[9]),
        score=float(row[10]),
        verification=row[11],
        severity=row[12],
        status=row[13],
        value_excerpt=row[14],
        shard_index=int(row[15]),
        created_at=_parse_iso(row[16]),
        updated_at=_parse_iso(row[17]),
        owner=row[18],
        sla_due_at=_parse_iso(row[19]) if row[19] is not None else None,
    )


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


__all__ = ["SqliteFindingsStore", "default_index_path"]
