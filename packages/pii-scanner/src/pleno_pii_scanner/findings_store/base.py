"""FindingsStore Protocol + shared dataclasses (ADR-0007 §11).

Two-tier persistence:

  1. Index — queryable. Holds (finding_id, fingerprint, source_id,
     entity, severity, status, owner, sla_due_at, value_excerpt, etc).
     Plaintext columns are limited to what is safe to grep / query
     against: fingerprint, entity, file path, connector ID, masked
     excerpt. **Raw `Finding.matched` never lands here.**
  2. Shard — append-only JSONL on disk. Holds the encrypted raw value +
     snippet under per-tenant DEK envelope encryption. One JSONL file per
     `(scan_id, source_id, shard_index)` tuple so independently scheduled
     connectors never contend on the same writer.

Severity is derived from `(verification, entity)` by `default_severity`
unless the FindingsStore is constructed with a custom classifier. The
default rules match ADR §11:

  * verification=passed + entity in CRITICAL_ENTITIES → critical
  * verification=passed + entity in HIGH_ENTITIES     → high
  * verification=unverified                            → medium
  * verification=failed                                → low

`reveal_value` is the only API that ever returns the plaintext
`matched`. It is wrapped in an audit hook (callable supplied at
construction) so every reveal call is recorded; the actual audit log
sink is the responsibility of #10.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from ..models import Finding


Severity = Literal["critical", "high", "medium", "low"]
Status = Literal["open", "triaged", "suppressed", "resolved"]
Verification = Literal["passed", "failed", "unverified"]


# WHY: entity buckets are defined here (not pulled from recognizers/)
# because the severity rules in ADR §11 are a property of the *governance*
# layer, not of the recognizer that emitted the finding. A new recognizer
# does not get to redefine "AWS_SECRET_KEY is critical".
CRITICAL_ENTITIES: frozenset[str] = frozenset(
    {
        "API_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "GCP_SERVICE_ACCOUNT_KEY",
        "GITHUB_TOKEN",
        "PRIVATE_KEY",
        "SECRET_KEY",
        "SLACK_TOKEN",
        "STRIPE_SECRET_KEY",
    }
)

HIGH_ENTITIES: frozenset[str] = frozenset(
    {
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "MY_NUMBER",
        "JP_MY_NUMBER",
        "CREDIT_CARD",
        "IBAN_CODE",
        "JP_BANK_ACCOUNT",
        "PERSON",
        "JP_PERSON",
        "ADDRESS",
        "JP_ADDRESS",
    }
)


SeverityClassifier = Callable[[Finding], Severity]
AuditHook = Callable[[str, str], Awaitable[None] | None]


def default_severity(finding: Finding) -> Severity:
    """ADR §11 severity rules; pure function so tests are trivial."""
    if finding.verification == "passed":
        if finding.entity in CRITICAL_ENTITIES:
            return "critical"
        if finding.entity in HIGH_ENTITIES:
            return "high"
        # WHY: a "passed" verification on an entity that is neither in
        # the critical nor high bucket still beats unverified — a live
        # secret of unknown class is operationally worse than an
        # unverified candidate of the same class.
        return "high"
    if finding.verification == "failed":
        return "low"
    return "medium"


def fingerprint_value(matched: str) -> str:
    """Stable 16-hex fingerprint of the raw matched value.

    Used both as the dedup key in the index and as a search token. The
    raw value is never persisted; this hash is the only durable trace of
    the underlying string outside the encrypted shard.
    """
    return hashlib.sha256(matched.encode("utf-8")).hexdigest()[:16]


def derive_finding_id(scan_id: str, source_id: str, fingerprint: str) -> str:
    """Stable PK for a finding within a scan.

    Includes the scan_id so a re-scan that produces the same fingerprint
    in a new scan still gets a fresh row (the dedup happens on
    fingerprint within the same scan, not across scans).
    """
    h = hashlib.sha256()
    h.update(scan_id.encode("utf-8"))
    h.update(b"\0")
    h.update(source_id.encode("utf-8"))
    h.update(b"\0")
    h.update(fingerprint.encode("utf-8"))
    return h.hexdigest()[:32]


def mask_excerpt(matched: str) -> str:
    """Render a non-reversible visual hint of `matched` for the index.

    Keeps at most 2 leading + 2 trailing characters; everything else is
    replaced by `*`. Returns an all-`*` string for very short matches so
    a 3-character token never round-trips unchanged.
    """
    n = len(matched)
    if n == 0:
        return ""
    if n <= 4:
        return "*" * n
    head = matched[:2]
    tail = matched[-2:]
    return f"{head}{'*' * (n - 4)}{tail}"


@dataclass(frozen=True, slots=True)
class ShardRef:
    """Pointer returned by `save_findings` after a shard flushes."""

    scan_id: str
    source_id: str
    shard_index: int
    path: Path
    finding_count: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FindingRecord:
    """Index-side representation of one persisted finding.

    Carries only what is safe to display in dashboards and queries.
    `value_excerpt` is the masked rendering, never the raw match.
    `__repr__` and `__str__` are overridden defensively so even a
    debug print cannot leak the `matched` value (which isn't a field
    here, but the override also guards against future field additions).
    """

    finding_id: str
    fingerprint: str
    scan_id: str
    source_id: str
    source_kind: str
    entity: str
    file_path: str
    line: int
    col: int
    score: float
    verification: Verification
    severity: Severity
    status: Status
    value_excerpt: str
    shard_index: int
    created_at: datetime
    updated_at: datetime
    pattern_name: str = ""
    owner: str | None = None
    sla_due_at: datetime | None = None
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return (
            f"FindingRecord(finding_id={self.finding_id!r}, "
            f"entity={self.entity!r}, severity={self.severity!r}, "
            f"status={self.status!r}, file={self.file_path!r}:{self.line}, "
            f"excerpt={self.value_excerpt!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True, slots=True)
class EncryptedFinding:
    """Internal: one shard line, raw side. Never crosses the public API."""

    finding_id: str
    fingerprint: str
    tenant_id: str
    nonce: bytes
    ciphertext: bytes
    tag: bytes

    def __repr__(self) -> str:
        return (
            f"EncryptedFinding(finding_id={self.finding_id!r}, "
            f"fingerprint={self.fingerprint!r}, "
            f"tenant_id={self.tenant_id!r}, <encrypted>)"
        )


@runtime_checkable
class FindingsStore(Protocol):
    """Persistence contract for scan findings.

    Implementations MUST:

      * never write `Finding.matched` to the index in plaintext;
      * encrypt raw value + snippet on the shard side under a per-tenant
        DEK protected by a `KekProvider`;
      * UPSERT on `(scan_id, source_id, fingerprint)` so a re-emission of
        the same value during the same scan does not double-count;
      * call the constructor's audit hook on every `reveal_value`;
      * be safe to call concurrently from multiple asyncio tasks.
    """

    async def save_findings(
        self, scan_id: str, source_id: str, findings: list[Finding]
    ) -> ShardRef:
        """Append `findings` to a fresh shard and UPSERT index rows."""
        ...

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
        """Filter the index. Plaintext `matched` is never returned."""
        ...

    async def get(self, finding_id: str) -> FindingRecord | None:
        """Return one record by primary key, or None."""
        ...

    async def reveal_value(self, finding_id: str, *, audit_principal: str) -> str:
        """Decrypt and return the raw `matched` value.

        Implementations MUST emit an audit-log event (via the hook
        supplied at construction) for every call, including failed ones.
        """
        ...

    async def close(self) -> None:
        """Release DB / file handles."""
        ...


__all__ = [
    "AuditHook",
    "CRITICAL_ENTITIES",
    "EncryptedFinding",
    "FindingRecord",
    "FindingsStore",
    "HIGH_ENTITIES",
    "Severity",
    "SeverityClassifier",
    "ShardRef",
    "Status",
    "Verification",
    "default_severity",
    "derive_finding_id",
    "fingerprint_value",
    "mask_excerpt",
]
