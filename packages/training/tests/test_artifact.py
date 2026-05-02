"""U5 tests: artifact schema + log.jsonl extension.

Mandatory acceptance criteria:
- All 21 pre-existing log.jsonl entries parse via LogJsonlEntry (P1-1).
- partial_run=True hard-gates aggregates and verdict_per_entity (P1-2).
- VerdictPerEntity has 8 required fields and rejects extras (P1-4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pleno_ner_training.artifact import (
    ArtifactMetadata,
    ComparisonArtifact,
    LeakageCheck,
    LogJsonlEntry,
    VerdictPerEntity,
    append_log_entry,
    parse_log_jsonl,
    write_artifact,
)


# Path layout: tests/test_artifact.py → parents[1] = packages/training/
TRAINING_ROOT = Path(__file__).resolve().parents[1]
EXISTING_LOG = TRAINING_ROOT / "experiments" / "log.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _leakage_passed() -> LeakageCheck:
    return LeakageCheck(
        algorithm="SHA256-NFC",
        manifest_hash="0" * 64,
        doc_overlap_count=0,
        template_overlap_count=0,
        passed=True,
    )


def _full_metadata() -> ArtifactMetadata:
    return ArtifactMetadata(
        corpus_hash="a" * 64,
        noise_floor_hash="b" * 64,
        recognizers_pack_git_sha="c" * 40,
        recognizers_pack_content_sha256="d" * 64,
        variant_versions={
            "ja_core_news_trf": {
                "version": "3.7.0",
                "wheel_sha256": "e" * 64,
                "score_availability": True,
            }
        },
        bootstrap_seed=42,
        tie_break_rule="(score, doc_id, span_start)",
        k_values=[10, 20, 30, 50, 70, 90, 100],
        leakage_check=_leakage_passed(),
        anchor_pr_sha="f" * 40,
    )


def _verdict(verdict: str = "KILL") -> VerdictPerEntity:
    return VerdictPerEntity(
        verdict=verdict,  # type: ignore[arg-type]
        r7_primary_gate=True,
        r8a_min_span_filter=True,
        r8b_p10_robust=True,
        r8c_dual_metric_agree=True,
        r7_diff_sign="oss_better",
        r7_diff_ci_lo=-0.02,
        r7_diff_ci_hi=0.05,
        n_eligible_templates=12,
    )


def _full_artifact(partial: bool = False) -> ComparisonArtifact:
    if partial:
        return ComparisonArtifact(
            schema_version="1.0",
            run_id="20260502_000000Z",
            metadata=_full_metadata(),
            measurements=[],
            partial_run=True,
            failed_variants=["ja_ginza"],
        )
    return ComparisonArtifact(
        schema_version="1.0",
        run_id="20260502_000000Z",
        metadata=_full_metadata(),
        measurements=[
            {
                "variant": "ja_core_news_trf",
                "k_percentile": 50,
                "entity": "ORG",
                "template": "ocr_forms_a",
                "tp": 1,
                "fp": 0,
                "fn": 0,
            }
        ],
        partial_run=False,
        failed_variants=[],
        aggregates={"ORG": {"diff": 0.01}, "DOB": {"diff": -0.02}},
        verdict_per_entity={"ORG": _verdict("KILL"), "DOB": _verdict("COMMIT")},
    )


# ---------------------------------------------------------------------------
# P1-1: backward compatibility with all 21 existing entries
# ---------------------------------------------------------------------------


def test_existing_21_entries_parse_via_LogJsonlEntry() -> None:
    """The mandatory P1-1 acceptance test."""
    entries = parse_log_jsonl(EXISTING_LOG)
    assert len(entries) == 21


def test_existing_21_entries_individual_lines() -> None:
    lines = EXISTING_LOG.read_text(encoding="utf-8").splitlines()
    parsed = 0
    for line in lines:
        if not line.strip():
            continue
        LogJsonlEntry.model_validate_json(line)  # raises ValidationError on mismatch
        parsed += 1
    assert parsed == 21


def test_legacy_entry_with_extra_unknown_field_is_allowed() -> None:
    """extra='allow' for backward compat."""
    raw = {
        "id": "test_2026_05_02",
        "timestamp": "2026-05-02T00:00:00Z",
        "language": "ja",
        "hypothesis": "h",
        "intervention_type": "data_augmentation",
        "verdict": "KEEP",
        "duration_minutes": 30.0,
        # Unknown legacy column:
        "model_score_old_benchmark": 0.95,
        "templates_added": 5,
    }
    entry = LogJsonlEntry.model_validate(raw)
    assert entry.verdict == "KEEP"
    # The extra field is preserved in dump (for round-trip fidelity).
    dumped = entry.model_dump(exclude_none=True)
    assert dumped["model_score_old_benchmark"] == 0.95


# ---------------------------------------------------------------------------
# New baseline_comparison entry round-trip
# ---------------------------------------------------------------------------


def test_baseline_comparison_entry_KILL_round_trip(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    entry = LogJsonlEntry(
        id="baseline_comparison_v0_12_0",
        timestamp="2026-05-02T12:00:00Z",
        language="ja",
        hypothesis="OSS variants vs custom: ORG and DOB independent verdicts",
        intervention_type="baseline_comparison",
        verdict="KILL",
        artifact_path="experiments/artifacts/20260502_120000Z/comparison.json",
        duration_minutes=180.0,
    )
    append_log_entry(entry, log_path)
    parsed = parse_log_jsonl(log_path)
    assert len(parsed) == 1
    assert parsed[0].verdict == "KILL"
    assert parsed[0].intervention_type == "baseline_comparison"
    assert parsed[0].artifact_path is not None


def test_baseline_comparison_COMMIT_and_NO_DECISION_valid() -> None:
    for v in ("COMMIT", "NO_DECISION"):
        entry = LogJsonlEntry(
            id=f"bc_{v}",
            timestamp="2026-05-02T00:00:00Z",
            language="ja",
            intervention_type="baseline_comparison",
            verdict=v,  # type: ignore[arg-type]
            artifact_path="some/path.json",
        )
        assert entry.verdict == v


def test_invalid_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        LogJsonlEntry(
            id="x",
            timestamp="t",
            language="ja",
            verdict="UNKNOWN_VALUE",  # type: ignore[arg-type]
        )


def test_missing_required_field_rejected() -> None:
    with pytest.raises(ValidationError):
        LogJsonlEntry(timestamp="t", language="ja")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# P1-2: partial-run hard gate
# ---------------------------------------------------------------------------


def test_partial_run_with_aggregates_raises() -> None:
    with pytest.raises(ValidationError):
        ComparisonArtifact(
            schema_version="1.0",
            run_id="r",
            metadata=_full_metadata(),
            measurements=[],
            partial_run=True,
            aggregates={"ORG": {}},
        )


def test_partial_run_with_verdict_per_entity_raises() -> None:
    with pytest.raises(ValidationError):
        ComparisonArtifact(
            schema_version="1.0",
            run_id="r",
            metadata=_full_metadata(),
            measurements=[],
            partial_run=True,
            verdict_per_entity={"ORG": _verdict()},
        )


def test_full_run_without_aggregates_raises() -> None:
    with pytest.raises(ValidationError):
        ComparisonArtifact(
            schema_version="1.0",
            run_id="r",
            metadata=_full_metadata(),
            measurements=[],
            partial_run=False,
            verdict_per_entity={"ORG": _verdict()},
            # aggregates intentionally None
        )


def test_partial_run_artifact_serialization_omits_keys(tmp_path: Path) -> None:
    """JSON output must NOT contain aggregates or verdict_per_entity keys."""
    art = _full_artifact(partial=True)
    out = tmp_path / "comparison.json"
    write_artifact(art, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "aggregates" not in data
    assert "verdict_per_entity" not in data
    assert data["partial_run"] is True
    assert data["failed_variants"] == ["ja_ginza"]


def test_full_run_artifact_serialization_includes_keys(tmp_path: Path) -> None:
    art = _full_artifact(partial=False)
    out = tmp_path / "comparison.json"
    write_artifact(art, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "aggregates" in data
    assert "verdict_per_entity" in data
    assert data["verdict_per_entity"]["ORG"]["verdict"] == "KILL"
    assert data["verdict_per_entity"]["ORG"]["n_eligible_templates"] == 12
    # Stable key order: alphabetical sort.
    assert list(data.keys()) == sorted(data.keys())


# ---------------------------------------------------------------------------
# P1-4: VerdictPerEntity required fields + extra="forbid"
# ---------------------------------------------------------------------------


def test_verdict_per_entity_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        VerdictPerEntity(
            verdict="KILL",
            r7_primary_gate=True,
            r8a_min_span_filter=True,
            r8b_p10_robust=True,
            r8c_dual_metric_agree=True,
            r7_diff_sign="oss_better",
            r7_diff_ci_lo=0.0,
            r7_diff_ci_hi=0.1,
            n_eligible_templates=10,
            unknown_field=True,  # type: ignore[call-arg]
        )


def test_verdict_per_entity_required_8_fields() -> None:
    """Confirm exactly the 8 plan-mandated fields are required."""
    required = {
        "verdict",
        "r7_primary_gate",
        "r8a_min_span_filter",
        "r8b_p10_robust",
        "r8c_dual_metric_agree",
        "r7_diff_sign",
        "r7_diff_ci_lo",
        "r7_diff_ci_hi",
        "n_eligible_templates",
    }
    schema_required = set(VerdictPerEntity.model_fields.keys())
    assert schema_required == required


def test_verdict_per_entity_invalid_diff_sign() -> None:
    with pytest.raises(ValidationError):
        _verdict_kwargs = dict(
            verdict="KILL",
            r7_primary_gate=True,
            r8a_min_span_filter=True,
            r8b_p10_robust=True,
            r8c_dual_metric_agree=True,
            r7_diff_sign="invalid_sign",
            r7_diff_ci_lo=0.0,
            r7_diff_ci_hi=0.0,
            n_eligible_templates=1,
        )
        VerdictPerEntity(**_verdict_kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Round-trip: write_artifact → reload → equal
# ---------------------------------------------------------------------------


def test_write_artifact_round_trip(tmp_path: Path) -> None:
    art = _full_artifact(partial=False)
    out = tmp_path / "sub" / "dir" / "comparison.json"  # parents must be created
    write_artifact(art, out)
    raw = json.loads(out.read_text(encoding="utf-8"))
    reloaded = ComparisonArtifact.model_validate(raw)
    assert reloaded.run_id == art.run_id
    assert reloaded.verdict_per_entity is not None
    assert reloaded.verdict_per_entity["ORG"].verdict == "KILL"


def test_append_log_entry_creates_parent_dirs(tmp_path: Path) -> None:
    log_path = tmp_path / "nested" / "experiments" / "log.jsonl"
    entry = LogJsonlEntry(
        id="x",
        timestamp="t",
        language="ja",
        intervention_type="baseline_comparison",
        verdict="NO_DECISION",
        artifact_path="p.json",
    )
    append_log_entry(entry, log_path)
    assert log_path.exists()
    parsed = parse_log_jsonl(log_path)
    assert parsed[0].id == "x"


def test_append_log_entry_preserves_existing_entries(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    e1 = LogJsonlEntry(
        id="e1", timestamp="t1", language="ja",
        intervention_type="baseline_comparison", verdict="KILL",
    )
    e2 = LogJsonlEntry(
        id="e2", timestamp="t2", language="en",
        intervention_type="baseline_comparison", verdict="COMMIT",
    )
    append_log_entry(e1, log_path)
    append_log_entry(e2, log_path)
    parsed = parse_log_jsonl(log_path)
    assert [p.id for p in parsed] == ["e1", "e2"]


# ---------------------------------------------------------------------------
# parse_log_jsonl error handling
# ---------------------------------------------------------------------------


def test_parse_log_jsonl_rejects_malformed_line(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(
        '{"id": "ok", "timestamp": "t", "language": "ja"}\n'
        '{"id": "bad", "language": "ja"}\n',  # missing timestamp
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        parse_log_jsonl(log_path)


def test_parse_log_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "log.jsonl"
    log_path.write_text(
        '{"id": "a", "timestamp": "t", "language": "ja"}\n'
        '\n'
        '{"id": "b", "timestamp": "t", "language": "ja"}\n',
        encoding="utf-8",
    )
    parsed = parse_log_jsonl(log_path)
    assert len(parsed) == 2
