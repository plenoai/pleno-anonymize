"""Tests for confidence threshold sweep (#68).

Covers:
- BIO span decoder with per-token min-score (predict_hf_with_scores)
- Threshold filtering + per-label P/R/F1 (sweep_threshold.evaluate_at_threshold)
- Recommended-threshold picker (best F1 at recall >= floor + fallback)
- Markdown / CSV emission shape

All tests are pure-Python (no torch / transformers / model files required).
"""

from __future__ import annotations

import csv
from pathlib import Path

from pleno_ner_training.predict_hf_with_scores import decode_bio_spans_with_scores
from pleno_ner_training.sweep_threshold import (
    DEFAULT_THRESHOLDS,
    evaluate_at_threshold,
    pick_recommended_threshold,
    render_markdown,
    run_per_label_sweep,
    run_sweep,
    write_csv,
)


# ---------- BIO decoder ----------


def _id2label() -> dict[int, str]:
    # Mirrors convert_to_hf_dataset.BIO_LABELS shape.
    labels = ["O", "B-ORGANIZATION", "I-ORGANIZATION", "B-PERSON", "I-PERSON"]
    return {i: lbl for i, lbl in enumerate(labels)}


def test_decoder_emits_single_span_with_min_score():
    # Tokens span chars 0..3 ("AB") + 3..6 ("CD") with B-ORG, I-ORG.
    offsets = [(0, 0), (0, 3), (3, 6), (0, 0)]
    label_ids = [0, 1, 2, 0]
    scores = [0.99, 0.91, 0.62, 0.99]
    spans = decode_bio_spans_with_scores(offsets, label_ids, scores, _id2label())
    assert spans == [
        {"start": 0, "end": 6, "label": "ORGANIZATION", "score": 0.62, "tokens": 2}
    ]


def test_decoder_handles_two_adjacent_different_labels():
    offsets = [(0, 3), (3, 6)]
    # B-ORG then B-PERSON -> two separate spans.
    label_ids = [1, 3]
    scores = [0.8, 0.7]
    spans = decode_bio_spans_with_scores(offsets, label_ids, scores, _id2label())
    assert len(spans) == 2
    assert spans[0]["label"] == "ORGANIZATION"
    assert spans[1]["label"] == "PERSON"
    assert spans[0]["score"] == 0.8
    assert spans[1]["score"] == 0.7


def test_decoder_treats_orphan_I_as_B():
    # Robustness: I- without preceding matching B- still opens a span.
    offsets = [(0, 3), (3, 6)]
    label_ids = [2, 2]  # I-ORG, I-ORG
    scores = [0.5, 0.4]
    spans = decode_bio_spans_with_scores(offsets, label_ids, scores, _id2label())
    assert len(spans) == 1
    assert spans[0]["label"] == "ORGANIZATION"
    assert spans[0]["score"] == 0.4
    assert spans[0]["tokens"] == 2


def test_decoder_skips_special_tokens_and_O():
    offsets = [(0, 0), (0, 2), (2, 4), (0, 0)]
    label_ids = [0, 0, 0, 0]
    scores = [0.99, 0.99, 0.99, 0.99]
    assert decode_bio_spans_with_scores(offsets, label_ids, scores, _id2label()) == []


# ---------- threshold sweep ----------


def _docs() -> list[dict]:
    """Fixture: 2 docs, 4 gold, 5 predictions across thresholds."""
    return [
        {
            "text": "...",
            "entities": [
                {"start": 0, "end": 3, "label": "ORGANIZATION"},
                {"start": 5, "end": 8, "label": "PERSON"},
            ],
            "predictions": [
                # Two correct spans at high score, one low-score FP.
                {"start": 0, "end": 3, "label": "ORGANIZATION", "score": 0.95},
                {"start": 5, "end": 8, "label": "PERSON", "score": 0.92},
                {"start": 9, "end": 12, "label": "ORGANIZATION", "score": 0.40},
            ],
        },
        {
            "text": "...",
            "entities": [
                {"start": 0, "end": 3, "label": "ORGANIZATION"},
                {"start": 5, "end": 9, "label": "PERSON"},
            ],
            "predictions": [
                # ORG correct + low-score; PERSON wrong-span low-score.
                {"start": 0, "end": 3, "label": "ORGANIZATION", "score": 0.55},
                {"start": 5, "end": 7, "label": "PERSON", "score": 0.45},
            ],
        },
    ]


