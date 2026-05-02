"""Tests for pleno_ner_training.metrics — pure statistical primitives.

Coverage targets per plan U2: happy paths, edge cases, error paths,
AE-style verdict scenarios. Uses numpy only (no scipy.stats.bootstrap).
"""

from __future__ import annotations

import numpy as np
import pytest

from pleno_ner_training.metrics import (
    bonferroni_correct,
    bootstrap_ci,
    compute_verdict,
    matched_precision_budget_recall,
    p10,
    per_template_recall,
    sort_by_score_then_id,
    strict_span_f1,
    token_overlap_f1,
)


# ----- bootstrap_ci -----

def test_bootstrap_ci_normal_contains_zero():
    rng = np.random.default_rng(123)
    samples = rng.standard_normal(100).tolist()
    lo, hi, mean = bootstrap_ci(samples, n=1000, alpha=0.05, seed=42)
    assert lo < 0.0 < hi
    assert lo < mean < hi


def test_bootstrap_ci_deterministic_seed():
    samples = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    a = bootstrap_ci(samples, n=500, seed=42)
    b = bootstrap_ci(samples, n=500, seed=42)
    assert a == b


def test_bootstrap_ci_different_seed_differs():
    samples = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    a = bootstrap_ci(samples, n=500, seed=1)
    b = bootstrap_ci(samples, n=500, seed=2)
    assert a != b


def test_bootstrap_ci_returns_mean():
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    _lo, _hi, mean = bootstrap_ci(samples, n=200, seed=7)
    assert mean == pytest.approx(3.0)


def test_bootstrap_ci_empty_raises():
    with pytest.raises(ValueError):
        bootstrap_ci([], n=100, seed=42)


# ----- matched_precision_budget_recall -----

def test_matched_precision_budget_recall_basic():
    gold = [(0, 5, "ORG"), (10, 15, "ORG"), (20, 25, "ORG"), (30, 35, "ORG")]
    predictions = [
        (0, 5, "ORG", 0.95, 0),     # TP
        (10, 15, "ORG", 0.90, 1),   # TP
        (20, 25, "ORG", 0.80, 2),   # TP
        (40, 45, "ORG", 0.60, 3),   # FP — drops precision to 3/4 = 0.75
        (30, 35, "ORG", 0.50, 4),   # TP — recall 4/4=1.0, precision 4/5=0.8
    ]
    recall = matched_precision_budget_recall(predictions, gold, "ORG", p_budget=0.7)
    assert recall == pytest.approx(1.0)


def test_matched_precision_budget_recall_budget_violated_early():
    gold = [(0, 5, "ORG"), (10, 15, "ORG")]
    predictions = [
        (100, 105, "ORG", 0.99, 0),  # FP — precision 0/1
        (200, 205, "ORG", 0.98, 1),  # FP — precision 0/2
        (0, 5, "ORG", 0.50, 2),      # TP — precision 1/3 < 0.7
    ]
    recall = matched_precision_budget_recall(predictions, gold, "ORG", p_budget=0.7)
    assert recall == 0.0


def test_matched_precision_budget_recall_label_filter():
    gold = [(0, 5, "ORG")]
    predictions = [(0, 5, "DOB", 0.99, 0), (0, 5, "ORG", 0.50, 1)]
    recall = matched_precision_budget_recall(predictions, gold, "ORG", p_budget=0.5)
    assert recall == pytest.approx(1.0)


def test_matched_precision_budget_recall_empty_gold():
    assert matched_precision_budget_recall([], [], "ORG") == 0.0


def test_matched_precision_budget_recall_score_none_pushed_to_tail():
    gold = [(0, 5, "ORG")]
    predictions = [
        (100, 105, "ORG", None, 0),  # score=None → tail
        (0, 5, "ORG", 0.50, 1),
    ]
    recall = matched_precision_budget_recall(predictions, gold, "ORG", p_budget=1.0)
    assert recall == pytest.approx(1.0)


# ----- token_overlap_f1 -----

def test_token_overlap_iou_boundary_matches():
    pred = [(0, 10, "ORG")]
    gold = [(3, 8, "ORG")]
    p, r, f = token_overlap_f1(pred, gold, iou_threshold=0.5)
    assert p == 1.0 and r == 1.0 and f == 1.0


