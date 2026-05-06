"""In-memory FindingsStore for unit tests and dry-run scans.

Mirrors `SqliteFindingsStore` behavior so the same `FindingsStore`
Protocol assertions exercise both backends. State is lost on `close()`.
The shard side is still encrypted — the memory store is not a "skip
encryption" backend, only a "no disk" one. That keeps tests honest:
encryption regressions cannot hide behind the in-memory path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import PurePath

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
    EncryptedPayload,
    EncryptionError,
    KekProvider,
    decrypt_payload,
    encrypt_payload,
    generate_dek,
)


class MemoryFindingsStore:
    """Process-local FindingsStore; everything held in dicts."""

    def __init__(
        self,
        *,
        kek: KekProvider,
        tenant_id: str = "default",
        source_kind: str = "unknown",
        audit_hook: AuditHook | None = None,
        severity_classifier: SeverityClassifier | None = None,
    ) -> None:
        self._kek = kek
        self._tenant_id = tenant_id
        self._source_kind = source_kind
        self._audit_hook = audit_hook
        self._severity_classifier = severity_classifier or default_severity
        self._lock = asyncio.Lock()
        self._wrapped_deks: dict[str, bytes] = {}
        self._dek_cache: dict[str, bytes] = {}
        self._records: dict[str, FindingRecord] = {}
        self._dedup: dict[tuple[str, str, str], str] = {}
        self._payloads: dict[str, EncryptedPayload] = {}
        self._shard_counters: dict[tuple[str, str], int] = {}
        self._closed = False

    async def __aenter__(self) -> MemoryFindingsStore:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _ensure_dek(self) -> bytes:
        if self._tenant_id in self._dek_cache:
            return self._dek_cache[self._tenant_id]
        async with self._lock:
            self._raise_if_closed()
            wrapped = self._wrapped_deks.get(self._tenant_id)
        if wrapped is None:
            dek = generate_dek()
            wrapped = await self._kek.wrap_dek(self._tenant_id, dek)
            async with self._lock:
                self._raise_if_closed()
                self._wrapped_deks[self._tenant_id] = wrapped
        else:
            dek = await self._kek.unwrap_dek(self._tenant_id, wrapped)
        self._dek_cache[self._tenant_id] = dek
        return dek

    async def save_findings(
        self, scan_id: str, source_id: str, findings: list[Finding]
    ) -> ShardRef:
        self._raise_if_closed()
        dek = await self._ensure_dek()
        async with self._lock:
            self._raise_if_closed()
            shard_index = self._shard_counters.get((scan_id, source_id), 0)
            self._shard_counters[(scan_id, source_id)] = shard_index + 1
        now = datetime.now(UTC)
        count = 0
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
            record = FindingRecord(
                finding_id=finding_id,
                fingerprint=fingerprint,
                scan_id=scan_id,
                source_id=source_id,
                source_kind=self._source_kind,
                entity=finding.entity,
                pattern_name=finding.pattern_name,
                file_path=finding.file,
                line=finding.line,
                col=finding.col,
                score=finding.score,
                verification=finding.verification,
                severity=severity,
                status="open",
                value_excerpt=mask_excerpt(finding.matched),
                shard_index=shard_index,
                created_at=now,
                updated_at=now,
            )
            async with self._lock:
                self._raise_if_closed()
                self._records[finding_id] = record
                self._dedup[(scan_id, source_id, fingerprint)] = finding_id
                self._payloads[finding_id] = payload
            count += 1
        return ShardRef(
            scan_id=scan_id,
            source_id=source_id,
            shard_index=shard_index,
            path=PurePath(f"memory://{scan_id}/{source_id}/{shard_index}"),
            finding_count=count,
            created_at=now,
        )

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
        async with self._lock:
            self._raise_if_closed()
            snapshot = list(self._records.values())
        filtered = [
            r
            for r in snapshot
            if (scan_id is None or r.scan_id == scan_id)
            and (source_kind is None or r.source_kind == source_kind)
            and (entity is None or r.entity == entity)
            and (status is None or r.status == status)
            and (verification is None or r.verification == verification)
            and (severity is None or r.severity == severity)
        ]
        filtered.sort(key=lambda r: (r.created_at, r.finding_id))
        return filtered[offset : offset + limit]

    async def get(self, finding_id: str) -> FindingRecord | None:
        async with self._lock:
            self._raise_if_closed()
            return self._records.get(finding_id)

    async def reveal_value(self, finding_id: str, *, audit_principal: str) -> str:
        async with self._lock:
            self._raise_if_closed()
            payload = self._payloads.get(finding_id)
        await self._emit_audit(finding_id, audit_principal)
        if payload is None:
            raise KeyError(f"finding_id not found: {finding_id}")
        dek = await self._ensure_dek()
        obj = decrypt_payload(dek, payload)
        matched = obj.get("matched")
        if not isinstance(matched, str):
            raise EncryptionError("decrypted payload missing 'matched' string")
        return matched

    async def _emit_audit(self, finding_id: str, audit_principal: str) -> None:
        if self._audit_hook is None:
            return
        result = self._audit_hook(finding_id, audit_principal)
        if isinstance(result, Awaitable):
            await result

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            self._records.clear()
            self._dedup.clear()
            self._payloads.clear()
            self._shard_counters.clear()
            for tenant in list(self._dek_cache):
                self._dek_cache[tenant] = b"\0" * len(self._dek_cache[tenant])
            self._dek_cache.clear()
            self._wrapped_deks.clear()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise RuntimeError("FindingsStore is closed")


__all__ = ["MemoryFindingsStore"]
