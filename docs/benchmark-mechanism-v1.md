# `ja_ner_ja-v2` (mechanism-v1) — baseline benchmark

**Status:** infrastructure ready (#154 PR landed). Numbers will appear here after the RunPod training run completes and `make eval-mechanism-300k-ja` is executed against the resulting checkpoint.

## Methodology

Identical to [`docs/benchmark.md`](benchmark.md) §4 (the public ruler):

- Dataset: `0xhikae/pii-masking-300k-ja`, `validation` split.
- Sample size: n = 300 (primary), n = 50 (pilot).
- Scoring: char-IoU ≥ 0.5, label-agnostic span matching.
- Metric: overall F1 (primary), per-label recall, avg latency / doc (CPU).

We deliberately reuse the **same evaluator** the SDK uses for the
shipped engines so v2 numbers are directly comparable to the
published v0.13.0 baseline:

| Engine | F1 | Precision | Recall |
|---|---:|---:|---:|
| `builtin` v0.13.0 | 0.342 | 0.453 | 0.275 |
| `openai-privacy-filter` v0.13.0 | 0.702 | 0.899 | 0.576 |
| **`ja_ner_ja-v2` (mechanism-v1)** | **TBD** | **TBD** | **TBD** |

## Eval driver

`packages/training/scripts/eval_mechanism_on_300k.py` runs the same
char-IoU ≥ 0.5 scoring as `packages/sdk/scripts/eval_pii_masking_300k.py`
but feeds the input through an arbitrary HuggingFace
`AutoModelForTokenClassification`. This keeps the SDK eval path
unchanged (the public ruler is fixed; #154 must not modify it).

```bash
cd packages/training
make eval-mechanism-300k-ja BENCH_MODEL=plenoai/ja_ner_ja-v2 BENCH_LIMIT=300
```

## Acceptance tiers (per `.claude/skills/ner-improve/SKILL.md`)

| Tier | F1 floor | Implication |
|---|---:|---|
| Smoke | 0.50 | first meaningful improvement over `builtin` (0.342); ship behind a flag |
| Parity | 0.82 | matches `openai-privacy-filter`; promote to default backend |
| Stretch | 0.88 | exceeds OPF; mark as production default |

Per-label recall floors apply additionally — see SKILL.md.

## Risks

- **Domain mismatch** — the synthetic dataset uses pleno's 17-label
  taxonomy, while `300k-ja` uses AI4Privacy's 27 fine-grained labels.
  The label-agnostic IoU protocol partially neutralises this, but
  classes that pleno doesn't model at all (e.g. `USERNAME`) will
  contribute only to FN.
- **Length distribution** — the synthetic dataset skews longer than
  AI4Privacy chat snippets; bertbase truncation may matter at the
  margin.
- **Tokenizer mismatch** — fine-tuning on `cl-tohoku/bert-base-japanese-v3`
  vs. evaluating on the AI4Privacy split (no JP coverage there;
  validation uses `300k-ja` which is our independent JP fork).