def test_token_overlap_label_mismatch_no_match():
    pred = [(0, 10, "ORG")]
    gold = [(0, 10, "DOB")]
    p, r, f = token_overlap_f1(pred, gold, iou_threshold=0.5)
    assert (p, r, f) == (0.0, 0.0, 0.0)


def test_token_overlap_empty_predictions():
    p, r, f = token_overlap_f1([], [(0, 5, "ORG")])
    assert (p, r, f) == (0.0, 0.0, 0.0)


def test_token_overlap_empty_both():
    assert token_overlap_f1([], []) == (0.0, 0.0, 0.0)


def test_token_overlap_below_threshold_no_match():
    # IoU = 1/10 = 0.1, below 0.5 threshold
    pred = [(0, 10, "ORG")]
    gold = [(9, 11, "ORG")]
    p, r, f = token_overlap_f1(pred, gold, iou_threshold=0.5)
    assert (p, r, f) == (0.0, 0.0, 0.0)


def test_token_overlap_greedy_one_to_one():
    pred = [(0, 10, "ORG"), (1, 11, "ORG")]
    gold = [(0, 10, "ORG")]
    p, r, f = token_overlap_f1(pred, gold, iou_threshold=0.5)
    # one TP, one FP
    assert p == pytest.approx(0.5)
    assert r == pytest.approx(1.0)
    assert f == pytest.approx(2 * 0.5 * 1.0 / 1.5)


# ----- strict_span_f1 -----

def test_strict_span_exact_match():
    pred = [(0, 5, "ORG")]
    gold = [(0, 5, "ORG")]
    assert strict_span_f1(pred, gold) == (1.0, 1.0, 1.0)


def test_strict_span_overlapping_not_identical_no_match():
    pred = [(0, 5, "ORG")]
    gold = [(1, 5, "ORG")]
    p, r, f = strict_span_f1(pred, gold)
    assert (p, r, f) == (0.0, 0.0, 0.0)


def test_strict_span_label_mismatch_no_match():
    pred = [(0, 5, "ORG")]
    gold = [(0, 5, "DOB")]
    assert strict_span_f1(pred, gold) == (0.0, 0.0, 0.0)


def test_strict_span_empty():
    assert strict_span_f1([], []) == (0.0, 0.0, 0.0)


# ----- per_template_recall -----

def test_per_template_recall_excludes_below_min_spans():
    preds = {
        "t1": [(0, 5, "ORG", 0.9, 0), (10, 15, "ORG", 0.8, 1)],
        "t2": [(0, 5, "ORG", 0.9, 0)],
    }
    gold = {
        "t1": [(0, 5, "ORG"), (10, 15, "ORG"), (20, 25, "ORG")],
        "t2": [(0, 5, "ORG")],
    }
    out = per_template_recall(preds, gold, "ORG", min_spans=3)
    assert out["t1"] == pytest.approx(2 / 3)
    assert out["t2"] is None


def test_per_template_recall_all_below_min():
    preds = {"t1": [(0, 5, "ORG", 0.9, 0)]}
    gold = {"t1": [(0, 5, "ORG")]}
    out = per_template_recall(preds, gold, "ORG", min_spans=5)
    assert out == {"t1": None}


def test_per_template_recall_missing_predictions_template():
    gold = {"t1": [(0, 5, "ORG"), (10, 15, "ORG"), (20, 25, "ORG")]}
    out = per_template_recall({}, gold, "ORG", min_spans=3)
    assert out["t1"] == 0.0


# ----- p10 -----

def test_p10_all_none_returns_none():
    assert p10([None, None, None]) is None


def test_p10_empty_returns_none():
    assert p10([]) is None


def test_p10_mixed_ignores_none():
    # Values 0..9; np.quantile linear at 0.10 → 0.9
    vals: list[float | None] = [None, *[float(i) for i in range(10)]]
    assert p10(vals) == pytest.approx(0.9)


# ----- bonferroni_correct -----

def test_bonferroni_correct_m1_unchanged():
    assert bonferroni_correct(0.04, 1) == pytest.approx(0.04)


def test_bonferroni_correct_clamps_to_1():
    assert bonferroni_correct(0.5, 10) == 1.0


