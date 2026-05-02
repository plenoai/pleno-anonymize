"""Statistical primitives for the GiNZA+Presidio honest baseline measurement.

Pure functions only. No I/O, no global state. Frozen scope under the plan's
Pre-Registration anchor SHA — see plan U2 / Frozen scope (1).
"""

from __future__ import annotations

from typing import Iterable, Literal, Sequence

import numpy as np

Span = tuple[int, int, str]
ScoredSpan = tuple[int, int, str, float | None, int]

Verdict = Literal["KILL", "COMMIT", "NO_DECISION"]
DiffSign = Literal["oss_better", "custom_better", "tied"]


def bootstrap_ci(
    samples: Sequence[float],
    n: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float]:
    """Percentile bootstrap CI of the mean. Returns (lo, hi, mean)."""
    if len(samples) == 0:
        raise ValueError("bootstrap_ci requires at least one sample")
    arr = np.asarray(samples, dtype=float)
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, len(arr), size=(n, len(arr)))
    means = arr[indices].mean(axis=1)
    lo = float(np.quantile(means, alpha / 2.0))
    hi = float(np.quantile(means, 1.0 - alpha / 2.0))
    return lo, hi, float(arr.mean())


def _iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    if inter == 0:
        return 0.0
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union > 0 else 0.0


def matched_precision_budget_recall(
    predictions: Sequence[ScoredSpan],
    gold: Sequence[Span],
    label: str,
    p_budget: float = 0.7,
) -> float:
    """Recall at the deepest operating point where running precision >= p_budget.

    Predictions are sorted by score desc with score=None pushed to the tail
    (deterministic by rank, then doc-order). Walks the sorted list, tracks
    cumulative TP/FP against gold spans of `label` (exact-span match, label-aware),
    and returns the recall observed at the lowest-score row whose cumulative
    precision still meets `p_budget`. Returns 0.0 if no such row exists.
    """
    gold_set = {(s, e) for s, e, lbl in gold if lbl == label}
    n_gold = len(gold_set)
    label_preds = [p for p in predictions if p[2] == label]
    if not label_preds or n_gold == 0:
        return 0.0
    ordered = sorted(
        label_preds,
        key=lambda p: (-(p[3] if p[3] is not None else float("-inf")), p[4]),
    )
    matched: set[tuple[int, int]] = set()
    tp = 0
    fp = 0
    best_recall = 0.0
    for start, end, _lbl, _score, _rank in ordered:
        if (start, end) in gold_set and (start, end) not in matched:
            tp += 1
            matched.add((start, end))
        else:
            fp += 1
        precision = tp / (tp + fp)
        if precision >= p_budget:
            best_recall = max(best_recall, tp / n_gold)
    return best_recall


def token_overlap_f1(
    pred_spans: Sequence[Span],
    gold_spans: Sequence[Span],
    iou_threshold: float = 0.5,
) -> tuple[float, float, float]:
    """Character-level IoU >= threshold AND label match. Greedy bipartite by IoU."""
    if not pred_spans and not gold_spans:
        return 0.0, 0.0, 0.0
    if not pred_spans or not gold_spans:
        return 0.0, 0.0, 0.0
    pairs: list[tuple[float, int, int]] = []
    for i, (ps, pe, plbl) in enumerate(pred_spans):
        for j, (gs, ge, glbl) in enumerate(gold_spans):
            if plbl != glbl:
                continue
            iou = _iou((ps, pe), (gs, ge))
            if iou >= iou_threshold:
                pairs.append((iou, i, j))
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    used_pred: set[int] = set()
    used_gold: set[int] = set()
    tp = 0
    for _iou_val, i, j in pairs:
        if i in used_pred or j in used_gold:
            continue
        used_pred.add(i)
        used_gold.add(j)
        tp += 1
    fp = len(pred_spans) - tp
    fn = len(gold_spans) - tp
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def strict_span_f1(
    pred_spans: Sequence[Span],
    gold_spans: Sequence[Span],
) -> tuple[float, float, float]:
    """Exact (start, end, label) match."""
    pred_set = set(pred_spans)
    gold_set = set(gold_spans)
    tp = len(pred_set & gold_set)
    fp = len(pred_set - gold_set)
    fn = len(gold_set - pred_set)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def per_template_recall(
    predictions_by_template: dict[str, Sequence[ScoredSpan]],
    gold_by_template: dict[str, Sequence[Span]],
    label: str,
    min_spans: int,
) -> dict[str, float | None]:
    """Per-template recall on `label`. Templates with < min_spans gold of
    that label return None (excluded by R8(a) eligibility filter)."""
    result: dict[str, float | None] = {}
    for template, gold in gold_by_template.items():
        gold_label = [g for g in gold if g[2] == label]
        if len(gold_label) < min_spans:
            result[template] = None
            continue
        gold_set = {(s, e) for s, e, _ in gold_label}
        preds = predictions_by_template.get(template, ())
        pred_set = {(s, e) for s, e, lbl, _sc, _rk in preds if lbl == label}
        tp = len(gold_set & pred_set)
        result[template] = tp / len(gold_set)
    return result


def p10(values: Iterable[float | None]) -> float | None:
    """10th percentile of non-None values; None if all values are None / empty."""
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return float(np.quantile(np.asarray(cleaned, dtype=float), 0.10))


def bonferroni_correct(p_value: float, m: int) -> float:
    """min(p_value * m, 1.0). For CI, the caller adjusts alpha to alpha/m."""
    if m < 1:
        raise ValueError("m must be >= 1")
    return min(p_value * m, 1.0)


def sort_by_score_then_id(
    predictions: Sequence[ScoredSpan],
    *,
    doc_ids: Sequence[str] | None = None,
) -> list[ScoredSpan]:
    """Lexicographic: (score_or_zero desc, doc_id asc, span_start asc).
    score=None is treated as 0.0 for tie-break determinism."""
    if doc_ids is not None and len(doc_ids) != len(predictions):
        raise ValueError("doc_ids length must match predictions length")
    indexed = list(enumerate(predictions))

    def key(item: tuple[int, ScoredSpan]) -> tuple[float, str, int]:
        idx, (start, _end, _lbl, score, _rank) = item
        score_val = score if score is not None else 0.0
        doc_id = doc_ids[idx] if doc_ids is not None else ""
        return (-score_val, doc_id, start)

    return [pred for _idx, pred in sorted(indexed, key=key)]


def compute_verdict(
    gates: dict[str, bool],
    diff_sign: DiffSign,
    diff_ci: tuple[float, float],
    n_eligible_templates: int,
) -> Verdict:
    """Plan KTD verdict mapping. P0-1 eligibility guard + P1-4 mapping."""
    if n_eligible_templates < 4:
        return "NO_DECISION"
    if not all(gates.values()):
        return "NO_DECISION"
    lo, hi = diff_ci
    if diff_sign == "oss_better" and lo > 0:
        return "KILL"
    if diff_sign == "custom_better" or (lo <= 0 <= hi):
        return "COMMIT"
    return "NO_DECISION"
