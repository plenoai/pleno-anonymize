"""AuditEvent + NdjsonHmacAuditLogger + verify_chain + OtlpAuditLogger."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pleno_pii_scanner.governance.audit import (
    AuditEvent,
    AuditLogger,
    NdjsonHmacAuditLogger,
    OtlpAuditLogger,
    verify_chain,
)
from pleno_pii_scanner.governance.rbac import Action, Subject

KEY = b"test-hmac-key-not-for-prod"


def _make_event(
    eid: str = "evt-1", target: str = "scan:abc", metadata: dict | None = None
) -> AuditEvent:
    return AuditEvent(
        event_id=eid,
        timestamp=datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC),
        actor=Subject(id="alice", kind="user", teams=("security",)),
        action=Action.SCAN_SUBMIT,
        target=target,
        decision="allow",
        metadata=metadata or {},
    )


async def test_emit_and_verify_chain(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(log_path, KEY)
    for i in range(3):
        await logger.emit(_make_event(eid=f"evt-{i}"))
    await logger.close()
    assert verify_chain(log_path, KEY) is True
    lines = log_path.read_text().splitlines()
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["prev_hmac"] == "00" * 32
    assert "hmac" in first
    second = json.loads(lines[1])
    assert second["prev_hmac"] == first["hmac"]


async def test_verify_chain_returns_true_for_missing_file(tmp_path: Path) -> None:
    assert verify_chain(tmp_path / "no-such.ndjson", KEY) is True


async def test_verify_chain_detects_tampered_field(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(log_path, KEY)
    await logger.emit(_make_event(eid="evt-0"))
    await logger.emit(_make_event(eid="evt-1"))
    await logger.close()
    # Tamper with the target field of line 0 without recomputing HMAC.
    raw = log_path.read_text().splitlines()
    obj = json.loads(raw[0])
    obj["target"] = "scan:tampered"
    raw[0] = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    log_path.write_text("\n".join(raw) + "\n")
    assert verify_chain(log_path, KEY) is False


async def test_verify_chain_detects_missing_line(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(log_path, KEY)
    for i in range(3):
        await logger.emit(_make_event(eid=f"evt-{i}"))
    await logger.close()
    raw = log_path.read_text().splitlines()
    # Drop the middle line: line 2's prev_hmac no longer matches line 0's hmac.
    log_path.write_text(raw[0] + "\n" + raw[2] + "\n")
    assert verify_chain(log_path, KEY) is False


async def test_verify_chain_detects_malformed_json(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    log_path.write_text("not-json\n")
    assert verify_chain(log_path, KEY) is False


async def test_verify_chain_detects_missing_hmac_field(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    log_path.write_text(json.dumps({"event_id": "x"}) + "\n")
    assert verify_chain(log_path, KEY) is False


async def test_verify_chain_skips_blank_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(log_path, KEY)
    await logger.emit(_make_event(eid="evt-0"))
    await logger.close()
    # Append a blank line - should not break verification.
    with log_path.open("a") as fh:
        fh.write("\n\n")
    assert verify_chain(log_path, KEY) is True


async def test_recovery_continues_chain(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(log_path, KEY)
    await logger.emit(_make_event(eid="evt-0"))
    await logger.close()
    # Reopen — the new logger must read the existing tail and chain to it.
    logger2 = NdjsonHmacAuditLogger(log_path, KEY)
    await logger2.emit(_make_event(eid="evt-1"))
    await logger2.close()
    assert verify_chain(log_path, KEY) is True
    lines = log_path.read_text().splitlines()
    assert len(lines) == 2


async def test_recovery_skips_blank_lines_in_tail(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(log_path, KEY)
    await logger.emit(_make_event(eid="evt-0"))
    await logger.close()
    with log_path.open("a") as fh:
        fh.write("\n")
    logger2 = NdjsonHmacAuditLogger(log_path, KEY)
    await logger2.emit(_make_event(eid="evt-1"))
    await logger2.close()
    assert verify_chain(log_path, KEY) is True


async def test_emit_after_close_raises(tmp_path: Path) -> None:
    logger = NdjsonHmacAuditLogger(tmp_path / "audit.ndjson", KEY)
    await logger.close()
    with pytest.raises(RuntimeError, match="closed"):
        await logger.emit(_make_event())


def test_logger_rejects_empty_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        NdjsonHmacAuditLogger(tmp_path / "x.ndjson", b"")


async def test_logger_path_property(tmp_path: Path) -> None:
    p = tmp_path / "subdir" / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(p, KEY)
    assert logger.path == p
    await logger.emit(_make_event())
    await logger.close()
    assert p.exists()


async def test_event_repr_masks_secret_metadata() -> None:
    event = _make_event(metadata={"token": "ghp_super_secret", "user_id": "alice"})
    rep = repr(event)
    assert "ghp_super_secret" not in rep
    assert "***" in rep
    assert "alice" in rep


async def test_audit_log_does_not_persist_raw_secret(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.ndjson"
    logger = NdjsonHmacAuditLogger(log_path, KEY)
    await logger.emit(
        _make_event(metadata={"github_token": "ghp_oops_leaked", "request_id": "r1"})
    )
    await logger.close()
    body = log_path.read_text()
    assert "ghp_oops_leaked" not in body
    assert "***" in body
    assert "r1" in body


async def test_event_to_payload_shape() -> None:
    event = _make_event(metadata={"k": "v"})
    payload = event.to_payload()
    assert payload["event_id"] == "evt-1"
    assert payload["actor"] == {"id": "alice", "kind": "user", "teams": ["security"]}
    assert payload["action"] == "scan:submit"
    assert payload["decision"] == "allow"
    assert payload["metadata"] == {"k": "v"}
    assert isinstance(payload["timestamp"], str)


async def test_audit_logger_protocol_runtime_check(tmp_path: Path) -> None:
    logger = NdjsonHmacAuditLogger(tmp_path / "audit.ndjson", KEY)
    assert isinstance(logger, AuditLogger)
    otlp = OtlpAuditLogger()
    assert isinstance(otlp, AuditLogger)


async def test_otlp_logger_collects_events() -> None:
    logger = OtlpAuditLogger()
    e1, e2 = _make_event("a"), _make_event("b")
    await logger.emit(e1)
    await logger.emit(e2)
    assert logger.events == (e1, e2)
    await logger.close()
    with pytest.raises(RuntimeError, match="closed"):
        await logger.emit(_make_event("c"))
