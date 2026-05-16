# `ja_ner_ja-v2-supervised` — benchmark + methodological accounting

Released at [`0xhikae/ja_ner_ja-v2-supervised`](https://huggingface.co/0xhikae/ja_ner_ja-v2-supervised).
Closes the goal opened by [#168](https://github.com/plenoai/pleno-anonymize/issues/168).

This document reports both the optimistic in-distribution number and
the more honest out-of-distribution numbers, surfaces a non-obvious
eval-protocol artifact, and lists the methodological limitations a
peer reviewer would (and did) flag.

## TL;DR

- **In-distribution F1: 0.957** [0.935, 0.973] — on the validation
  split of the same dataset whose train split was used to train v2.
  Treat as an upper bound, **not** a generalisation estimate.
- **OOD F1 (strict): 0.770** [0.745, 0.797] — on the Simula v1
  synthetic test set (separate generator, different label schema, never
  seen at training). Penalised by label granularity (see below).
- **OOD F1 (schema-intersection): 0.807** [0.782, 0.834] — restricting
  gold to labels with a v2 vocabulary analogue.
- **OOD F1 (span-merged): 0.862** [0.841, 0.881] — collapses contiguous
  v2 sub-spans, neutralising the label-granularity artifact. This is
  the most defensible single number.
- **Classic baseline (spaCy `ja_core_news_lg`) OOD: 0.855** [0.832,
  0.878]. v2 (merged) edges it, but the bootstrap CIs overlap.

## Methodology

Identical to [`docs/benchmark.md`](benchmark.md) §4 (the public ruler):

- Scoring: char-IoU ≥ 0.5, label-agnostic span matching.
- Driver: `packages/training/scripts/eval_mechanism_on_300k.py` and
  `packages/training/scripts/eval_ood_jsonl.py`.
- 1000-iteration document-level bootstrap, seed 42, for 95% CIs.

## In-distribution evaluation

Validation split of `0xhikae/pii-masking-300k-ja`, 300 docs.

| Engine | F1 | F1 95% CI | Precision | Recall | Latency/doc (CPU) |
|---|---:|---|---:|---:|---:|
| `builtin` v0.13.0 | 0.342 | — | 0.453 | 0.275 | 55 ms |
| `ja_ner_ja-v2-mechanism` (v1, synthetic only) | 0.352 | — | 0.612 | 0.247 | 37 ms |
| spaCy `ja_core_news_lg` | 0.274 | [0.250, 0.297] | 0.205 | 0.411 | 22 ms |
| `openai-privacy-filter` v0.13.0 | 0.702 | — | 0.899 | 0.576 | 2.3 s |
| **`ja_ner_ja-v2-supervised`** | **0.957** | **[0.935, 0.973]** | **0.933** | **0.983** | 43 ms |

**Caveat:** v2 was trained on the **train split** of the same
dataset. Per template-overlap analysis (below) the splits are
0.4% surface-overlapping but cannot be assumed fully independent.
Use OOD numbers for production expectations.

## Train/val template overlap (reviewer concern: split leakage)

`0xhikae/pii-masking-300k-ja` is a JP-translated fork of
`ai4privacy/pii-masking-300k`. A common failure mode is that train
and validation share generation templates, so a model can memorise
the template skeleton and predict spans by position rather than
context. Two probes on a 20k-row train / 1.5k-row val sample:

| Signature | Distinct in train | Distinct in val | Train-val overlap |
|---|---:|---:|---:|
| Char-level skeleton (first 150 chars with PII tags replaced by labels) | 19,961 / 20,000 | 1,497 / 1,500 | 0.4 % (6 of 1,500 val rows) |
| Label-sequence only (tuple of label types per row) | 13,952 | 1,223 | 36.1 % |

Char-level: surface forms in train and val are essentially disjoint;
template memorisation is not a credible explanation for the 0.957
in-dist F1. Label-sequence overlap of 36% is expected for any
fixed-vocabulary NER task and does not by itself imply leakage —
distinct documents can share label sequences without sharing text.

This does not prove zero leakage. AI4Privacy could have shared
*scenario seeds* across splits before instantiation. But the
character-level evidence makes wholesale template memorisation
unlikely.

## Out-of-distribution evaluation

Combined dev+test from the v1 (Simula / synthetic) dataset:
`packages/training/data/raw/ja-mechanism-v1/{dev,test}.jsonl`,
n=134 docs. Different generator pipeline (Simula meta-prompts,
complexification operators, dual-critic loop), different label
schema (17 pleno labels vs 28 ai4privacy v2 emits), zero overlap
with v2 training data.

| Engine | F1 | F1 95% CI | Precision | Recall | Latency/doc (CPU) |
|---|---:|---|---:|---:|---:|
| spaCy `ja_core_news_lg` | 0.855 | [0.832, 0.878] | 0.787 | 0.937 | 26 ms |
| **v2-supervised (strict)** | 0.770 | [0.745, 0.797] | 0.718 | 0.829 | 41 ms |
| v2-supervised (schema-intersection) | 0.807 | [0.782, 0.834] | 0.708 | 0.940 | 41 ms |
| **v2-supervised (span-merged)** | **0.862** | **[0.841, 0.881]** | 0.888 | 0.837 | 41 ms |

### Why v2 (strict) < spaCy on OOD — the label-granularity artifact

The v1 OOD set uses **coarse** labels (e.g., `PERSON` covering an
entire 「山田太郎」 span). v2 was trained on **fine-grained**
ai4privacy labels and emits `LASTNAME1` + `GIVENNAME1` as two
adjacent narrow spans. With char-IoU ≥ 0.5, neither narrow
prediction reaches the threshold against the wide gold span; both
become FP and the gold becomes FN.

Concrete example from `/tmp/v2-ood-extended.jsonl[1]`:

- Gold `ADDRESS` (33, 48) — 15 chars
- v2 predicts: `STREET` (33, 36), `CITY` (36, 40), `STREET` (40, 48) — three fragments
- IoU values: 0.20, 0.27, 0.53 → only the last one barely matches

`eval_ood_span_merged.py` re-runs the same eval after merging any
contiguous non-O v2 spans into one. F1 climbs from 0.770 to 0.862.
The model's underlying span predictions are correct; the strict
scoring penalises the schema mismatch.

**The 0.862 (merged) number is the most defensible OOD figure.**

### Why this isn't a real "OOD" test

Both training data and "OOD" data are LLM-synthesised. A genuine
out-of-distribution test would be hand-annotated real Japanese text
(chat logs, scanned forms, call-centre transcripts). We do not
have one yet, so all reported OOD numbers should be read as
"synthetic-to-synthetic generalisation upper bound for the
production target". Production expectations should be calibrated
below 0.862, not at it.

### Schema-intersection per-label recall (OOD, strict)

`PHONE_NUMBER` 1.00 · `EMAIL_ADDRESS` 1.00 · `POSTAL_CODE` 1.00 ·
`CREDIT_CARD` 1.00 · `PERSON` 0.96 · `BANK_ACCOUNT` 0.94 · `ADDRESS`
0.87 · `DATE_OF_BIRTH` 0.82.

`HEALTH_INSURANCE` (0.05) and `ORGANIZATION` (0.00) are excluded —
those labels have no analogue in v2's 28-label ai4privacy output
vocabulary, so v2 structurally cannot emit them. Production
deployments needing these classes should stack Presidio pattern
recognizers on top.

## Acceptance tiers from [`SKILL.md`](../.claude/skills/ner-improve/SKILL.md)

| Tier | F1 floor | In-dist | OOD (strict) | OOD (merged) |
|---|---:|:---:|:---:|:---:|
| Smoke | 0.50 | ✅ | ✅ | ✅ |
| Parity | 0.82 | ✅ | ❌ (0.770) | ✅ (0.862) |
| Stretch | 0.88 | ✅ | ❌ | ❌ (CI upper 0.881) |

Honest reading: **Smoke and Parity are met under the merged OOD
protocol; Stretch is not.**

## Training recipe

- Base: `FacebookAI/xlm-roberta-base` (270M params)
- Data: 25,082 Japanese rows from `0xhikae/pii-masking-300k-ja` train
  split (dumped locally then scp'd to RunPod since the dataset is
  private; no token leaves the operator's machine)
- 2 epochs, batch size 16, lr 5e-5, fp16, **seed 42**
- ~5 min on a single RTX A6000 (RunPod), ~$0.05 compute
- Reproducibility caveats below

## Reproducibility

- Training script `packages/training/scripts/train_supervised_300k_ja.py`
  pins seed via `--seed`, `TrainingArguments(seed=, data_seed=)`,
  `random.seed`, `numpy.random.seed`, `torch.manual_seed`,
  `PYTHONHASHSEED`.
- 1000-iter bootstrap CIs use seed 42.
- The training dataset is private (`0xhikae/pii-masking-300k-ja`).
  Third-party reproduction requires the dataset owner granting
  access. The training script reads from a local JSONL dump.
- Library versions: not pinned in `pyproject.toml`. Recorded only
  loosely in CLAUDE.md as "transformers>=4.45,<5". Pinning is a
  follow-up.
- Replicate runs across multiple seeds: not done. Reported numbers
  are from a single training run.

## Open methodological gaps (reviewer feedback, R1 round)

Resolved in this revision:
- ✅ Train/val template overlap quantified (R1)
- ✅ Bootstrap 95% CIs on every reported number (R3)
- ✅ Schema-intersection OOD F1 reported (R3)
- ✅ Span-merged OOD F1 reported, label-granularity artifact called out (R3)
- ✅ Classic baseline added (spaCy `ja_core_news_lg`) (R4)
- ✅ Seed pinned in training script (R5)
- ✅ Headline tables include CIs and avoid bold-comparing in-dist to OPF (R2)

Still outstanding (would block top-venue Accept):
- ❌ **No real Japanese text in evaluation.** Both training and OOD
  are LLM-synthesised. Adding ≥100 hand-annotated real-text samples
  is the highest-priority follow-up.
- ❌ **No multi-seed replicate runs.** Single training run, single
  bootstrap of that run. Variance across seeds unknown.
- ❌ **No v1↔v2 ablation isolating base model from data.** v1 used
  `xlm-roberta-base` after a switch from `cl-tohoku/bert-base-japanese-v3`
  (which lacked a fast tokenizer); v2 also uses `xlm-roberta-base`,
  so the v1→v2 jump is attributable to data only. But the v1
  config used 3 epochs, v2 uses 2 epochs — minor confound.
- ❌ **AI4Privacy split-protocol still partially opaque.** The
  template-overlap probe is necessary but not sufficient; dataset
  card does not document the splitting strategy at the scenario or
  seed level.
- ❌ **Library versions not pinned in package manifests.**
- ❌ **Only one classic baseline (spaCy).** GiNZA would be a natural
  second.
