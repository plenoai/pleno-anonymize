# `pleno_anonymize_en` — benchmark + methodological accounting

Released at [`0xhikae/pleno_anonymize_en`](https://huggingface.co/0xhikae/pleno_anonymize_en).

> **Note — version mismatch:** this document describes the **v0.2.0 transformer**
> build (`distilbert-base-uncased`, 418MB wheel). The currently-shipped artifact
> is **v0.2.1 tok2vec** (~35MB wheel, mirrors the JA recipe; training-time
> F1=0.973 on the same EN val split). Real-text re-eval against CoNLL-2003 is
> pending — until then, treat the real-text numbers below as upper bounds for
> v0.2.1 (tok2vec typically trails transformer slightly on cross-domain prose).

EN counterpart of [`pleno_anonymize_ja`](./benchmark-pleno-anonymize-ja.md).
Same recipe template, same eval protocol, same honest-reading guarantees —
read the JP benchmark doc first if you want the protocol rationale; this
doc only reports EN numbers and the diff from JP.

## TL;DR

| Eval set | F1 | Seed-42 CI 95% | Smoke ≥ 0.50 | Parity ≥ 0.82 |
|---|---:|---|:---:|:---:|
| In-dist (ai4privacy/pii-masking-300k EN val, 300 docs) | **0.968** | — | ✅ | ✅ |
| **Real text (CoNLL-2003 test, PII subset {PER, LOC}, 272 docs)** | **0.470** | [0.403, 0.542] | ❌ | ❌ |
| Real text (CoNLL-2003 test, all 4 categories, 300 docs) | 0.432 | [0.359, 0.496] | ❌ | ❌ |

**Honest reading:** Same shape as the JP card. In-distribution: dominates.
**On real English news (CoNLL-2003 Reuters), it falls below Smoke.**
spaCy `en_core_web_lg` beats it on the same real-text set (see baselines).

## Methodology

Char-IoU ≥ 0.5, label-agnostic. 1000-iter document-level bootstrap with
fixed seed=42 for CIs. CoNLL-2003 ships token sequences with IOB2 tags;
we reconstruct text by joining tokens with single spaces and derive char
offsets from the join — the standard English detokenisation convention.

Training: 2 epochs, batch 16, lr 5e-5, fp16, seed 42 on
`distilbert-base-uncased` (~66M params, lightweight EN-only) with the
29,908-row EN train split of `ai4privacy/pii-masking-300k`. JP used
`xlm-roberta-base` because it needs cross-lingual coverage; EN only needs
English, so the smaller distilbert is the natural pick and the artefact
is ~half the size.

## In-distribution evaluation

Validation split of `ai4privacy/pii-masking-300k` filtered to `language == "English"`, 300 docs.

| Model | F1 | P | R | Latency (CPU) |
|---|---:|---:|---:|---:|
| `builtin` v0.13.0 | 0.319 | 0.386 | 0.272 | 53 ms |
| `openai-privacy-filter` v0.13.0 | 0.847 | 0.915 | 0.788 | 2.2 s |
| **`pleno_anonymize_en` (seed 42)** | **0.968** | 0.955 | 0.982 | 19 ms |

**Caveat:** the model was trained on the **train split** of the same
dataset (`ai4privacy/pii-masking-300k`, language=English). Read this as
"supervised fit on the methodology", not production performance.

## Real-text evaluation — CoNLL-2003

Real English Reuters news with 4 entity categories: PER, ORG, LOC, MISC.
First 300 test rows that contain ≥1 gold entity.

Reported two ways: (a) all 4 categories, and (b) restricted to the
**PII-relevant subset** `{PER, LOC}`. ORG and MISC are out of scope for a
PII NER by design.

| Model | Subset | F1 | F1 95% CI | P | R |
|---|---|---:|---|---:|---:|
| **spaCy `en_core_web_lg`** | All 4 | **0.712** | [0.678, 0.746] | 0.611 | 0.852 |
| spaCy `en_core_web_lg` | PII (PER+LOC) | 0.666 | [0.627, 0.704] | 0.542 | 0.863 |
| **`pleno_anonymize_en`** | PII (PER+LOC) | **0.470** | [0.403, 0.542] | 0.682 | 0.358 |
| `pleno_anonymize_en` | All 4 | 0.432 | [0.359, 0.496] | 0.702 | 0.312 |

**This model loses to spaCy by 0.20 F1 on the real-text PII subset.**
Same shape as the JP result. It reflects:

1. **Domain mismatch.** Trained on form-/record-/chat-style PII text
   (ai4privacy generation methodology). CoNLL Reuters news is a very
   different distribution. The model favours precision (0.68) over
   recall (0.36) because it was tuned on dense, fully-labelled PII
   examples and is overly conservative on sparse news prose.
2. **Schema mismatch.** Trained to find ~28 fine PII categories
   (phones, emails, postcodes, ID numbers, addresses). CoNLL's `ORG`
   and `MISC` overlap almost nothing with that schema. The PII-subset
   numbers above already restrict to the overlap.
3. **spaCy's home turf.** `en_core_web_lg` was trained on OntoNotes
   (news + web), and CoNLL Reuters is close in style.

**A truly fair real-text PII eval would use hand-annotated EN
chat/form/email.** That dataset doesn't exist publicly. Building one
(~50–100 samples) is the highest-priority follow-up, identical to the
JP card's open item.

The CoNLL result should be read as: "on news prose, with a non-PII
schema, this model trails spaCy". It is not a verdict on PII
performance in production PII contexts.

## Acceptance tiers — final read

| Tier | F1 floor | In-dist | Real (PII) | Real (full) |
|---|---:|:---:|:---:|:---:|
| Smoke | 0.50 | ✅ | ❌ | ❌ |
| Parity | 0.82 | ✅ | ❌ | ❌ |
| Stretch | 0.88 | ✅ | ❌ | ❌ |

**Smoke and Parity met in-distribution. Real-text performance is below
Smoke even on PII-relevant categories.**

Production deployment expectations should be calibrated to the
real-text number (~0.47), not the in-distribution number (~0.97).

## Reproducibility

All scripts in `packages/training/scripts/`:
- `dump_pii_300k.py --language English` (re-dump the EN slice)
- `train_supervised_300k_en.py` (seed pinned; distilbert default)
- `eval_on_300k.py --dataset ai4privacy/pii-masking-300k --language English` (in-dist)
- `eval_conll_en_real.py --dataset tomaarsen/conll2003` (real-text)
- `eval_conll_en_spacy.py --model en_core_web_lg` (spaCy baseline)

Run via Makefile:

```bash
make -C packages/training dump-supervised-en
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
