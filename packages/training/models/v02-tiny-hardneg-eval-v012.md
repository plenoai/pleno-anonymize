# hf-ja-v02-tiny-hardneg: v0.12.0 adversarial evaluation

Trained: 2026-05-03 on RunPod RTX A5000 (~13 min, 20 epochs, 10580 steps).
Pod: `hbkmdx4jjywii6` (terminated).

Base model: `ku-nlp/deberta-v2-tiny-japanese`
Train data: `raw_ja_v02_generated.json` with `--include-negatives`
- 8533 positive docs + 2032 hard-neg docs (entities=[] kept as all-O)
- 400 zero-entity docs dropped by PII heuristic (label-miss noise)

Hyperparams: lr=3e-5, bs=16, warmup=0.15, weight_decay=0.01, fp16

## Metrics

| split | precision | recall | F1 |
| --- | --- | --- | --- |
| dev (in-domain) | 0.892 | 0.942 | 0.917 |
| test (in-domain) | 0.894 | 0.939 | 0.916 |
| **adversarial v0.12.0** | 0.263 | 0.989 | **0.416** |

## Comparison vs baseline (v02-tiny without hard-neg)

| metric | baseline | hardneg | delta |
| --- | --- | --- | --- |
| dev F1 | 0.929 | 0.917 | -0.012 |
| **adversarial F1** | 0.374 | 0.416 | **+0.042** |
| adversarial precision | 0.231 | 0.263 | +0.032 |
| adversarial recall | 0.979 | 0.989 | +0.010 |

## Per-label on v0.12.0 (hardneg)

| label | TP | FP | FN | P | R | F1 |
| --- | --- | --- | --- | --- | --- | --- |
| ADDRESS | 45 | 45 | 0 | 0.500 | 1.000 | 0.667 |
| BANK_ACCOUNT | 24 | 38 | 0 | 0.387 | 1.000 | 0.558 |
| DATE_OF_BIRTH | 23 | 32 | 0 | 0.418 | 1.000 | 0.590 |
| ORGANIZATION | 36 | 390 | 1 | 0.085 | 0.973 | 0.156 |
| PERSON | 58 | 15 | 1 | 0.795 | 0.983 | 0.879 |

## Diagnosis

Including 2032 zero-entity docs nudged precision from 0.231 → 0.263 (+14% relative)
but did not break the over-prediction problem. ORGANIZATION still emits 390 false
positives — the in-corpus negatives are too easy and don't contain the look-alike
noun phrases that the adversarial benchmark uses.

## Acceptance vs #48

| AC | target | actual | met |
| --- | --- | --- | --- |
| Overall F1 | ≥ 0.70 | 0.416 | NO |
| Per-entity precision | ≥ 0.70 | only PERSON (0.795) | NO |
| PERSON / ADDRESS recall | ≥ 0.95 | 0.983 / 1.000 | YES |

## Next iteration

Real hard-negative mining (not just including in-corpus negatives):
1. Pull a large clean Japanese corpus (Wikipedia abstracts, JNLI/JSNLI text, news)
2. Predict with current v02-tiny on it
3. Every emitted span becomes a labeled `O` example
4. Re-train

The adversarial v0.12.0 benchmark is built specifically with FP-pressure phrases;
ordinary "no-PII" text in `generated.json` doesn't carry the same signal density.
