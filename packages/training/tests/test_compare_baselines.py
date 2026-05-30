"""Tests for pleno_ner_training.compare_baselines (U4).

Covers:
- F0a: clean pass + doc-level overlap abort + template-level overlap abort
- F0b: carry-forward + force recompute
- F0c: real-repo round-trip (git show <blob_sha1> re-hash matches working tree)
- F1: predictor_factory injection, score-bearing + score-less variants
- F2: AE1-AE5 verdict scenarios, n_eligible_templates<4 → NO_DECISION
- R12 partial gate: aggregates + verdict_per_entity OMITTED (KeyError on read)
- Full chain integration on a tiny synthetic corpus.

All tests stub baselines via `predictor_factory` + a custom `spec_lookup` —
no real spaCy/Presidio loads.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from pleno_ner_training import compare_baselines as cb
from pleno_ner_training.baselines_ja import BaselineSpec, Predictor


# --- helpers ----------------------------------------------------------------


def _nfc_sha256(text: str) -> str:
    n = (
        unicodedata.normalize("NFC", text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    return hashlib.sha256(n.encode("utf-8")).hexdigest()


def _make_corpus(docs: list[dict]) -> list[dict]:
    """Build a corpus list from compact dicts."""
    out: list[dict] = []
    for i, d in enumerate(docs):
        out.append(
            {
                "text": d["text"],
                "entities": d.get("entities", []),
                "_meta": {
                    "template": d["template"],
                    "doc_idx": i,
                    "version": "vtest",
                    "language": "ja",
                },
            }
        )
    return out


def _write_corpus(tmp_path: Path, corpus: list[dict]) -> Path:
    p = tmp_path / "raw.json"
    p.write_text(json.dumps(corpus, ensure_ascii=False), "utf-8")
    return p


def _write_manifest(
    tmp_path: Path,
    *,
    doc_hashes: list[str] | None = None,
    template_fps: list[str] | None = None,
) -> Path:
    p = tmp_path / "training_corpus_manifest.json"
    p.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "path": "data/processed/ja/train.spacy",
                        "sha256_nfc": "deadbeef",
                        "doc_hashes": doc_hashes or [],
                        "template_fingerprints": template_fps or [],
                    }
                ]
            }
        ),
        "utf-8",
    )
    return p


@dataclass
class _StubPredictor:
    """Returns canned predictions per text."""

    table: dict[str, list[tuple]]

    def predict(self, text: str) -> list[tuple]:
        return self.table.get(text, [])


def _stub_factory(
    table_by_variant: dict[str, dict[str, list[tuple]]],
    fail: set[str] | None = None,
):
    fail = fail or set()

    def factory(name: str) -> Predictor:
        if name in fail:
            raise RuntimeError(f"stub failure for {name}")
        return _StubPredictor(table_by_variant.get(name, {}))

    return factory


def _stub_specs(score_bearing: dict[str, bool]) -> dict[str, BaselineSpec]:
    """Build a spec_lookup with the given score_bearing flags + categories.

    Naming convention: names starting with `oss_*` get category=`oss_presidio`,
    `custom_*` get category=`custom`.
    """
    out: dict[str, BaselineSpec] = {}
    for name, sb in score_bearing.items():
        category = "oss_presidio" if name.startswith("oss_") else "custom"
        out[name] = BaselineSpec(
            name=name,
            category=category,
            score_bearing=sb,
            builder=lambda: (_ for _ in ()).throw(RuntimeError("stub builder")),
        )
    return out


# ============================================================================
# F0a: data leakage
# ============================================================================


def test_f0a_clean_passes(tmp_path: Path) -> None:
    corpus = _make_corpus(
        [
            {
                "text": "ABC会社の太郎",
                "template": "t1",
                "entities": [{"start": 0, "end": 4, "label": "ORGANIZATION"}],
            },
        ]
    )
    corpus_path = _write_corpus(tmp_path, corpus)
    manifest_path = _write_manifest(tmp_path)
    result = cb.f0a_data_leakage_check(corpus_path, manifest_path)
    assert result["passed"] is True
    assert result["doc_overlap_count"] == 0
    assert result["template_overlap_count"] == 0
    assert result["algorithm"] == "SHA256-NFC"
    # manifest_hash is deterministic SHA256 of file bytes.
    assert len(result["manifest_hash"]) == 64


def test_f0a_doc_overlap_aborts(tmp_path: Path) -> None:
    bench_text = "重複する本文テスト"
    corpus = _make_corpus([{"text": bench_text, "template": "tA", "entities": []}])
    corpus_path = _write_corpus(tmp_path, corpus)
    manifest_path = _write_manifest(
        tmp_path,
        doc_hashes=[_nfc_sha256(bench_text)],
    )
    result = cb.f0a_data_leakage_check(corpus_path, manifest_path)
    assert result["passed"] is False
    assert result["doc_overlap_count"] == 1


def test_f0a_template_overlap_aborts(tmp_path: Path) -> None:
    corpus = _make_corpus(
        [
            {
                "text": "全く別の本文",
                "template": "ocr_forms_a.txt",
                "entities": [{"start": 0, "end": 1, "label": "ORGANIZATION"}],
            }
        ]
    )
    corpus_path = _write_corpus(tmp_path, corpus)
    # Match the template fingerprint that f0a will compute for the bench doc.
    bench_fp = hashlib.sha256(
        ("ocr_forms_a.txt" + "|" + "ORGANIZATION").encode("utf-8")
    ).hexdigest()
    manifest_path = _write_manifest(tmp_path, template_fps=[bench_fp])
    result = cb.f0a_data_leakage_check(corpus_path, manifest_path)
    assert result["passed"] is False
    assert result["template_overlap_count"] == 1


def test_f0a_missing_manifest_aborts(tmp_path: Path) -> None:
    corpus_path = _write_corpus(
        tmp_path, _make_corpus([{"text": "x", "template": "t"}])
    )
    with pytest.raises(FileNotFoundError):
        cb.f0a_data_leakage_check(corpus_path, tmp_path / "missing.json")


# ============================================================================
# F0b: noise floor lifecycle
# ============================================================================


def test_f0b_carry_forward_when_unchanged(tmp_path: Path) -> None:
    nf_path = tmp_path / "noise_floor.json"
    # First call: writes a fresh pin.
    first = cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v0.12.0",
        variant_set_hash="vh1",
        manifest_hash="mh1",
    )
    assert first["carried_forward"] is False
    assert nf_path.exists()
    # Second call with identical inputs: carry-forward.
    second = cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v0.12.0",
        variant_set_hash="vh1",
        manifest_hash="mh1",
    )
    assert second["carried_forward"] is True
    assert second["per_entity_floor"] == first["per_entity_floor"]


def test_f0b_recompute_when_manifest_changes(tmp_path: Path) -> None:
    nf_path = tmp_path / "noise_floor.json"
    cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v0.12.0",
        variant_set_hash="vh1",
        manifest_hash="mh1",
    )
    # Different manifest hash → recompute.
    third = cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v0.12.0",
        variant_set_hash="vh1",
        manifest_hash="mh2_DIFFERENT",
    )
    assert third["carried_forward"] is False
    assert third["manifest_hash"] == "mh2_DIFFERENT"


def test_f0b_force_recompute(tmp_path: Path) -> None:
    nf_path = tmp_path / "noise_floor.json"
    cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v",
        variant_set_hash="vh",
        manifest_hash="mh",
    )
    forced = cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v",
        variant_set_hash="vh",
        manifest_hash="mh",
        force_recompute=True,
    )
    assert forced["carried_forward"] is False


# ============================================================================
# F0c: recognizers git SHA round-trip on the real repo file
# ============================================================================


def test_f0c_round_trip_on_real_recognizers_file() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    rec_path = repo_root / cb.RECOGNIZERS_PATH_REL
    if not rec_path.exists():
        pytest.skip("recognizers_ja.py not at expected path")
    meta = cb.f0c_recognizers_git_sha(rec_path, repo_root=repo_root)
    assert len(meta["git_blob_sha1"]) == 40
    assert len(meta["content_sha256"]) == 64
    assert len(meta["git_commit_sha"]) == 40
    # SHA1 (git blob) and SHA256 (content) are independent values.
    assert meta["git_blob_sha1"] != meta["content_sha256"]
    # Round-trip: git show output re-hashed must match working-tree content hash.
    assert meta["round_trip_clean"] is True


# ============================================================================
# F1: measurement with stub factory
# ============================================================================


def test_f1_predictor_factory_injection() -> None:
    corpus = _make_corpus(
        [
            {
                "text": "doc1",
                "template": "tA",
                "entities": [{"start": 0, "end": 4, "label": "ORGANIZATION"}],
            },
            {
                "text": "doc2",
                "template": "tA",
                "entities": [{"start": 0, "end": 4, "label": "DATE_OF_BIRTH"}],
            },
        ]
    )
    factory = _stub_factory(
        {
            "oss_a": {
                "doc1": [(0, 4, "ORGANIZATION", 0.9, 0)],
                "doc2": [(0, 4, "DATE_OF_BIRTH", 0.8, 0)],
            },
            "custom_a": {
                "doc1": [],
                "doc2": [],
            },
        }
    )
    out = cb.f1_measurement(
        ["oss_a", "custom_a"], corpus, "cpu", predictor_factory=factory
    )
    assert set(out["predictions_by_variant"].keys()) == {"oss_a", "custom_a"}
    assert out["failed_variants"] == []
    assert out["time_box_exceeded"] is False
    rows = out["predictions_by_variant"]["oss_a"]
    assert len(rows) == 2
    assert rows[0]["predictions"] == [(0, 4, "ORGANIZATION", 0.9, 0)]
    assert rows[0]["template"] == "tA"


def test_f1_failure_records_in_failed_variants() -> None:
    corpus = _make_corpus([{"text": "x", "template": "t"}])
    factory = _stub_factory(
        {"oss_a": {"x": []}, "custom_a": {"x": []}},
        fail={"custom_a"},
    )
    out = cb.f1_measurement(
        ["oss_a", "custom_a"], corpus, "cpu", predictor_factory=factory
    )
    assert "custom_a" in out["failed_variants"]
    assert "oss_a" in out["predictions_by_variant"]


# ============================================================================
# F2: verdict scenarios + R12 partial gate
# ============================================================================


def _build_synthetic_balanced_corpus(
    *,
    n_org_templates: int = 5,
    n_dob_templates: int = 4,
    spans_per_template: int = 5,
) -> list[dict]:
    """Build a corpus where each template has `spans_per_template` ORG and DOB
    spans, satisfying eligibility (ORG≥5 / DOB≥3 per template, ≥4 templates)."""
    docs: list[dict] = []
    for ti in range(max(n_org_templates, n_dob_templates)):
        tname = f"tmpl_{ti}"
        # Unique text per doc so the stub-factory dict (keyed by text) does
        # not collapse duplicates. Body uses single-byte ASCII so char offsets
        # = byte offsets.
        prefix = f"d{ti:02d}_"
        body = "X" * (spans_per_template * 4)
        text = prefix + body
        offset_base = len(prefix)
        ents = []
        for si in range(spans_per_template):
            offset = offset_base + si * 4
            if ti < n_org_templates:
                ents.append(
                    {"start": offset, "end": offset + 2, "label": "ORGANIZATION"}
                )
            if ti < n_dob_templates:
                ents.append(
                    {"start": offset + 2, "end": offset + 4, "label": "DATE_OF_BIRTH"}
                )
        docs.append({"text": text, "template": tname, "entities": ents})
    return _make_corpus(docs)


def _predictions_from_gold(
    corpus: list[dict],
    *,
    coverage: float,
    score: float = 0.9,
) -> dict[str, list[tuple]]:
    """For each doc, emit predictions covering the first `coverage` fraction
    of gold spans (per entity, deterministically). Returns table keyed by text."""
    table: dict[str, list[tuple]] = {}
    for entry in corpus:
        preds: list[tuple] = []
        rank = 0
        for ent_label in cb.ENTITIES:
            ents = [e for e in entry["entities"] if e["label"] == ent_label]
            n_keep = int(round(len(ents) * coverage))
            for e in ents[:n_keep]:
                preds.append((e["start"], e["end"], e["label"], score, rank))
                rank += 1
        table[entry["text"]] = preds
    return table


def _f1_run(
    variants: list[str],
    corpus: list[dict],
    factory_table: dict[str, dict[str, list[tuple]]],
    *,
    fail: set[str] | None = None,
) -> dict[str, list[dict]]:
    out = cb.f1_measurement(
        variants,
        corpus,
        "cpu",
        predictor_factory=_stub_factory(factory_table, fail=fail),
    )
    return out["predictions_by_variant"]


def _zero_noise_floor() -> dict[str, Any]:
    return {"per_entity_floor": {"ORGANIZATION": 0.0, "DATE_OF_BIRTH": 0.0}}


# ----- AE1: clear OSS-better → KILL for both entities -----------------------


def test_ae1_clear_oss_better_kills_both_entities() -> None:
    corpus = _build_synthetic_balanced_corpus(
        n_org_templates=5, n_dob_templates=4, spans_per_template=5
    )
    oss_table = _predictions_from_gold(corpus, coverage=1.0, score=0.95)
    # custom is much weaker (predicts nothing).
    custom_table = {entry["text"]: [] for entry in corpus}
    spec_lookup = _stub_specs({"oss_strong": True, "custom_weak": False})
    preds = _f1_run(
        ["oss_strong", "custom_weak"],
        corpus,
        {"oss_strong": oss_table, "custom_weak": custom_table},
    )
    artifact = cb.f2_verdict_compute(
        preds, _zero_noise_floor(), partial_run=False, spec_lookup=spec_lookup
    )
    assert artifact["verdict_per_entity"]["ORGANIZATION"]["verdict"] == "KILL"
    assert artifact["verdict_per_entity"]["DATE_OF_BIRTH"]["verdict"] == "KILL"


# ----- AE2: clear custom-better → COMMIT for both entities ------------------


def test_ae2_custom_better_commits_both_entities() -> None:
    corpus = _build_synthetic_balanced_corpus(
        n_org_templates=5, n_dob_templates=4, spans_per_template=5
    )
    custom_table = _predictions_from_gold(corpus, coverage=1.0)
    oss_table = {entry["text"]: [] for entry in corpus}
    spec_lookup = _stub_specs({"oss_weak": True, "custom_strong": False})
    preds = _f1_run(
        ["oss_weak", "custom_strong"],
        corpus,
        {"oss_weak": oss_table, "custom_strong": custom_table},
    )
    artifact = cb.f2_verdict_compute(
        preds, _zero_noise_floor(), partial_run=False, spec_lookup=spec_lookup
    )
    assert artifact["verdict_per_entity"]["ORGANIZATION"]["verdict"] == "COMMIT"
    assert artifact["verdict_per_entity"]["DATE_OF_BIRTH"]["verdict"] == "COMMIT"


# ----- AE3: gate fails → NO_DECISION for that entity (n_eligible<4 path) ---


def test_ae3_n_eligible_below_4_forces_no_decision() -> None:
    # DOB has only 2 templates with ≥3 DOB spans → DOB n_eligible=2 < 4.
    # ORG has 5 templates with ≥5 ORG spans → ORG n_eligible=5 ≥ 4.
    corpus = _build_synthetic_balanced_corpus(
        n_org_templates=5, n_dob_templates=2, spans_per_template=5
    )
    oss_table = _predictions_from_gold(corpus, coverage=1.0)
    custom_table = {entry["text"]: [] for entry in corpus}
    spec_lookup = _stub_specs({"oss_a": True, "custom_a": False})
    preds = _f1_run(
        ["oss_a", "custom_a"],
        corpus,
        {"oss_a": oss_table, "custom_a": custom_table},
    )
    artifact = cb.f2_verdict_compute(
        preds, _zero_noise_floor(), partial_run=False, spec_lookup=spec_lookup
    )
    assert artifact["verdict_per_entity"]["DATE_OF_BIRTH"]["verdict"] == "NO_DECISION"
    assert artifact["verdict_per_entity"]["DATE_OF_BIRTH"]["n_eligible_templates"] == 2


# ----- AE4: time-box exceeded simulated → partial_run=True, no aggregates --


def test_ae4_partial_run_omits_aggregates_and_verdict_per_entity() -> None:
    """R12 hard gate: aggregates + verdict_per_entity must not be present (KeyError)."""
    corpus = _build_synthetic_balanced_corpus()
    oss_table = _predictions_from_gold(corpus, coverage=1.0)
    spec_lookup = _stub_specs({"oss_a": True, "custom_a": False})
    preds = _f1_run(
        ["oss_a", "custom_a"],
        corpus,
        {"oss_a": oss_table, "custom_a": {}},
    )
    artifact = cb.f2_verdict_compute(
        preds, _zero_noise_floor(), partial_run=True, spec_lookup=spec_lookup
    )
    # Hard gate: KeyError, not None.
    with pytest.raises(KeyError):
        _ = artifact["aggregates"]
    with pytest.raises(KeyError):
        _ = artifact["verdict_per_entity"]
    assert artifact["partial_run"] is True


def test_leakage_failed_also_omits_aggregates_and_verdict() -> None:
    corpus = _build_synthetic_balanced_corpus()
    oss_table = _predictions_from_gold(corpus, coverage=1.0)
    spec_lookup = _stub_specs({"oss_a": True, "custom_a": False})
    preds = _f1_run(
        ["oss_a", "custom_a"],
        corpus,
        {"oss_a": oss_table, "custom_a": {}},
    )
    artifact = cb.f2_verdict_compute(
        preds,
        _zero_noise_floor(),
        partial_run=False,
        leakage_passed=False,
        spec_lookup=spec_lookup,
    )
    with pytest.raises(KeyError):
        _ = artifact["aggregates"]
    with pytest.raises(KeyError):
        _ = artifact["verdict_per_entity"]


# ----- AE5: partial-kill — ORG kill + DOB commit independently --------------


def test_ae5_partial_kill_org_kill_dob_commit() -> None:
    """Construct: OSS perfect on ORG, zero on DOB; custom zero on ORG, perfect on DOB.
    With ≥4 eligible templates per entity, expect ORG=KILL + DOB=COMMIT."""
    corpus = _build_synthetic_balanced_corpus(
        n_org_templates=5, n_dob_templates=5, spans_per_template=5
    )

    def filter_label(
        table: dict[str, list[tuple]], label: str
    ) -> dict[str, list[tuple]]:
        return {t: [p for p in preds if p[2] == label] for t, preds in table.items()}

    full = _predictions_from_gold(corpus, coverage=1.0)
    oss_table = filter_label(full, "ORGANIZATION")
    custom_table = filter_label(full, "DATE_OF_BIRTH")
    spec_lookup = _stub_specs({"oss_a": True, "custom_a": False})
    preds = _f1_run(
        ["oss_a", "custom_a"],
        corpus,
        {"oss_a": oss_table, "custom_a": custom_table},
    )
    artifact = cb.f2_verdict_compute(
        preds, _zero_noise_floor(), partial_run=False, spec_lookup=spec_lookup
    )
    assert artifact["verdict_per_entity"]["ORGANIZATION"]["verdict"] == "KILL"
    assert artifact["verdict_per_entity"]["DATE_OF_BIRTH"]["verdict"] == "COMMIT"


# ============================================================================
# Full chain: F0a → F0b → F0c → F1 → F2
# ============================================================================


def test_full_chain_integration(tmp_path: Path) -> None:
    corpus = _build_synthetic_balanced_corpus()
    corpus_path = _write_corpus(tmp_path, corpus)
    manifest_path = _write_manifest(tmp_path)

    # F0a
    leakage = cb.f0a_data_leakage_check(corpus_path, manifest_path)
    assert leakage["passed"] is True

    # F0b
    nf_path = tmp_path / "noise_floor.json"
    nf = cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="vtest",
        variant_set_hash="vh",
        manifest_hash=leakage["manifest_hash"],
    )
    assert "per_entity_floor" in nf

    # F0c — only assert structure if the real recognizers file is present
    repo_root = Path(__file__).resolve().parents[3]
    rec_path = repo_root / cb.RECOGNIZERS_PATH_REL
    if rec_path.exists():
        rec_meta = cb.f0c_recognizers_git_sha(rec_path, repo_root=repo_root)
        assert rec_meta["round_trip_clean"] is True

    # F1
    full = _predictions_from_gold(corpus, coverage=1.0)
    spec_lookup = _stub_specs({"oss_a": True, "custom_a": False})
    f1_out = cb.f1_measurement(
        ["oss_a", "custom_a"],
        corpus,
        "cpu",
        predictor_factory=_stub_factory(
            {"oss_a": full, "custom_a": {entry["text"]: [] for entry in corpus}}
        ),
    )
    assert not f1_out["failed_variants"]

    # F2
    artifact = cb.f2_verdict_compute(
        f1_out["predictions_by_variant"],
        nf,
        partial_run=False,
        leakage_passed=leakage["passed"],
        spec_lookup=spec_lookup,
    )
    assert "verdict_per_entity" in artifact
    assert "aggregates" in artifact
    for entity in cb.ENTITIES:
        cell = artifact["verdict_per_entity"][entity]
        for key in (
            "verdict",
            "r7_primary_gate",
            "r8a_min_span_filter",
            "r8b_p10_robust",
            "r8c_dual_metric_agree",
            "r7_diff_sign",
            "r7_diff_ci_lo",
            "r7_diff_ci_hi",
            "n_eligible_templates",
        ):
            assert key in cell, f"missing {key} in {entity}"


# ============================================================================
# Additional invariant: failed-variant short-circuit in F1 propagates partial
# ============================================================================


def test_f1_partial_to_f2_omits_aggregates() -> None:
    corpus = _build_synthetic_balanced_corpus()
    factory = _stub_factory(
        {"oss_a": _predictions_from_gold(corpus, coverage=1.0)},
        fail={"custom_a"},
    )
    f1_out = cb.f1_measurement(
        ["oss_a", "custom_a"], corpus, "cpu", predictor_factory=factory
    )
    partial = bool(f1_out["failed_variants"]) or f1_out["time_box_exceeded"]
    assert partial is True
    spec_lookup = _stub_specs({"oss_a": True, "custom_a": False})
    artifact = cb.f2_verdict_compute(
        f1_out["predictions_by_variant"],
        _zero_noise_floor(),
        partial_run=partial,
        spec_lookup=spec_lookup,
    )
    with pytest.raises(KeyError):
        _ = artifact["verdict_per_entity"]


# ============================================================================
# Artifact metadata population (U6 variant_versions, U7 anchor, U4 rows,
# noise_floor_hash). Regression guards for the silently-empty-field bugs.
# ============================================================================


def test_noise_floor_hash_present_and_stable_across_carry_forward(
    tmp_path: Path,
) -> None:
    nf_path = tmp_path / "noise_floor.json"
    first = cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v1",
        variant_set_hash="vh",
        manifest_hash="mh",
    )
    assert len(first["noise_floor_hash"]) == 64
    second = cb.f0b_noise_floor_pin(
        predictions_by_variant={},
        output_path=nf_path,
        corpus_version="v1",
        variant_set_hash="vh",
        manifest_hash="mh",
    )
    assert second["carried_forward"] is True
    # Carry-forward must hash identically to the run that computed the pin.
    assert second["noise_floor_hash"] == first["noise_floor_hash"]


def test_variant_versions_reports_score_availability_from_registry() -> None:
    names = ["ja_core_news_trf", "custom_cnn"]
    vv = cb._variant_versions(names)
    assert set(vv) == set(names)
    # score_availability is authoritative (read from the real registry spec).
    assert vv["ja_core_news_trf"]["score_availability"] is True
    assert vv["custom_cnn"]["score_availability"] is False
    for entry in vv.values():
        assert set(entry) == {"version", "wheel_sha256", "score_availability"}


def test_build_measurement_rows_emits_exact_span_tp_fp_fn() -> None:
    predictions_by_variant = {
        "custom_a": [
            {
                "doc_idx": 0,
                "template": "t1",
                "predictions": [(0, 4, "ORGANIZATION", None, 0)],
                "gold": [(0, 4, "ORGANIZATION"), (10, 14, "ORGANIZATION")],
            }
        ]
    }
    specs = _stub_specs({"custom_a": False})
    rows = cb._build_measurement_rows(predictions_by_variant, specs)
    org_rows = [r for r in rows if r["entity"] == "ORGANIZATION"]
    assert org_rows == [
        {
            "variant": "custom_a",
            "k_percentile": 100,
            "entity": "ORGANIZATION",
            "template": "t1",
            "tp": 1,
            "fp": 0,
            "fn": 1,
        }
    ]
