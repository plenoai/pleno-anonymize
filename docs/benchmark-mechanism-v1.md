# `ja_ner_ja-v2` (mechanism-v1) — baseline benchmark

**Status:** initial training run complete (2,014 samples, RPD-limited). Numbers below are from `make eval-mechanism-300k-ja` on the resulting checkpoint pushed to `0xhikae/ja_ner_ja-v2-mechanism` (private).

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
| **`ja_ner_ja-v2` (mechanism-v1, 2k samples)** | **0.352** | **0.612** | **0.247** |

Latency: 36.77 ms/doc avg (CPU). Source: `output/pii-300k-ja-mechanism-v1.json`.

The first run lands marginally above the `builtin` floor (+0.010 F1) with
substantially higher precision (0.612 vs 0.453) but lower recall. Recall is
the obvious target: the model was trained on only 2,014 samples (OpenAI
RPD-limited) vs the ≥15k target. Top per-label recall gaps:

- `TIME` 0.6% — model effectively does not predict time spans
- `COUNTRY`, `SECADDRESS`, `PASS`, `GEOCOORD` 0% — out of taxonomy
- `STATE` 3.3%, `SEX` 7.4%, `BUILDING` 5.6%, `IP` 7.6% — under-represented in 2k samples

Strong labels (recall): `TEL` 0.765, `BOD` 0.729, `EMAIL` 0.638, `USERNAME` 0.444.

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
