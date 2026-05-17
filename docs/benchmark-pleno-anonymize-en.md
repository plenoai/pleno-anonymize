# `pleno_anonymize_en` — benchmark + methodological accounting

Released at [`0xhikae/pleno_anonymize_en`](https://huggingface.co/0xhikae/pleno_anonymize_en).

EN counterpart of [`pleno_anonymize_ja`](./benchmark-pleno-anonymize-ja.md).
Same recipe template, same eval protocol, same honest-reading guarantees —
read the JP benchmark doc first if you want the protocol rationale; this
doc only reports EN numbers and the diff from JP.

Current shipped artifact: **v0.2.1 tok2vec** (~35MB wheel, no torch /
spacy-transformers). Earlier v0.2.0 was a transformer build that pushed
the server image past fly.io's 8GB rootfs limit and was replaced by the
tok2vec retrain that mirrors the JA recipe.

## TL;DR

| Eval set | F1 | CI 95% | Smoke ≥ 0.50 | Parity ≥ 0.82 |
|---|---:|---|:---:|:---:|
| In-dist (ai4privacy/pii-masking-300k EN val) | **0.973** | — | ✅ | ✅ |
| **Real text (CoNLL-2003 test, PII subset {PER, LOC}, 272 docs)** | **0.574** | [0.521, 0.624] | ✅ | ❌ |
| Real text (CoNLL-2003 test, all 4 categories, 300 docs) | 0.561 | [0.511, 0.610] | ✅ | ❌ |

**Honest reading:** In-distribution dominates as expected. On real
English news (CoNLL-2003 Reuters) the tok2vec build now meets **Smoke**
on both protocols (the prior transformer fell below 0.50). spaCy
`en_core_web_lg` still wins the real-text PII subset (0.666 vs 0.574,
−0.09 F1) — same domain-mismatch story as JP, but the gap has narrowed
from the −0.20 we saw with the transformer.

## Methodology

Training: spaCy tok2vec + ner pipeline (mirroring the JA recipe), seed
42, on the 29,908-row EN train split of `ai4privacy/pii-masking-300k`.
The 28 fine-grained PII categories collapse to 5 super-classes
(`PERSON`, `ADDRESS`, `ORGANIZATION`, `DATE_OF_BIRTH`, `BANK_ACCOUNT`)
to match the SDK/server entity taxonomy. JP uses the same 5-class
collapse.

Real-text eval: char-IoU ≥ 0.5, label-agnostic, 1000-iter document-level
bootstrap with fixed seed=42 for CIs. CoNLL-2003 ships token sequences
with IOB2 tags; we reconstruct text by joining tokens with single spaces
and derive char offsets from the join (standard English detokenisation).
Label mapping: `PERSON → PER`, other tok2vec labels stay as-is.

## In-distribution evaluation

Validation split of `ai4privacy/pii-masking-300k` filtered to
`language == "English"`. F1 = **0.973** (P = 0.972, R = 0.974) reported
by spaCy's training-time scorer on the dev shard; see
`packages/training/output/en/model-best/meta.json` for the raw payload.

**Caveat:** the model was trained on the **train split** of the same
dataset, so this is a supervised-fit number, not a production estimate.
Treat it as an upper bound.

## Real-text evaluation — CoNLL-2003

Real English Reuters news with 4 entity categories: PER, ORG, LOC, MISC.
First 300 test rows that contain ≥1 gold entity (dataset:
`tomaarsen/conll2003`).

Reported two ways: (a) all 4 categories, and (b) restricted to the
**PII-relevant subset** `{PER, LOC}`. ORG and MISC are out of scope for a
PII NER by design.

| Model | Subset | F1 | F1 95% CI | P | R |
|---|---|---:|---|---:|---:|
| **spaCy `en_core_web_lg`** | All 4 | **0.712** | [0.678, 0.746] | 0.611 | 0.852 |
| spaCy `en_core_web_lg` | PII (PER+LOC) | 0.666 | [0.627, 0.704] | 0.542 | 0.863 |
| **`pleno_anonymize_en` v0.2.1** | PII (PER+LOC) | **0.574** | [0.521, 0.624] | 0.675 | 0.499 |
| `pleno_anonymize_en` v0.2.1 | All 4 | 0.561 | [0.511, 0.610] | 0.724 | 0.458 |
| `pleno_anonymize_en` v0.2.0 (prior transformer) | PII (PER+LOC) | 0.470 | [0.403, 0.542] | 0.682 | 0.358 |

The tok2vec retrain trails `en_core_web_lg` by 0.09 F1 on the real-text
PII subset (vs −0.20 for the prior transformer build). It reflects:

1. **Domain mismatch.** Trained on form-/record-/chat-style PII text
   (ai4privacy generation methodology). CoNLL Reuters news is a very
   different distribution. The model favours precision (0.68) over
   recall (0.50) because it was tuned on dense, fully-labelled PII
   examples and is conservative on sparse news prose.
2. **Schema mismatch.** Trained on the 5 PII super-classes
   (PERSON, ADDRESS, ORGANIZATION, DATE_OF_BIRTH, BANK_ACCOUNT). CoNLL
   `LOC` covers geopolitical entities (countries, cities) which our
   `ADDRESS` class does not target; gold LOC entities under the
   strict label match score as misses.
3. **spaCy's home turf.** `en_core_web_lg` was trained on OntoNotes
   (news + web), and CoNLL Reuters is close in style.

**A truly fair real-text PII eval would use hand-annotated EN
chat/form/email.** That dataset doesn't exist publicly. Building one
(~50–100 samples) is the highest-priority follow-up, identical to the
JP card's open item.

The CoNLL result should be read as: "on news prose, with a schema
mismatch, this model trails spaCy by 0.09 F1". It is not a verdict on
PII performance in production PII contexts.

## Acceptance tiers — final read

| Tier | F1 floor | In-dist | Real (PII) | Real (full) |
|---|---:|:---:|:---:|:---:|
| Smoke | 0.50 | ✅ | ✅ | ✅ |
| Parity | 0.82 | ✅ | ❌ | ❌ |
| Stretch | 0.88 | ✅ | ❌ | ❌ |

**Smoke met across the board (was ❌ for real-text in v0.2.0). Parity
still only met in-distribution.**

Production deployment expectations should be calibrated to the
real-text number (~0.57), not the in-distribution number (~0.97).

## Reproducibility

All scripts in `packages/training/scripts/`:
- `train_supervised_300k_en.py` (spaCy tok2vec, seed pinned)
- `eval_conll_en_spacy.py --model pleno_anonymize_en --dataset tomaarsen/conll2003` (real-text)
- `eval_conll_en_spacy.py --model en_core_web_lg --dataset tomaarsen/conll2003` (spaCy baseline)
- `package_anonymize_model.py --model output/en/model-best --language en --version 0.2.1 --build-wheel` (build wheel)

Run via Makefile:

```bash
make -C packages/training train-supervised-en          # RunPod GPU recommended
make -C packages/training eval-300k-en
```

## What's still open

- ❌ **No PII-context real-text eval.** CoNLL is real but off-domain.
  ≥50 hand-annotated EN chat/form/email samples would resolve this.
  Highest-priority follow-up, same as JP.
- ❌ Multi-seed run not done (JP shipped 3-seed mean ± std; EN is
  single-seed seed=42 only).
- ❌ Library versions not pinned in `pyproject.toml`.
- ❌ Only one classic baseline (spaCy `en_core_web_lg`). Stanford NER
  or `en_core_web_trf` would be natural #2 / #3.

The single highest-impact follow-up is hand-annotating ≥50 real EN PII
samples. Until then, real-text production performance is estimated from
the CoNLL result with the caveat that PII-context is a different
distribution.
