"""ORG-only confidence threshold sweep against the spaCy Scorer (#98).

Why this exists separately from `sweep_threshold.py`:
- `sweep_threshold` compares (start,end,label) tuples in char space. That
  systematically under-counts TPs vs spaCy `Scorer`, which token-aligns spans
  via `char_span(alignment_mode="expand")` before strict matching. The two
  paths disagree by ~0.10 F1 on PERSON for hf_v02_tiny_aug_ext.
- For #98 we need numbers comparable to scores.json (0.452 overall F1 baseline)
  to evaluate AC = "ORG precision ≥ 0.30 AND overall F1 ≥ 0.500".

This module sweeps a single label's threshold and re-scores via
`evaluate_scored_predictions_on_benchmark`, holding all other labels at 0.0.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pleno_ner_training.benchmark_config import (
    BENCHMARK_CONFIGS,
    BENCHMARK_VERSIONS,
    LATEST_BENCHMARK_VERSION,
)
from pleno_ner_training.evaluate_benchmark import (
    evaluate_scored_predictions_on_benchmark,
)


def render_markdown(
    sweep: dict[float, dict],
    sweep_label: str,
    source_path: Path,
    benchmark_path: Path,
) -> str:
    lines: list[str] = []
    lines.append(f"# {sweep_label}-only confidence threshold sweep (#98)\n")
    lines.append(f"Source predictions: `{source_path}`\n")
    lines.append(f"Benchmark gold: `{benchmark_path}`\n")
    lines.append("Scoring path: spaCy `Scorer` strict-span (matches scores.json).\n")

    lines.append("\n## Overall\n")
    lines.append("| threshold | precision | recall | F1 | neg-clean rate | neg FP |")
    lines.append("|---:|---:|---:|---:|---:|---:|")
    for t in sorted(sweep.keys()):
        s = sweep[t]
        lines.append(
            f"| {t:.2f} | {s['ents_p']:.3f} | {s['ents_r']:.3f} | {s['ents_f']:.3f} | "
            f"{s['negative_doc_clean_rate']:.3f} | {s['negative_fp_total']} |"
        )

    labels = ["PERSON", "ADDRESS", "ORGANIZATION", "DATE_OF_BIRTH", "BANK_ACCOUNT"]
    for label in labels:
        lines.append(f"\n## {label}\n")
        lines.append("| threshold | precision | recall | F1 |")
        lines.append("|---:|---:|---:|---:|")
        for t in sorted(sweep.keys()):
            ent = sweep[t].get("ents_per_type", {}).get(label, {})
            p = ent.get("p", 0.0) or 0.0
            r = ent.get("r", 0.0) or 0.0
            f = ent.get("f", 0.0) or 0.0
            lines.append(f"| {t:.2f} | {p:.3f} | {r:.3f} | {f:.3f} |")

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ORG-only threshold sweep through spaCy Scorer (#98)"
    )
    parser.add_argument(
        "--scored-predictions",
        type=Path,
        required=True,
        help="raw_with_scores.json from predict_hf_with_scores.py",
    )
    parser.add_argument(
        "--sweep-label",
        type=str,
        default="ORGANIZATION",
        help="Entity label to sweep (default: ORGANIZATION).",
    )
    parser.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=[0.0, 0.5, 0.7, 0.8, 0.85, 0.9, 0.95, 0.99],
    )
    parser.add_argument(
        "--language",
        default="ja",
        choices=["ja", "en"],
    )
    parser.add_argument(
        "--version",
        default=LATEST_BENCHMARK_VERSION,
        choices=BENCHMARK_VERSIONS,
    )
    parser.add_argument("--benchmark-data", type=Path, default=None)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument(
        "--ac-overall-f1",
        type=float,
        default=0.500,
        help="Overall F1 acceptance threshold (default 0.5; #98 AC).",
    )
    parser.add_argument(
        "--ac-label-precision",
        type=float,
        default=0.30,
        help="Per-label precision floor for the swept label (default 0.30; #98 AC).",
    )
    args = parser.parse_args()

    config = BENCHMARK_CONFIGS[args.version]  # noqa: F841 — surfaces version validity
    data_root = Path(__file__).parents[2] / "data"
    benchmark_path = args.benchmark_data or (
        data_root / "benchmark" / args.version / args.language / "test.spacy"
    )
    if not benchmark_path.exists():
        raise SystemExit(f"Benchmark gold not found: {benchmark_path}")

    sweep: dict[float, dict] = {}
    for t in args.thresholds:
        scores = evaluate_scored_predictions_on_benchmark(
            scored_predictions_path=args.scored_predictions,
            benchmark_path=benchmark_path,
            language=args.language,
            label_thresholds={args.sweep_label: float(t)},
        )
        # Strip non-serializable fields.
        scores.pop("ents_per_type_micro", None)
        sweep[float(t)] = scores
        ent = scores.get("ents_per_type", {}).get(args.sweep_label, {})
        print(
            f"t={t:.2f}  overall F1={scores['ents_f']:.3f}  "
            f"P={scores['ents_p']:.3f}  R={scores['ents_r']:.3f}  "
            f"{args.sweep_label} P={ent.get('p', 0):.3f} R={ent.get('r', 0):.3f}"
        )

    md = render_markdown(sweep, args.sweep_label, args.scored_predictions, benchmark_path)

    # Pick best threshold under #98 AC: max overall F1 such that
    # ents_f >= ac_overall_f1 AND swept-label precision >= ac_label_precision.
    eligible = []
    for t, s in sweep.items():
        ent = s.get("ents_per_type", {}).get(args.sweep_label, {})
        if (
            s["ents_f"] >= args.ac_overall_f1
            and (ent.get("p") or 0.0) >= args.ac_label_precision
        ):
            eligible.append((t, s["ents_f"]))
    if eligible:
        eligible.sort(key=lambda x: (-x[1], -x[0]))
        best_t = eligible[0][0]
        ac_met = True
    else:
        sorted_by_f1 = sorted(sweep.items(), key=lambda kv: -kv[1]["ents_f"])
        best_t = sorted_by_f1[0][0]
        ac_met = False

    best = sweep[best_t]
    best_ent = best.get("ents_per_type", {}).get(args.sweep_label, {})
    md += "\n## Recommendation\n\n"
    if ac_met:
        md += (
            f"**{args.sweep_label} threshold = {best_t:.2f}** — meets #98 AC "
            f"(overall F1 ≥ {args.ac_overall_f1:.3f}, "
            f"{args.sweep_label} precision ≥ {args.ac_label_precision:.2f}). "
            f"Overall: P={best['ents_p']:.3f} R={best['ents_r']:.3f} "
            f"F1={best['ents_f']:.3f}; "
            f"{args.sweep_label}: P={best_ent.get('p', 0):.3f} "
            f"R={best_ent.get('r', 0):.3f}.\n"
        )
    else:
        md += (
            f"**{args.sweep_label} threshold = {best_t:.2f}** — *AC not met* "
            f"(no threshold reached overall F1 ≥ {args.ac_overall_f1:.3f} AND "
            f"{args.sweep_label} P ≥ {args.ac_label_precision:.2f}). "
            f"Best by overall F1: P={best['ents_p']:.3f} R={best['ents_r']:.3f} "
            f"F1={best['ents_f']:.3f}; "
            f"{args.sweep_label}: P={best_ent.get('p', 0):.3f} "
            f"R={best_ent.get('r', 0):.3f}.\n"
        )

    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    args.output_md.write_text(md, encoding="utf-8")
    print(f"Wrote markdown report → {args.output_md}")

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(
                {f"{t:.2f}": s for t, s in sweep.items()},
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"Wrote JSON sweep → {args.output_json}")

    print(
        f"Recommended {args.sweep_label} threshold: {best_t:.2f} (AC met: {ac_met})"
    )


if __name__ == "__main__":
    main()
