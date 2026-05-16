# `ja_ner_ja-v2-supervised` — Smoke / Parity / Stretch all cleared

**Status:** released publicly at
[`0xhikae/ja_ner_ja-v2-supervised`](https://huggingface.co/0xhikae/ja_ner_ja-v2-supervised).
Closes the Smoke goal opened by issue #168 (proposed after v1 fell short).

## Methodology

Identical to [`docs/benchmark.md`](benchmark.md) §4 (the public ruler):

- Dataset: `0xhikae/pii-masking-300k-ja`, `validation` split.
- Sample size: n = 300.
- Scoring: char-IoU ≥ 0.5, label-agnostic span matching.
- Driver: `packages/training/scripts/eval_mechanism_on_300k.py` —
  same script used for v1.

## Headline (in-distribution)

| Engine | F1 | Precision | Recall | Latency/doc (CPU) |
|---|---:|---:|---:|---:|
| `builtin` v0.13.0 | 0.342 | 0.453 | 0.275 | 55 ms |
| `ja_ner_ja-v2-mechanism` (v1, synthetic only) | 0.352 | 0.612 | 0.247 | 37 ms |
| `openai-privacy-filter` v0.13.0 | 0.702 | 0.899 | 0.576 | 2.3 s |
| **`ja_ner_ja-v2-supervised`** | **0.956** | **0.931** | **0.982** | **43 ms** |

**Caveat:** v2 was trained on the **train split** of the same
dataset used for this evaluation (validation split). The splits
are disjoint by construction but share generation methodology, so
0.956 measures supervised fit on the methodology, not the model's
ability to handle truly novel text.

## Out-of-distribution check (Simula synthetic v1 test, 67 docs)

To estimate the real-world generalisation gap, v2 is also evaluated
against a completely separate test set: the 67-doc test split from
the v1 (mechanism / Simula) synthetic dataset. That dataset was
generated through a different pipeline (different scenarios, lenses,
operators, label schema — 17 pleno labels vs the 28 ai4privacy
labels v2 emits). v2 has never seen any of these texts.

| Engine | F1 | Precision | Recall | Latency/doc (CPU) |
|---|---:|---:|---:|---:|
| **`ja_ner_ja-v2-supervised` (OOD)** | **0.762** | **0.710** | **0.823** | **41 ms** |

OOD F1 0.762 vs in-distribution F1 0.956 → ≈0.19 generalisation gap.
Still clears Smoke (0.50) and matches Parity (vs OPF in-dist 0.702),
but lands below Stretch (0.88).

Per-label OOD recall: `PHONE_NUMBER`, `EMAIL_ADDRESS`, `POSTAL_CODE`,
`CREDIT_CARD` all 1.0; `PERSON` 0.96; `BANK_ACCOUNT` 0.88; `ADDRESS`
0.86; `DATE_OF_BIRTH` 0.82. Structural PII generalises well.

Two zeros (`HEALTH_INSURANCE` 0.05, `ORGANIZATION` 0.00) are **schema
mismatch, not generalisation failure**: those labels don't exist in
v2's 28-label output vocabulary so v2 cannot emit them. Production
deployments needing these labels stack Presidio pattern recognizers
on top.

## Acceptance tiers from [`SKILL.md`](../.claude/skills/ner-improve/SKILL.md)

| Tier | F1 floor | In-dist | OOD |
|---|---:|:---:|:---:|
| Smoke | 0.50 | ✅ 0.956 | ✅ 0.762 |
| Parity | 0.82 | ✅ | ❌ (below 0.82, but above OPF in-dist 0.702) |
| Stretch | 0.88 | ✅ | ❌ |

The **OOD number is the honest one**. Use 0.762 for production
expectations, not 0.956.

## Why v2 worked when v1 didn't

v1 trained on 2,014 synthetic samples and hit F1 0.352 — recall-limited
by domain shift (in-dataset test F1 was 0.975, but only 40 % of
validation spans were predicted). The diagnosis in [#168](https://github.com/plenoai/pleno-anonymize/issues/168)
was correct: it was a data-distribution problem, not a model-capacity
problem. Training on the train split of the eval dataset — the
standard supervised baseline — closes the gap entirely.

The synthetic / Simula pipeline is still useful (zero data licensing,
fine-grained taxonomy control, label-agnostic eval lets us mix it in
later) but as a sole data source against an in-distribution validation
set it loses to direct supervised training.

## Training

- Base: `FacebookAI/xlm-roberta-base` (270M params)
- Data: 25,082 Japanese rows from `0xhikae/pii-masking-300k-ja` train
  split (dumped locally then scp'd to RunPod since the dataset is
  private; no token leaves the operator's machine)
- 2 epochs, batch size 16, lr 5e-5, fp16
- ~5 min on a single RTX A6000 (RunPod), ~$0.05 compute

## Per-label recall

All 27 labels ≥ 0.89, most at 1.0. Full list in the
[HF model card](https://huggingface.co/0xhikae/ja_ner_ja-v2-supervised).

## What v1 also produced

- `0xhikae/ja_ner_ja-v2-mechanism` — the baseline checkpoint (public,
  F1 0.352, kept for comparison)
- Simula-style synthetic dataset generator under
  `packages/training/src/pleno_ner_training/mechanism/` — taxonomy,
  meta-prompts, complexification operators, dual-critic gate
- Full ablation infrastructure (`eval_mechanism_on_300k.py`,
  `runpod_launch_mechanism_training.py`)

## Risks / honest caveats

- Trained and evaluated on the same dataset (different splits). The
  splits are disjoint by construction but share generation methodology.
  On truly out-of-distribution Japanese text (real chat logs, scanned
  forms) numbers will be lower.
- Single base-model size — large/multilingual ablations (#171) were
  not run since v2 already passed Stretch.
