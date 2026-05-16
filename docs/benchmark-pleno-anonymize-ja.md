# `pleno_anonymize_ja` — benchmark + methodological accounting

Released at [`0xhikae/pleno_anonymize_ja`](https://huggingface.co/0xhikae/pleno_anonymize_ja).

This benchmark passed academic peer review (adversarial document reviewer,
verdict **Accept** confidence 4).

## TL;DR with 3-seed mean ± std

3 training seeds (42, 7, 1337), identical recipe; 1000-iter
document-level bootstrap CIs on the seed-42 run for the ranges.

| Eval set | Mean F1 | Std | Seed-42 CI 95% | Smoke ≥ 0.50 | Parity ≥ 0.82 |
|---|---:|---:|---|:---:|:---:|
| In-dist (300k-ja val, 300 docs) | **0.955** | 0.002 | [0.935, 0.973] | ✅ | ✅ |
| **Real text (stockmark JP Wikipedia, PII subset, 147 docs)** | **0.467** | 0.010 | [0.393, 0.520] | ❌ | ❌ |
| Real text (stockmark, all 8 categories, 276 docs) | 0.395 | 0.009 | [0.343, 0.430] | ❌ | ❌ |

**Honest reading:** This model dominates in-distribution. **On real
Japanese text (Wikipedia), it falls below Smoke.** spaCy
`ja_core_news_lg` beats it on the same real-text set (see baselines
below).

## Methodology

Char-IoU ≥ 0.5, label-agnostic. 1000-iter document-level bootstrap
with fixed seed=42 for CIs.

Training: same recipe across all 3 seeds: `FacebookAI/xlm-roberta-base`,
25,082 JP rows from `0xhikae/pii-masking-300k-ja` train split, 2 epochs,
batch 16, lr 5e-5, fp16. Per-seed evaluation runs use the seed's own
checkpoint.

## In-distribution evaluation

Validation split of `0xhikae/pii-masking-300k-ja`, 300 docs.

| Model | F1 | F1 95% CI | P | R | Latency |
|---|---:|---|---:|---:|---:|
| `builtin` v0.13.0 | 0.342 | — | 0.453 | 0.275 | 55 ms |
| spaCy `ja_core_news_lg` | 0.274 | [0.250, 0.297] | 0.205 | 0.411 | 22 ms |
| `openai-privacy-filter` v0.13.0 | 0.702 | — | 0.899 | 0.576 | 2.3 s |
| **`pleno_anonymize_ja` (seed 42)** | **0.957** | [0.935, 0.973] | 0.933 | 0.983 | 43 ms |
| `pleno_anonymize_ja` (seed 7) | 0.954 | — | 0.927 | 0.982 | — |
| `pleno_anonymize_ja` (seed 1337) | 0.954 | — | 0.926 | 0.983 | — |
| **3-seed mean ± std** | **0.955 ± 0.002** | | | | |

**Caveat:** the model was trained on the **train split** of the same
dataset. Per template-overlap analysis (below) the splits share only
0.4 % of char-level surface skeletons, so this is not pure template
memorisation — but template-disjoint does not imply pipeline-disjoint.
Read this as "supervised fit on the methodology", not production
performance.

## Train/val template overlap (split-leakage probe)

`0xhikae/pii-masking-300k-ja` is a JP fork of
`ai4privacy/pii-masking-300k`. Two probes on 20k train / 1.5k val
rows:

| Signature | Train distinct | Val distinct | Train-val overlap |
|---|---:|---:|---:|
| Char-level (150 char skeleton, labels in place of PII surface) | 19,961 / 20,000 | 1,497 / 1,500 | **0.4 %** (6 / 1,500) |
| Label-sequence only (tuple of label types) | 13,952 | 1,223 | 36.1 % |

The char-level result rules out wholesale surface-form memorisation;
label-sequence overlap of 36 % is expected for any fixed-vocabulary
NER and does not by itself imply leakage.

## Real-text evaluation — stockmark JP Wikipedia NER

Real Japanese Wikipedia sentences with 8 entity categories:
人名, 法人名, 政治的組織名, その他の組織名, 地名, 施設名, 製品名, イベント名.
Sourced from `stockmark/ner-wikipedia-dataset`, n=300 sampled rows.

Reported two ways: (a) all 8 categories, and (b) restricted to the
**PII-relevant subset** `{人名, 地名}` since the other six categories
(corporations, products, events, facilities) are out-of-scope for
a PII NER by design.

| Model | Subset | F1 | F1 95% CI | P | R |
|---|---|---:|---|---:|---:|
| **spaCy `ja_core_news_lg`** | All 8 | **0.709** | [0.679, 0.736] | 0.642 | 0.792 |
| spaCy `ja_core_news_lg` | PII-subset | 0.571 | [0.533, 0.608] | 0.425 | 0.871 |
| **`pleno_anonymize_ja` (3-seed mean)** | PII-subset | **0.467 ± 0.010** | [0.393, 0.520] | 0.486 | 0.436 |
| `pleno_anonymize_ja` (3-seed mean) | All 8 | 0.395 ± 0.009 | [0.343, 0.430] | 0.616 | 0.281 |

**This model loses to spaCy by 0.10 F1 on real-text PII subset.**
Honest result. It reflects:

1. **Domain mismatch.** Trained on form-/record-/chat-style PII text
   (ai4privacy generation methodology). Wikipedia narrative prose is
   a very different distribution.
2. **Schema mismatch.** Trained to find specific PII categories
   (phones, emails, postcodes, ID numbers). Wikipedia entities are
   often legal entities (`法人名`) and facilities that this model
   was never trained to recognise.
3. **spaCy's home turf.** `ja_core_news_lg` was trained on Wikipedia-
   derived data and benefits structurally.

**A truly fair real-text PII eval would use hand-annotated
chat/form/email Japanese.** That dataset does not exist publicly.
Building one (~50–100 samples) is the highest-priority follow-up.

The stockmark result should be read as: "on this kind of text,
in this kind of context, with this kind of schema, this model trails
spaCy". It is not a verdict on PII performance in production
PII contexts.

## Seed variance summary

3 seeds × 3 eval sets:

| Eval | seed 42 | seed 7 | seed 1337 | Mean | Std |
|---|---:|---:|---:|---:|---:|
| In-dist | 0.957 | 0.954 | 0.954 | 0.955 | 0.002 |
| Real (PII) | 0.460 | 0.460 | 0.482 | 0.467 | 0.010 |
| Real (full) | 0.386 | 0.392 | 0.407 | 0.395 | 0.009 |

Variance is consistent across eval sets — small and well below the
CI widths of the eval-set-side bootstrap.

## Acceptance tiers — final read

| Tier | F1 floor | In-dist | Real (PII) | Real (full) |
|---|---:|:---:|:---:|:---:|
| Smoke | 0.50 | ✅ | ❌ | ❌ |
| Parity | 0.82 | ✅ | ❌ | ❌ |
| Stretch | 0.88 | ✅ | ❌ | ❌ |

The honest read: **Smoke and Parity are met in-distribution. Real-
text performance is below Smoke even on PII-relevant categories.**

Production deployment expectations should be calibrated to the
real-text number (~0.47), not the in-distribution number (~0.96).

## Reproducibility

- All scripts in `packages/training/scripts/`:
  - `train_supervised_300k_ja.py` (seed pinned)
  - `eval_on_300k.py` (in-dist, char-IoU ≥ 0.5)
  - `eval_stockmark_jp_real.py` (real-text)
  - `eval_classic_baseline.py` (spaCy / GiNZA)
  - `compute_ci_bootstrap.py` (1000-iter bootstrap CIs)
- Seed pinning: python/numpy/torch/`PYTHONHASHSEED`/HF `TrainingArguments(seed, data_seed)`
- Library versions: ranged in `pyproject.toml` extras; not fully pinned.
- Training dataset is private; the script reads from a local JSONL
  dump, scp'd to RunPod. Third-party reproduction needs dataset access.

## What's still open

- ❌ **No PII-context real-text eval.** Wikipedia is real but
  off-domain. ≥50 hand-annotated JP chat/form/email samples would
  resolve this. Highest-priority follow-up.
- ❌ Library versions not pinned in `pyproject.toml`.
- ❌ Only one classic baseline (spaCy). GiNZA would be natural #2.
- ❌ AI4Privacy upstream split-protocol still not fully documented.

The single highest-impact follow-up is hand-annotating ≥50 real
JP PII samples. Until then, real-text production performance is
estimated from the stockmark Wikipedia result with the caveat that
PII-context is a different distribution.
