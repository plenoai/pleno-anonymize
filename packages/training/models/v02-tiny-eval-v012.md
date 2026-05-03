# hf-ja-v02-tiny: v0.12.0 adversarial evaluation

Trained: 2026-05-03 on RunPod RTX A5000 (~8 min, 20 epochs, 8540 steps).

Base model: `ku-nlp/deberta-v2-tiny-japanese`
Train data: `raw_ja_v02_generated.json` (8533 valid docs after noise filter)
Hyperparams: lr=3e-5, bs=16, warmup=0.15, weight_decay=0.01, fp16

## Metrics

| split | precision | recall | F1 |
| --- | --- | --- | --- |
| dev (train-time eval) | 0.912 | 0.948 | **0.929** |
| test (train-time eval) | 0.907 | 0.937 | **0.922** |
| **adversarial v0.12.0/raw.json** | 0.231 | 0.979 | **0.374** |

The dev/test splits come from the same generated synthetic distribution; adversarial v0.12.0 is the OOD precision benchmark.

## Per-label on v0.12.0

| label | TP | FP | FN | P | R | F1 |
| --- | --- | --- | --- | --- | --- | --- |
| ADDRESS | 45 | 40 | 0 | 0.529 | 1.000 | 0.692 |
| BANK_ACCOUNT | 24 | 30 | 0 | 0.444 | 1.000 | 0.615 |
| DATE_OF_BIRTH | 23 | 33 | 0 | 0.411 | 1.000 | 0.582 |
| ORGANIZATION | 35 | 491 | 2 | 0.067 | 0.946 | **0.124** |
| PERSON | 57 | 19 | 2 | 0.750 | 0.966 | 0.844 |

## Diagnosis

Severe over-prediction. Recall is high (catches the entities) but precision collapses, especially on ORGANIZATION (491 false positives). The synthetic generator does not include hard negatives — the model never learns to *not* tag look-alike noun phrases.

## Next iteration

Hard-negative mining (issue #55):
1. Predict on a large corpus of clearly non-PII Japanese text.
2. Treat every emitted span as a labeled `O` (no entity) example.
3. Re-train with the augmented dataset.

Target: adversarial v0.12.0 F1 >= 0.85, ORGANIZATION precision >= 0.5.
