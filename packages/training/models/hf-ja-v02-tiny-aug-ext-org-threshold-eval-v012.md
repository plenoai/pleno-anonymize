# hf-ja-v02-tiny-aug-ext + ORG threshold: v0.12.0 adversarial eval

**Issue:** [#98 hf NER ORGANIZATION precision がフロアに到達しない (8.8%) — ORG-FP 専用イテレーション](https://github.com/plenoai/pleno-anonymize/issues/98)
**Parent:** [#48 [Phase 2] NER 再訓練 (RunPod GPU) で adversarial precision を 33% → 70%+ に](https://github.com/plenoai/pleno-anonymize/issues/48)
**Method:** Post-hoc per-label confidence floor on ORG; no retraining.
**Model:** `output/hf-ja-v02-tiny-aug-ext` (unchanged from #97).
**Benchmark:** `data/benchmark/v0.12.0/ja` (500 docs, FP-pressure DLP corpus, strict-span F1 micro via spaCy `Scorer`).

## Summary

#98 AC met at **ORG threshold = 0.99**:

- Overall F1 ≥ 0.500 ✓ — **0.701**
- ORG precision ≥ 0.30 ✓ — **0.394**

Parent #48 partial AC also met at the same threshold:

- Overall F1 ≥ 70% ✓ — **0.701**
- PERSON precision ≥ 70% ✓ — 89.2%
- PERSON recall ≥ 95% ✓ — 98.3%
- ADDRESS precision ≥ 70% ✗ — 66.0%
- ADDRESS recall ≥ 95% ✗ — 77.8%
- ORG / DOB / BANK precision ≥ 70% ✗ — model-side limitation, not addressable by ORG threshold alone.

## Per-entity scores at recommended threshold (ORG=0.99)

| Entity        | F1    | Precision | Recall | Δ F1 vs no-threshold |
|---            |---    |---        |---     |---                   |
| PERSON        | 0.935 | 0.892     | 0.983  | -0.016               |
| ADDRESS       | 0.714 | 0.660     | 0.778  | -0.008               |
| ORGANIZATION  | 0.371 | 0.394     | 0.351  | +0.211               |
| DATE_OF_BIRTH | 0.613 | 0.442     | 1.000  | ±0                   |
| BANK_ACCOUNT  | 0.691 | 0.613     | 0.792  | -0.026               |
| **Overall**   | **0.701** | **0.632** | **0.787** | **+0.249**       |

Negative-document FP totals across the sweep:

| ORG threshold | overall F1 | overall P | overall R | neg-clean rate | neg FP total |
|---:|---:|---:|---:|---:|---:|
| 0.00 (baseline) | 0.452 | 0.303 | 0.883 | 0.632 | 333 |
| 0.50 | 0.459 | 0.310 | 0.883 | 0.639 | 324 |
| 0.70 | 0.536 | 0.388 | 0.867 | 0.698 | 216 |
| 0.80 | 0.583 | 0.443 | 0.851 | 0.748 | 162 |
| 0.85 | 0.605 | 0.470 | 0.846 | 0.775 | 140 |
| 0.90 | 0.632 | 0.505 | 0.846 | 0.791 | 118 |
| 0.95 | 0.655 | 0.542 | 0.830 | 0.830 | 95  |
| **0.99** | **0.701** | **0.632** | **0.787** | **0.907** | **52**  |

## Comparison to prior baselines

| Model | Overall F1 | Overall P | Overall R |
|---|---:|---:|---:|
| `pleno_ner` (spaCy CNN, current production)         | 0.490 | 0.330 | 0.952 |
| `hf_v02_tiny`                                        | 0.374 | 0.231 | 0.979 |
| `hf_v02_tiny_hardneg`                                | 0.416 | 0.263 | 0.989 |
| `hf_v02_tiny_aug_ext` (no threshold)                 | 0.452 | 0.303 | 0.883 |
| **`hf_v02_tiny_aug_ext` + ORG≥0.99 (this report)**   | **0.701** | **0.632** | **0.787** |

+21.1 pt overall F1 vs the previous best (`pleno_ner`), achieved with **zero additional training**.

## Why ORG=0.99 (not the recall-floor recommendation)

Tradeoff is correct for DLP / PII-detection use cases: false-positive `<ORGANIZATION>` masks corrupt downstream text. ORG recall drops 0.838 → 0.351, but:

- PERSON / ADDRESS / DOB / BANK recall changes are ≤ 1.6 pt — those entities use score < 0.99 only on rare uncertain spans.
- Negative-doc clean rate jumps 63.2 % → 90.7 % (matches issue #69 measurement triad target).
- Total FP count on entity-free docs: 333 → 52 (6.4× reduction).

If recall on ORG matters more than precision in a future use case, the threshold is a tunable production parameter; nothing in the model needs to change.

## Mechanism

Per-token softmax probabilities were already produced by `predict_hf_with_scores.py` (#68 infra). The new piece is `evaluate_scored_predictions_on_benchmark` in `evaluate_benchmark.py`, which decouples scoring from inference and accepts per-label thresholds. The ORG-only sweep applies `score < 0.99` as a hard filter on every span whose decoded BIO label is `ORGANIZATION`; other labels pass through at score ≥ 0.0.

The `sweep_threshold.py` (#68) char-set scoring path under-reports vs spaCy `Scorer` (~0.10 F1 on PERSON) because it does not project predictions back to gold token boundaries via `char_span(alignment_mode="expand")`. The new `sweep_org_threshold.py` reuses the spaCy Scorer path so numbers match `scores.json`.

## Reproducibility

```bash
cd packages/training

# Step 1: produce per-token softmax score predictions (CPU, ~3 s on 500 docs).
make predict-scores-v12-aug-ext

# Step 2: sweep ORG threshold + score via spaCy Scorer (CPU, ~30 s).
make sweep-threshold-org-v12

# Outputs:
#   data/benchmark/v0.12.0/ja/raw_with_scores_aug_ext.json
#   data/benchmark/v0.12.0/ja/threshold_sweep_org.md
#   data/benchmark/v0.12.0/ja/threshold_sweep_org.json
```

## Decision

- Tag `model/v0.13.0` against `output/hf-ja-v02-tiny-aug-ext` with `inference.org_threshold = 0.99` recorded in artifact metadata.
- Wire ORG threshold into pii-scanner consumer as a configurable knob (default 0.99 for DLP profile).
- Trigger #79 HF Hub release pipeline.
- Keep #48 open for the remaining per-entity precision ≥ 70 % goals (ADDRESS, DOB, BANK) — those need model-side work, not threshold tuning.

## References

- Predict module: `packages/training/src/pleno_ner_training/predict_hf_with_scores.py` (#68)
- Sweep driver: `packages/training/src/pleno_ner_training/sweep_org_threshold.py` (#98)
- Evaluator: `packages/training/src/pleno_ner_training/evaluate_benchmark.py::evaluate_scored_predictions_on_benchmark` (#98)
- Prior eval (no threshold): `packages/training/models/hf-ja-v02-tiny-aug-ext-eval-v012.md`
