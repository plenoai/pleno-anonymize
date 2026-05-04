"""Confidence-threshold sweep over scored predictions (issue #68).

Reads a `raw_with_scores.json` produced by `predict_hf_with_scores.py`, applies
each threshold in `--thresholds`, and computes per-label and overall
precision / recall / F1 against the gold spans in the same file.

A prediction span survives threshold `t` iff its `score >= t`.

Matching is **strict**: identical (start, end, label) tuples count as TP.
Matches `evaluate_benchmark.py`'s spaCy `Scorer` semantics for ents-level
metrics under exact-span scoring.

Recommended threshold = arg-max F1 among thresholds whose recall ≥ 0.90 (issue
#68 AC). If no threshold meets recall ≥ 0.90, falls back to the highest-recall
threshold (and flags it in the report).

Outputs:
- CSV: threshold, label, p, r, f1, n_pred, n_gold, n_tp
- Markdown report (per-threshold per-label table + overall + recommendation)
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Sequence

# Default sweep grid from the issue.
DEFAULT_THRESHOLDS = (0.3, 0.5, 0.7, 0.85, 0.95)
RECALL_FLOOR = 0.90


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f1


def _label_threshold(threshold: float | dict[str, float], label: str) -> float:
    """Resolve the threshold to apply for `label`.

    `threshold` may be a single float (uniform) or a per-label mapping. Labels
    missing from the mapping default to 0.0 (= keep all predictions). This lets
    callers pass `{"ORGANIZATION": 0.85}` to filter ORG only.
    """
    if isinstance(threshold, dict):
        return float(threshold.get(label, 0.0))
    return float(threshold)


def evaluate_at_threshold(
    docs: Sequence[dict[str, Any]],
    threshold: float | dict[str, float],
    labels: Sequence[str],
) -> dict[str, dict[str, float | int]]:
    """Return {label: {p, r, f1, tp, fp, fn, n_pred, n_gold}, "_overall": {...}}.

    Pure function; the heavy lifting in this module. `docs` follow the
    raw_with_scores.json schema. `threshold` may be a uniform float or a
    per-label dict (#98: ORG-only confidence floor).
    """
    per_label: dict[str, dict[str, float | int]] = {
        lbl: {"tp": 0, "fp": 0, "fn": 0, "n_pred": 0, "n_gold": 0} for lbl in labels
    }
    total_tp = 0
    total_fp = 0
    total_fn = 0

    for doc in docs:
        gold_set: set[tuple[int, int, str]] = {
            (int(g["start"]), int(g["end"]), str(g["label"]))
            for g in doc.get("entities", [])
            if str(g.get("label")) in labels
        }
        pred_set: set[tuple[int, int, str]] = {
            (int(p["start"]), int(p["end"]), str(p["label"]))
            for p in doc.get("predictions", [])
            if str(p.get("label")) in labels
            and float(p.get("score", 0.0)) >= _label_threshold(threshold, str(p["label"]))
        }

        for lbl in labels:
            gold_l = {(s, e, lab) for (s, e, lab) in gold_set if lab == lbl}
            pred_l = {(s, e, lab) for (s, e, lab) in pred_set if lab == lbl}
            tp = len(gold_l & pred_l)
            fp = len(pred_l - gold_l)
            fn = len(gold_l - pred_l)
            per_label[lbl]["tp"] += tp
            per_label[lbl]["fp"] += fp
            per_label[lbl]["fn"] += fn
            per_label[lbl]["n_pred"] += len(pred_l)
            per_label[lbl]["n_gold"] += len(gold_l)
            total_tp += tp
            total_fp += fp
            total_fn += fn

    out: dict[str, dict[str, float | int]] = {}
    for lbl, c in per_label.items():
        p, r, f1 = _prf(int(c["tp"]), int(c["fp"]), int(c["fn"]))
        out[lbl] = {**c, "p": p, "r": r, "f1": f1}

    p, r, f1 = _prf(total_tp, total_fp, total_fn)
    out["_overall"] = {
        "tp": total_tp,
        "fp": total_fp,
        "fn": total_fn,
        "n_pred": total_tp + total_fp,
        "n_gold": total_tp + total_fn,
        "p": p,
        "r": r,
        "f1": f1,
    }
    return out


def pick_recommended_threshold(
    sweep: dict[float, dict[str, dict[str, float | int]]],
    recall_floor: float = RECALL_FLOOR,
) -> tuple[float, bool]:
    """Best F1 at overall recall >= floor. Returns (threshold, met_floor).

    Falls back to the threshold with highest recall if none meet the floor.
    Tie-breaks (equal F1): prefer higher threshold (= more conservative, better
    precision); the issue's "post-process precision lever" framing favors the
    stricter setting when results tie.
    """
    eligible = [
        (t, r["_overall"]["f1"])
        for t, r in sweep.items()
        if float(r["_overall"]["r"]) >= recall_floor
    ]
    if eligible:
        eligible.sort(key=lambda x: (-float(x[1]), -x[0]))
        return eligible[0][0], True
    # Fallback: highest overall recall, then highest threshold.
    fallback = sorted(
        sweep.items(),
        key=lambda kv: (-float(kv[1]["_overall"]["r"]), -kv[0]),
    )
    return fallback[0][0], False


def render_markdown(
    sweep: dict[float, dict[str, dict[str, float | int]]],
    labels: Sequence[str],
    recommended: float,
    met_floor: bool,
    recall_floor: float,
    source_path: Path | None = None,
) -> str:
    """Markdown table per the issue's "CSV / Markdown で threshold × P/R/F1 表"."""
    lines: list[str] = []
    lines.append("# Confidence threshold sweep (#68)\n")
    if source_path is not None:
        lines.append(f"Source: `{source_path}`\n")
    lines.append("## Per-threshold overall\n")
    lines.append("| threshold | precision | recall | F1 | n_pred | n_gold |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    for t in sorted(sweep.keys()):
        ov = sweep[t]["_overall"]
        lines.append(
            f"| {t:.2f} | {float(ov['p']):.3f} | {float(ov['r']):.3f} | "
            f"{float(ov['f1']):.3f} | {int(ov['n_pred'])} | {int(ov['n_gold'])} |"
        )

    for lbl in labels:
        lines.append(f"\n## Per-threshold {lbl}\n")
        lines.append("| threshold | precision | recall | F1 | n_pred | n_gold |")
        lines.append("|---:|---:|---:|---:|---:|---:|")
        for t in sorted(sweep.keys()):
            row = sweep[t][lbl]
            lines.append(
                f"| {t:.2f} | {float(row['p']):.3f} | {float(row['r']):.3f} | "
                f"{float(row['f1']):.3f} | {int(row['n_pred'])} | {int(row['n_gold'])} |"
            )

    lines.append("\n## Recommendation\n")
    rec_overall = sweep[recommended]["_overall"]
    if met_floor:
        lines.append(
            f"**threshold = {recommended:.2f}** — best F1 among thresholds with "
            f"recall ≥ {recall_floor:.2f}.\n"
        )
    else:
        lines.append(
            f"**threshold = {recommended:.2f}** — *fallback* (no threshold met "
            f"recall ≥ {recall_floor:.2f}; picked highest-recall instead).\n"
        )
    lines.append(
        f"At threshold {recommended:.2f}: "
        f"P={float(rec_overall['p']):.3f}, "
        f"R={float(rec_overall['r']):.3f}, "
        f"F1={float(rec_overall['f1']):.3f}.\n"
    )
    return "\n".join(lines) + "\n"


def write_csv(
    sweep: dict[float, dict[str, dict[str, float | int]]],
    labels: Sequence[str],
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["threshold", "label", "precision", "recall", "f1", "n_pred", "n_gold", "tp"]
        )
        for t in sorted(sweep.keys()):
            for lbl in [*labels, "_overall"]:
                row = sweep[t][lbl]
                w.writerow(
                    [
                        f"{t:.4f}",
                        lbl,
                        f"{float(row['p']):.6f}",
                        f"{float(row['r']):.6f}",
                        f"{float(row['f1']):.6f}",
                        int(row["n_pred"]),
                        int(row["n_gold"]),
                        int(row["tp"]),
                    ]
                )


def run_sweep(
    docs: Sequence[dict[str, Any]],
    thresholds: Sequence[float],
    labels: Sequence[str],
) -> dict[float, dict[str, dict[str, float | int]]]:
    return {float(t): evaluate_at_threshold(docs, float(t), labels) for t in thresholds}


def run_per_label_sweep(
    docs: Sequence[dict[str, Any]],
    sweep_label: str,
    thresholds: Sequence[float],
    labels: Sequence[str],
    base_thresholds: dict[str, float] | None = None,
) -> dict[float, dict[str, dict[str, float | int]]]:
    """Sweep `sweep_label`'s threshold while holding others at `base_thresholds`.

    #98: ORG precision is the sole bottleneck; PERSON / ADDRESS / DOB / BANK are
    well-calibrated at the default (0.0) threshold. Sweeping a per-label floor
    lets us recover overall precision without sacrificing well-tuned recall on
    the other labels.
    """
    base = dict(base_thresholds or {})
    out: dict[float, dict[str, dict[str, float | int]]] = {}
    for t in thresholds:
        per_label = {**base, sweep_label: float(t)}
        out[float(t)] = evaluate_at_threshold(docs, per_label, labels)
    return out


def _infer_labels(docs: Sequence[dict[str, Any]]) -> list[str]:
    """Discover the label set used in this benchmark (gold ∪ predictions)."""
    seen: set[str] = set()
    for d in docs:
        for ent in d.get("entities", []):
            seen.add(str(ent["label"]))
        for ent in d.get("predictions", []):
            seen.add(str(ent["label"]))
    # Stable, human-friendly order: the canonical 5-class set first, then
    # anything else discovered.
    canonical = ["PERSON", "ADDRESS", "ORGANIZATION", "DATE_OF_BIRTH", "BANK_ACCOUNT"]
    ordered = [lbl for lbl in canonical if lbl in seen]
    ordered += sorted(seen - set(canonical))
    return ordered


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Confidence threshold sweep over HF NER predictions (#68)"
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        required=True,
        help="raw_with_scores.json from predict_hf_with_scores.py",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        required=True,
        help="Markdown report path",
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=None,
        help="Optional CSV path",
    )
    parser.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=list(DEFAULT_THRESHOLDS),
    )
    parser.add_argument(
        "--recall-floor",
        type=float,
        default=RECALL_FLOOR,
    )
    parser.add_argument(
        "--sweep-label",
        type=str,
        default=None,
        help=(
            "If set, sweep this label's threshold only and hold others at the "
            "values from --base-threshold (or 0.0). #98 ORG-only precision floor."
        ),
    )
    parser.add_argument(
        "--base-threshold",
        action="append",
        default=[],
        metavar="LABEL=VALUE",
        help=(
            "Per-label baseline threshold while sweeping. Repeatable; e.g. "
            "--base-threshold PERSON=0.0 --base-threshold ADDRESS=0.0. "
            "Only used with --sweep-label."
        ),
    )
    args = parser.parse_args()

    with open(args.predictions, encoding="utf-8") as f:
        docs = json.load(f)
    if not isinstance(docs, list):
        raise SystemExit(f"Expected list at {args.predictions}")
    print(f"Loaded {len(docs)} scored docs")

    labels = _infer_labels(docs)
    print(f"Labels: {labels}")
    if args.sweep_label:
        if args.sweep_label not in labels:
            raise SystemExit(
                f"--sweep-label {args.sweep_label!r} not present in dataset labels {labels}"
            )
        base: dict[str, float] = {}
        for kv in args.base_threshold:
            if "=" not in kv:
                raise SystemExit(f"--base-threshold expects LABEL=VALUE; got {kv!r}")
            k, v = kv.split("=", 1)
            base[k.strip()] = float(v)
        print(f"Sweeping {args.sweep_label} only; base = {base}")
        sweep = run_per_label_sweep(
            docs, args.sweep_label, args.thresholds, labels, base_thresholds=base
        )
    else:
        sweep = run_sweep(docs, args.thresholds, labels)
    recommended, met_floor = pick_recommended_threshold(sweep, args.recall_floor)

    md = render_markdown(
        sweep, labels, recommended, met_floor, args.recall_floor, args.predictions
    )
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(md, encoding="utf-8")
    print(f"Wrote markdown report → {args.output_md}")

    if args.output_csv is not None:
        write_csv(sweep, labels, args.output_csv)
        print(f"Wrote CSV → {args.output_csv}")

    rec = sweep[recommended]["_overall"]
    print(
        f"Recommended threshold: {recommended:.2f} "
        f"(P={float(rec['p']):.3f}, R={float(rec['r']):.3f}, F1={float(rec['f1']):.3f}, "
        f"recall_floor_met={met_floor})"
    )


if __name__ == "__main__":
    main()
