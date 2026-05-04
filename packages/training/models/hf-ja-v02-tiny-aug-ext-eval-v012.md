# hf-ja-v02-tiny-aug-ext: v0.12.0 adversarial eval

**Issue:** [#48 [Phase 2] NER 再訓練 RunPod GPU で adversarial precision を 33% → 70%+ に](https://github.com/plenoai/pleno-anonymize/issues/48)
**Trained:** 2026-05-03 (RunPod RTX A5000 community pod, ~11 min, $0.05)
**Base model:** `ku-nlp/deberta-v2-tiny-japanese`
**Artifact:** `packages/training/output/hf-ja-v02-tiny-aug-ext/` (30 MB safetensors)
**Benchmark:** `data/benchmark/v0.12.0/ja` (500 docs, FP-pressure DLP corpus, strict-span F1 micro)

## Summary

Acceptance criterion (`overall precision ≥ 70 %`) **NOT MET** — achieved **30.3 %**.
4 of 5 entities improved over the prior `hf_v02_tiny_hardneg` baseline; **ORGANIZATION precision remains the bottleneck (8.8 %, dominant FP source)**.

## Per-entity scores (strict-span F1, micro)

| Entity        | F1    | Precision | Recall | Δ F1 vs hardneg |
|---            |---    |---        |---     |---              |
| PERSON        | 0.951 | 0.921     | 0.983  | +0.072          |
| ADDRESS       | 0.722 | 0.673     | 0.778  | +0.055          |
| ORGANIZATION  | 0.160 | 0.088     | 0.838  | +0.004          |
| DATE_OF_BIRTH | 0.613 | 0.442     | 1.000  | +0.023          |
| BANK_ACCOUNT  | 0.717 | 0.655     | 0.792  | +0.159          |
| **Overall**   | **0.452** | **0.303** | **0.883** | **+0.036**  |

Comparison anchor (same corpus, same eval script):

| Model                       | Overall F1 | Overall P | Overall R |
|---                          |---         |---        |---        |
| `pleno_ner` (spaCy CNN)     | 0.490      | 0.330     | 0.952     |
| `hf_v02_tiny`               | 0.374      | 0.231     | 0.979     |
| `hf_v02_tiny_hardneg`       | 0.416      | 0.263     | 0.989     |
| **`hf_v02_tiny_aug_ext`**   | **0.452**  | **0.303** | **0.883** |

## What worked

- **PERSON +7.2 pt F1** — augmented diversity transferred cleanly.
- **BANK_ACCOUNT +15.9 pt F1** — heuristic-filtered hardneg + ORG diversification reduced bank-vs-code FPs.
- **ADDRESS recovered to 72.2 %** above the 69.2 % `hf_v02_tiny` baseline.

## What failed (ORG)

- ORG FP count is the dominant overall-precision drag.
- `external_hardneg.json` (4 000 Wikipedia docs) reduced FP rate but did not change the marginal cost of an ORG token: heuristic filter is too permissive; Wikipedia ORG-mention density remains high.
- Recall/precision tradeoff is wrong for the use case (DLP wants high P, can tolerate slight R loss).

## Training details

- Dataset: `data/hf/ja-v02-tiny-aug-ext` (train 16554 / dev 2069 / test 2070, total 21 565 docs)
  - augmented.json (#80 ORG diverse seeds via `augment-ja`)
  - external_hardneg.json (#64 4 000 Wikipedia docs, CC-BY-SA 4.0)
- Hyperparameters: 20 epochs, batch 16, lr 3e-5, warmup 0.15, fp16
- Step count: 20 700 (1 035/epoch × 20)
- Loss curve: 1.32 → 0.143
- Wallclock: 648 s on RTX A5000

## Decision

- Tag `model/v0.13.0` deferred — precision target not met.
- Keep current production tag (`model/v0.12.x`) until ORG-FP work lands.
- Open follow-up issue: ORG recognition needs a different intervention (label noise audit / negative loss reweighting / postprocessing precision floor) — see ner-improve skill.

## Reproducibility

```bash
# Train (RunPod GPU, ~11 min on RTX A5000)
make convert-hf-v02-tiny-aug-ext
make train-hf-v02-tiny-aug-ext

# Evaluate
uv run python -m pleno_ner_training.evaluate_benchmark \
  --benchmark-dir data/benchmark/v0.12.0/ja \
  --hf-model output/hf-ja-v02-tiny-aug-ext \
  --output data/benchmark/v0.12.0/ja/scores.json \
  --label hf_v02_tiny_aug_ext
```
