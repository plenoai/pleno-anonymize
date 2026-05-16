"""Apply complexification + score difficulty across a JSONL sample stream.

Input  (JSONL):  {"text": "...", "entities": [{"start", "end", "label"}, ...]}
Output (JSONL):  {..., "difficulty": float, "operators_applied": [...]}

The default run is a pure rule-based heuristic — fast, no network. The
optional `--llm-elo` flag runs pairwise comparisons through an LLM
(per Simula §4) and rescales difficulty into the Elo's percentile so
the heuristic remains the bucketing default while the Elo serves as a
calibrating signal.

Example:
    uv run --extra training python scripts/score_difficulty.py \\
        --input  data/processed/ja-mechanism-v1/raw.jsonl \\
        --output data/processed/ja-mechanism-v1/scored.jsonl \\
        --histogram output/difficulty_hist.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pleno_ner_training.mechanism.complexify import (  # noqa: E402
    DEFAULT_TARGET,
    Sample,
    Span,
    apply_with_ratio,
    bucket,
    difficulty_score,
    histogram,
)


def _from_dict(d: dict) -> Sample:
    return Sample(
        text=d["text"],
        entities=[Span(e["start"], e["end"], e["label"]) for e in d.get("entities", [])],
    )


def _to_dict(s: Sample) -> dict:
    return {
        "text": s.text,
        "entities": [{"start": e.start, "end": e.end, "label": e.label} for e in s.entities],
        "difficulty": s.difficulty,
        "difficulty_bucket": bucket(s.difficulty) if s.difficulty is not None else None,
        "operators_applied": s.operators_applied,
    }


def _elo_rescale(samples: list[Sample], model: str, sample_size: int) -> None:
    """Optional LLM pairwise Elo to recalibrate difficulty scores in place."""
    try:
        from openai import OpenAI
    except ImportError:
        print("openai not installed; skipping --llm-elo.", file=sys.stderr)
        return
    if "OPENAI_API_KEY" not in os.environ:
        print("OPENAI_API_KEY missing; skipping --llm-elo.", file=sys.stderr)
        return

    import random

    client = OpenAI()
    rng = random.Random(0)
    elo = {i: 1500.0 for i in range(len(samples))}
    k = 32

    n_pairs = min(sample_size, len(samples) * 2)
    for _ in range(n_pairs):
        i, j = rng.sample(range(len(samples)), 2)
        a, b = samples[i].text, samples[j].text
        prompt = (
            "次の 2 つの日本語テキストのうち、PII 抽出 NER モデルにとって "
            "**より難しい** のはどちらですか。番号 (1 or 2) のみ回答してください。\n\n"
            f"1. {a[:600]}\n\n2. {b[:600]}"
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.0,
                max_tokens=2,
                messages=[{"role": "user", "content": prompt}],
            )
            verdict = (resp.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"elo call failed: {e}", file=sys.stderr)
            continue
        if verdict.startswith("1"):
            winner, loser = i, j
        elif verdict.startswith("2"):
            winner, loser = j, i
        else:
            continue
        expected_winner = 1.0 / (1 + 10 ** ((elo[loser] - elo[winner]) / 400))
        elo[winner] += k * (1 - expected_winner)
        elo[loser] += k * (0 - (1 - expected_winner))

    # Rescale into [0, 1] over the observed Elo distribution.
    ranks = sorted(elo.items(), key=lambda kv: kv[1])
    for rank_idx, (sample_idx, _) in enumerate(ranks):
        samples[sample_idx].difficulty = rank_idx / max(len(samples) - 1, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--histogram", type=Path, default=None)
    parser.add_argument("--apply-operators", action="store_true",
                        help="Also rewrite a fraction of samples to hit target buckets.")
    parser.add_argument("--easy-ratio", type=float, default=DEFAULT_TARGET["easy"])
    parser.add_argument("--medium-ratio", type=float, default=DEFAULT_TARGET["medium"])
    parser.add_argument("--hard-ratio", type=float, default=DEFAULT_TARGET["hard"])
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--llm-elo", action="store_true", help="Calibrate via LLM Elo pairs.")
    parser.add_argument("--elo-model", default="gpt-4o-mini")
    parser.add_argument("--elo-pairs", type=int, default=200)
    args = parser.parse_args()

    samples: list[Sample] = []
    with args.input.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            samples.append(_from_dict(json.loads(line)))
    print(f"[load] {len(samples)} samples from {args.input}")

    if args.apply_operators:
        target = {
            "easy": args.easy_ratio,
            "medium": args.medium_ratio,
            "hard": args.hard_ratio,
        }
        samples = apply_with_ratio(samples, target=target, seed=args.seed)
        applied = sum(1 for s in samples if s.operators_applied)
        print(f"[apply] hardened {applied} / {len(samples)} samples to hit target {target}")

    for s in samples:
        if s.difficulty is None:
            s.difficulty = difficulty_score(s)

    if args.llm_elo:
        _elo_rescale(samples, model=args.elo_model, sample_size=args.elo_pairs)
        print(f"[elo] rescaled via {args.elo_model} over {args.elo_pairs} pairs")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(_to_dict(s), ensure_ascii=False) + "\n")
    print(f"[write] {args.output}")

    if args.histogram:
        hist = histogram(samples)
        buckets = {"easy": 0, "medium": 0, "hard": 0}
        for s in samples:
            buckets[bucket(s.difficulty or 0)] += 1
        args.histogram.parent.mkdir(parents=True, exist_ok=True)
        args.histogram.write_text(
            json.dumps({"hist_10": hist, "buckets": buckets}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[write] {args.histogram} buckets={buckets}")


if __name__ == "__main__":
    main()