def test_evaluate_at_low_threshold_keeps_everything():
    docs = _docs()
    res = evaluate_at_threshold(docs, threshold=0.0, labels=["ORGANIZATION", "PERSON"])
    # ORG: gold=2, pred=3 (2 TP + 1 FP); PERSON: gold=2, pred=2 (1 TP + 1 FP).
    assert res["ORGANIZATION"]["tp"] == 2
    assert res["ORGANIZATION"]["fp"] == 1
    assert res["ORGANIZATION"]["fn"] == 0
    assert res["PERSON"]["tp"] == 1
    assert res["PERSON"]["fp"] == 1
    assert res["PERSON"]["fn"] == 1
    assert res["_overall"]["tp"] == 3
    assert res["_overall"]["fp"] == 2
    assert res["_overall"]["fn"] == 1


def test_evaluate_at_high_threshold_drops_low_score_preds():
    docs = _docs()
    res = evaluate_at_threshold(docs, threshold=0.9, labels=["ORGANIZATION", "PERSON"])
    # Only doc1's two high-score preds survive.
    assert res["ORGANIZATION"]["tp"] == 1
    assert res["ORGANIZATION"]["fp"] == 0
    assert res["ORGANIZATION"]["fn"] == 1
    assert res["PERSON"]["tp"] == 1
    assert res["PERSON"]["fp"] == 0
    assert res["PERSON"]["fn"] == 1
    assert res["_overall"]["p"] == 1.0
    # 2 gold survived in FN, 2 TP -> recall = 0.5
    assert res["_overall"]["r"] == 0.5


def test_evaluate_threshold_strict_match():
    # PERSON pred (5,7) vs gold (5,9) — strict mismatch counts as FP+FN.
    docs = _docs()
    res = evaluate_at_threshold(docs, threshold=0.0, labels=["PERSON"])
    assert res["PERSON"]["tp"] == 1  # only doc1's (5,8) hit
    assert res["PERSON"]["fp"] == 1  # doc2's (5,7) ≠ (5,9)
    assert res["PERSON"]["fn"] == 1


def test_pick_recommended_picks_best_f1_above_floor():
    # Synthetic sweep: t=0.5 has recall 0.95 F1 0.6; t=0.7 recall 0.92 F1 0.7;
    # t=0.95 recall 0.5 F1 0.65. Floor 0.90 -> t=0.7 wins (highest F1).
    sweep = {
        0.5: {"_overall": {"p": 0.45, "r": 0.95, "f1": 0.6, "tp": 0, "fp": 0, "fn": 0,
                            "n_pred": 0, "n_gold": 0}},
        0.7: {"_overall": {"p": 0.6, "r": 0.92, "f1": 0.7, "tp": 0, "fp": 0, "fn": 0,
                            "n_pred": 0, "n_gold": 0}},
        0.95: {"_overall": {"p": 0.9, "r": 0.5, "f1": 0.65, "tp": 0, "fp": 0, "fn": 0,
                             "n_pred": 0, "n_gold": 0}},
    }
    rec, met = pick_recommended_threshold(sweep, recall_floor=0.9)
    assert rec == 0.7
    assert met is True


def test_pick_recommended_falls_back_when_floor_unmet():
    sweep = {
        0.5: {"_overall": {"p": 0.6, "r": 0.5, "f1": 0.55, "tp": 0, "fp": 0, "fn": 0,
                            "n_pred": 0, "n_gold": 0}},
        0.7: {"_overall": {"p": 0.7, "r": 0.4, "f1": 0.51, "tp": 0, "fp": 0, "fn": 0,
                            "n_pred": 0, "n_gold": 0}},
    }
    rec, met = pick_recommended_threshold(sweep, recall_floor=0.9)
    assert met is False
    assert rec == 0.5  # higher-recall fallback


