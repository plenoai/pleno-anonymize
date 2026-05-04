"""Append-only audit log with HMAC chain (ADR-0007 §10).

Each NDJSON line carries `prev_hmac` + `hmac` so that any post-hoc edit,
re-ordering, or mid-file deletion is detectable by replaying the chain.
The chain seed (line 0's `prev_hmac`) is the literal hex of 32 zero
bytes; honest implementations never emit that as an interior `hmac`
because it would require a SHA256 collision.

`AuditEvent` is a frozen dataclass; `metadata` values are coerced to
strings to keep serialization unambiguous and to ensure `repr` does not
inadvertently dump non-string objects (e.g. raw secrets carried via
`Credential.payload`). Even if a caller mistakenly stuffs a token into
`metadata`, the repr masks every value matching credential broker
secret-key heuristics.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from pleno_pii_scanner.credentials.broker import _is_secret_key
from pleno_pii_scanner.governance.rbac import Action, Subject

# Hex of 32 zero bytes — the chain seed. Splitting from the literal so
# constant-folding is explicit and auditors can search for it.
_CHAIN_SEED: str = "00" * 32


def _mask_metadata(md: Mapping[str, str]) -> dict[str, str]:
    # Defensive masking so a careless `metadata={"token": secret}` still
    # does not leak via repr/log paths. The HMAC payload below uses the
    # masked form too, because the audit log itself must not store raw
    # secrets even when the caller mishandled an event.
    return {k: ("***" if _is_secret_key(k) else v) for k, v in md.items()}


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One audited action.

    `target` is a free-form resource URI ("scan:abc", "source:github:org/r")
    so the schema does not need an enum per resource type. `decision`
    mirrors the RBACEnforcer verdict so denies are recorded too.
    """

    event_id: str
    timestamp: datetime
    actor: Subject
    action: Action
    target: str
    decision: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        masked = _mask_metadata(self.metadata)
        return (
            f"AuditEvent(event_id={self.event_id!r}, "
            f"timestamp={self.timestamp!r}, actor={self.actor!r}, "
            f"action={self.action!r}, target={self.target!r}, "
            f"decision={self.decision!r}, metadata={masked!r})"
        )

    def to_payload(self) -> dict[str, object]:
        """Canonical dict form used by the HMAC and the NDJSON line."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "actor": {
                "id": self.actor.id,
                "kind": self.actor.kind,
                "teams": list(self.actor.teams),
            },
            "action": self.action.value,
            "target": self.target,
            "decision": self.decision,
            "metadata": _mask_metadata(self.metadata),
        }


@runtime_checkable
class AuditLogger(Protocol):
    """Append-only audit sink. Implementations MUST be safe to call
    concurrently because the scheduler emits from multiple coroutines."""

    async def emit(self, event: AuditEvent) -> None: ...
    async def close(self) -> None: ...


def _serialize_for_hmac(payload: dict[str, object]) -> bytes:
    # sort_keys + default UTF-8 separators give a deterministic byte
    # stream so chain verification on re-read produces identical hashes.
    return json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _compute_hmac(key: bytes, prev_hmac: str, payload: dict[str, object]) -> str:
    h = hmac.new(key, digestmod=hashlib.sha256)
    h.update(prev_hmac.encode("ascii"))
    h.update(b"\0")
    h.update(_serialize_for_hmac(payload))
    return h.hexdigest()


class NdjsonHmacAuditLogger:
    """File-backed append-only NDJSON with per-line HMAC chain.

    Concurrency is guarded by a single asyncio.Lock so two emits cannot
    interleave the chain. The cost is minimal because audit emit is rare
    relative to the data plane.
    """

    def __init__(self, path: Path, hmac_key: bytes) -> None:
        if not hmac_key:
            raise ValueError("hmac_key must be non-empty")
        self._path = path
        self._key = hmac_key
        self._lock = asyncio.Lock()
        self._prev_hmac: str = self._recover_chain_tip()
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    def _recover_chain_tip(self) -> str:
        # Read the existing tail so a process restart continues the chain
        # instead of forking it. Validating the whole tail here would be
        # too slow on large logs; we rely on `verify_chain` for audits.
        if not self._path.exists():
            return _CHAIN_SEED
        last_hmac = _CHAIN_SEED
        with self._path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                obj = json.loads(line)
                last_hmac = obj["hmac"]
        return last_hmac

    async def emit(self, event: AuditEvent) -> None:
        if self._closed:
            raise RuntimeError("audit logger is closed")
        async with self._lock:
            payload = event.to_payload()
            mac = _compute_hmac(self._key, self._prev_hmac, payload)
            line_obj = {**payload, "prev_hmac": self._prev_hmac, "hmac": mac}
            line = json.dumps(line_obj, sort_keys=True, ensure_ascii=False)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._prev_hmac = mac

    async def close(self) -> None:
        # No persistent file handle is held (open/close per-line keeps
        # us crash-safe), so close is a state flag flip.
        self._closed = True


def verify_chain(path: Path, hmac_key: bytes) -> bool:
    """Replay every line and verify the HMAC chain is intact.

    Returns False on any of: tampered field (recomputed HMAC mismatch),
    missing line (next line's `prev_hmac` does not equal previous line's
    `hmac`), malformed JSON, missing chain fields.
    """
    if not path.exists():
        return True
    prev = _CHAIN_SEED
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return False
            if "hmac" not in obj or "prev_hmac" not in obj:
                return False
            if obj["prev_hmac"] != prev:
                return False
            payload = {k: v for k, v in obj.items() if k not in ("prev_hmac", "hmac")}
            recomputed = _compute_hmac(hmac_key, prev, payload)
            # constant-time compare so a malicious file cannot time us.
            if not hmac.compare_digest(recomputed, obj["hmac"]):
                return False
            prev = obj["hmac"]
    return True


class OtlpAuditLogger:
    """OpenTelemetry-bound audit sink stub for SIEM forwarding.

    The actual OTLP exporter is wired by an enterprise plugin so the
    core wheel does not pull in `opentelemetry-*`. This skeleton stores
    events in memory; replacing `_export` is the integration seam.
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._closed = False

    @property
    def events(self) -> tuple[AuditEvent, ...]:
        return tuple(self._events)

    async def emit(self, event: AuditEvent) -> None:
        if self._closed:
            raise RuntimeError("audit logger is closed")
        self._events.append(event)
        await self._export(event)

    async def _export(self, event: AuditEvent) -> None:
        # Plugin override target. Default is a no-op so unit tests of
        # callers that only need an AuditLogger Protocol implementation
        # have a working backend without the OTLP dependency.
        return None

    async def close(self) -> None:
        self._closed = True
