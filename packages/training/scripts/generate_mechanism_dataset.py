"""Generate the JP PII mechanism-v1 synthetic dataset (Simula 5/8).

Composes the four upstream stages:
    taxonomy + meta-prompts  -> LLM generation  -> complexification
                              -> dual-critic gate
    output: data/raw/ja-mechanism-v1/all.jsonl
            data/raw/ja-mechanism-v1/{train,dev,test}.jsonl

Cost note: at 5 lenses × 382 leaves = 1,910 meta-prompts, with
`--samples-per-prompt 16` the total OpenAI call count is ≈ 30,560.
With `gpt-4o-mini` (~$0.0005/call) this runs around $15 and ~30
minutes wall-clock at `--max-workers 32`.

Use `--smoke` for a 100-sample dry run that exercises the whole
pipeline end-to-end before committing to the full ≥ 30k run.

Example (smoke):
    dotenvx run -f ../../.env -- \\
        uv run --extra training python scripts/generate_mechanism_dataset.py \\
            --meta-prompts data/meta_prompts/jp/all.jsonl \\
            --smoke

Example (full):
    dotenvx run -f ../../.env -- \\
        uv run --extra training python scripts/generate_mechanism_dataset.py \\
            --meta-prompts data/meta_prompts/jp/all.jsonl \\
            --samples-per-prompt 16 --max-workers 32 \\
            --output-dir data/raw/ja-mechanism-v1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pleno_ner_training.mechanism.generate import (  # noqa: E402
    entity_histogram,
    generate_dataset,
    load_meta_prompts,
    split_dataset,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--meta-prompts", type=Path, default=Path("data/meta_prompts/jp/all.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/raw/ja-mechanism-v1"))
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--samples-per-prompt", type=int, default=2)
    parser.add_argument("--max-workers", type=int, default=16)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--limit-prompts",
        type=int,
        default=None,
        help="Cap how many meta-prompts to use (for cost-controlled smoke runs).",
    )
    parser.add_argument("--smoke", action="store_true",
                        help="50-prompt × 2-samples smoke run; overrides --limit-prompts and --samples-per-prompt.")
    parser.add_argument("--skip-complexification", action="store_true")
    parser.add_argument("--skip-critics", action="store_true")
    args = parser.parse_args()

    if args.smoke:
        args.limit_prompts = 50
        args.samples_per_prompt = 2
        args.max_workers = 8

    meta = load_meta_prompts(args.meta_prompts)
    if args.limit_prompts:
        meta = meta[: args.limit_prompts]
    print(f"[plan] meta_prompts={len(meta)} samples_per_prompt={args.samples_per_prompt} "
          f"max_workers={args.max_workers} model={args.model}")

    raw_path = args.output_dir / "all.jsonl"
    log_path = args.output_dir / "generation_log.json"
    stats = generate_dataset(
        meta_prompts=meta,
        samples_per_prompt=args.samples_per_prompt,
        model=args.model,
        max_workers=args.max_workers,
        output_path=raw_path,
        log_path=log_path,
        seed=args.seed,
        skip_complexification=args.skip_complexification,
        skip_critics=args.skip_critics,
    )
    print(f"[generated] accepted={stats['accepted']} accept_rate={stats['accept_rate']:.2%}")

    splits = split_dataset(
        raw_path,
        args.output_dir / "train.jsonl",
        args.output_dir / "dev.jsonl",
        args.output_dir / "test.jsonl",
        seed=args.seed,
    )
    print(f"[split] {splits}")

    hist = entity_histogram(raw_path)
    hist_path = args.output_dir / "entity_histogram.json"
    hist_path.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[histogram] {hist_path}")

    # AC check
    if stats["accepted"] == 0:
        raise SystemExit("FAIL: zero samples generated")
    if not args.smoke and stats["accepted"] < 30000:
        print(f"WARN: only {stats['accepted']} samples (< 30k target). "
              "Increase --samples-per-prompt or rerun.", file=sys.stderr)
    if stats["accept_rate"] < 0.30:
        print(f"WARN: accept rate {stats['accept_rate']:.2%} < 30%; tune critics or prompts.", file=sys.stderr)


if __name__ == "__main__":
    main()