def test_pick_recommended_tiebreak_prefers_higher_threshold():
    sweep = {
        0.5: {"_overall": {"p": 0.5, "r": 0.95, "f1": 0.7, "tp": 0, "fp": 0, "fn": 0,
                            "n_pred": 0, "n_gold": 0}},
        0.7: {"_overall": {"p": 0.7, "r": 0.92, "f1": 0.7, "tp": 0, "fp": 0, "fn": 0,
                            "n_pred": 0, "n_gold": 0}},
    }
    rec, met = pick_recommended_threshold(sweep, recall_floor=0.9)
    assert rec == 0.7
    assert met is True


def test_run_sweep_default_grid_returns_all_thresholds():
    docs = _docs()
    sweep = run_sweep(docs, DEFAULT_THRESHOLDS, ["ORGANIZATION", "PERSON"])
    assert set(sweep.keys()) == {float(t) for t in DEFAULT_THRESHOLDS}
    for t, r in sweep.items():
        assert "_overall" in r
        assert "ORGANIZATION" in r
        assert "PERSON" in r


def test_render_markdown_contains_recommendation_and_per_label_section():
    docs = _docs()
    labels = ["ORGANIZATION", "PERSON"]
    sweep = run_sweep(docs, DEFAULT_THRESHOLDS, labels)
    rec, met = pick_recommended_threshold(sweep, 0.5)
    md = render_markdown(sweep, labels, rec, met, 0.5)
    assert "# Confidence threshold sweep" in md
    assert "## Per-threshold overall" in md
    assert "## Per-threshold ORGANIZATION" in md
    assert "## Per-threshold PERSON" in md
    assert "## Recommendation" in md
    assert f"threshold = {rec:.2f}" in md


def test_evaluate_per_label_threshold_filters_only_targeted_label():
    """#98: ORG-only floor must not filter PERSON predictions."""
    docs = _docs()
    # ORG floor 0.9 drops doc1's (9,12) FP and doc2's (0,3) TP.
    # PERSON floor 0.0 keeps both PERSON preds.
    res = evaluate_at_threshold(
        docs,
        threshold={"ORGANIZATION": 0.9, "PERSON": 0.0},
        labels=["ORGANIZATION", "PERSON"],
    )
    # ORG: only doc1's (0,3) survives (score 0.95). gold=2 -> tp=1, fp=0, fn=1.
    assert res["ORGANIZATION"]["tp"] == 1
    assert res["ORGANIZATION"]["fp"] == 0
    assert res["ORGANIZATION"]["fn"] == 1
    # PERSON unchanged from threshold=0: tp=1, fp=1, fn=1.
    assert res["PERSON"]["tp"] == 1
    assert res["PERSON"]["fp"] == 1
    assert res["PERSON"]["fn"] == 1


def test_run_per_label_sweep_holds_other_labels_constant():
    docs = _docs()
    sweep = run_per_label_sweep(
        docs,
        sweep_label="ORGANIZATION",
        thresholds=[0.0, 0.6, 0.9],
        labels=["ORGANIZATION", "PERSON"],
    )
    # PERSON tp/fp/fn should be identical across all sweep points (label not swept).
    person_sigs = {
        (r["PERSON"]["tp"], r["PERSON"]["fp"], r["PERSON"]["fn"]) for r in sweep.values()
    }
    assert len(person_sigs) == 1
    # ORG should monotonically lose true positives as threshold rises.
    org_tps = [sweep[t]["ORGANIZATION"]["tp"] for t in sorted(sweep.keys())]
    assert org_tps == sorted(org_tps, reverse=True)


def test_write_csv_emits_one_row_per_threshold_label(tmp_path: Path):
    docs = _docs()
    labels = ["ORGANIZATION", "PERSON"]
    sweep = run_sweep(docs, [0.3, 0.7], labels)
    out = tmp_path / "sweep.csv"
    write_csv(sweep, labels, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    # 2 thresholds × (2 labels + _overall) = 6 rows.
    assert len(rows) == 6
    assert {r["label"] for r in rows} == {"ORGANIZATION", "PERSON", "_overall"}
    assert {r["threshold"] for r in rows} == {"0.3000", "0.7000"}
