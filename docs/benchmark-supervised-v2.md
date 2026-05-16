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

## Headline

| Engine | F1 | Precision | Recall | Latency/doc (CPU) |
|---|---:|---:|---:|---:|
| `builtin` v0.13.0 | 0.342 | 0.453 | 0.275 | 55 ms |
| `ja_ner_ja-v2-mechanism` (v1, synthetic only) | 0.352 | 0.612 | 0.247 | 37 ms |
| `openai-privacy-filter` v0.13.0 | 0.702 | 0.899 | 0.576 | 2.3 s |
| **`ja_ner_ja-v2-supervised`** | **0.956** | **0.931** | **0.982** | **43 ms** |

Acceptance tiers from [`SKILL.md`](../.claude/skills/ner-improve/SKILL.md):
Smoke 0.50 ✅ · Parity 0.82 ✅ · Stretch 0.88 ✅.

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