def test_bonferroni_correct_typical():
    assert bonferroni_correct(0.001, 35) == pytest.approx(0.035)


def test_bonferroni_correct_invalid_m():
    with pytest.raises(ValueError):
        bonferroni_correct(0.5, 0)


# ----- sort_by_score_then_id -----

def test_sort_by_score_descending():
    preds = [
        (0, 5, "ORG", 0.5, 0),
        (10, 15, "ORG", 0.9, 1),
        (20, 25, "ORG", 0.7, 2),
    ]
    out = sort_by_score_then_id(preds)
    assert [p[3] for p in out] == [0.9, 0.7, 0.5]


def test_sort_by_score_then_doc_id_then_start():
    preds = [
        (5, 10, "ORG", 0.8, 0),
        (0, 5, "ORG", 0.8, 1),
    ]
    out = sort_by_score_then_id(preds, doc_ids=["b", "a"])
    # doc_id "a" < "b" → second pred first
    assert out[0] == (0, 5, "ORG", 0.8, 1)
    assert out[1] == (5, 10, "ORG", 0.8, 0)


def test_sort_score_none_treated_as_zero():
    preds = [
        (0, 5, "ORG", None, 0),
        (10, 15, "ORG", 0.5, 1),
        (20, 25, "ORG", -0.1, 2),
    ]
    out = sort_by_score_then_id(preds)
    assert out[0][3] == 0.5
    assert out[1][3] is None  # 0.0 effective
    assert out[2][3] == -0.1


def test_sort_deterministic_repeated_calls():
    preds = [
        (0, 5, "ORG", 0.5, 0),
        (10, 15, "ORG", 0.5, 1),
        (20, 25, "ORG", 0.5, 2),
    ]
    a = sort_by_score_then_id(preds, doc_ids=["d1", "d2", "d3"])
    b = sort_by_score_then_id(preds, doc_ids=["d1", "d2", "d3"])
    assert a == b


def test_sort_doc_ids_length_mismatch_raises():
    preds = [(0, 5, "ORG", 0.5, 0)]
    with pytest.raises(ValueError):
        sort_by_score_then_id(preds, doc_ids=["a", "b"])


# ----- compute_verdict -----

ALL_TRUE = {
    "r7_primary_gate": True,
    "r8a_min_span_filter": True,
    "r8b_p10_robust": True,
    "r8c_dual_metric_agree": True,
}


def test_verdict_eligibility_guard_blocks_kill():
    assert compute_verdict(ALL_TRUE, "oss_better", (0.05, 0.10), n_eligible_templates=3) == "NO_DECISION"


def test_verdict_oss_better_ci_above_zero_kills():
    assert compute_verdict(ALL_TRUE, "oss_better", (0.05, 0.10), n_eligible_templates=10) == "KILL"


def test_verdict_custom_better_commits():
    assert compute_verdict(ALL_TRUE, "custom_better", (-0.10, -0.05), n_eligible_templates=10) == "COMMIT"


def test_verdict_ci_straddles_zero_commits():
    # diff_sign oss_better but CI straddles 0 → COMMIT (per plan KTD)
    assert compute_verdict(ALL_TRUE, "oss_better", (-0.02, 0.05), n_eligible_templates=10) == "COMMIT"


def test_verdict_one_gate_false_no_decision():
    gates = {**ALL_TRUE, "r7_primary_gate": False}
    assert compute_verdict(gates, "oss_better", (0.05, 0.10), n_eligible_templates=10) == "NO_DECISION"


def test_verdict_tied_no_decision():
    # tied + CI not straddling 0, all gates true → falls through to NO_DECISION
    assert compute_verdict(ALL_TRUE, "tied", (0.01, 0.02), n_eligible_templates=10) == "NO_DECISION"


def test_verdict_oss_better_ci_lo_zero_no_kill():
    # lo > 0 strict; lo == 0 must not kill
    out = compute_verdict(ALL_TRUE, "oss_better", (0.0, 0.05), n_eligible_templates=10)
    # ci straddles 0 (lo<=0<=hi) → COMMIT branch is also taken, but only when
    # diff_sign is custom_better OR straddles. oss_better with lo==0 → falls
    # through (lo <= 0 <= hi true) → COMMIT.
    assert out == "COMMIT"
