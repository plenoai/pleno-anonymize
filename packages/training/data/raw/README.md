# data/raw — dataset provenance notes

## `en-300k-supervised/`, `ja-300k-supervised/`

Local JSONL dumps of `ai4privacy/pii-masking-300k` (EN split) and
`0xhikae/pii-masking-300k-ja` (JA split, itself derived from the same
upstream dataset), produced by `scripts/dump_pii_300k.py`.

**Evaluation-only. Do not train on these dumps.** The upstream dataset is
licensed non-commercially; training a model on it, or publishing any
derivative model, requires written permission from AI4Privacy. This
project's shipped models (v0.3.0+) are trained on license-clean Faker
synthetic data instead — see
`packages/sdk/src/pleno_anonymize/_models.py`.

`scripts/train_supervised_300k_en.py` and `train_supervised_300k_ja.py`
refuse to run without an explicit `--i-have-written-permission` flag, and
the corresponding `Makefile` targets (`train-supervised-en`,
`train-supervised-ja`) refuse to run without `I_HAVE_WRITTEN_PERMISSION=1`.
Both directories are also git-ignored — the dumps never leave the local
machine or a training pod.

See issue #294.
