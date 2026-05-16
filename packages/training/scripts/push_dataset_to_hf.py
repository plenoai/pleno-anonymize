"""Push the mechanism-v1 JSONL dataset to Hugging Face Hub.

Builds a `datasets.DatasetDict` with train/dev/test splits and the
mechanism-v1 schema, attaches a model card, and uploads.

Used by the RunPod training pod and by Simula 8/8 (#155).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DATASET_README = """\
---
language: ja
license: cc-by-4.0
task_categories:
  - token-classification
tags:
  - pii
  - japanese
  - synthetic
  - simula
size_categories:
  - 1K<n<100K
---

# pii-masking-jp-mechanism-v1

A synthetic JP PII dataset generated via the mechanism-design pipeline
described in [docs/mechanism-design.md][1] of the
[plenoai/pleno-anonymize][2] repo. Inspired by Google Research's
[Simula][3] (designing synthetic datasets via mechanism design).

[1]: https://github.com/plenoai/pleno-anonymize/blob/main/docs/mechanism-design.md
[2]: https://github.com/plenoai/pleno-anonymize
[3]: https://research.google/blog/designing-synthetic-datasets-for-the-real-world-mechanism-design-and-reasoning-from-first-principles/

## Schema

Each row:

| Field | Type | Description |
|---|---|---|
| `text` | string | The synthetic JP document |
| `entities` | list of {start, end, label} | Char-offset PII spans |
| `scenario_id` | string | Taxonomy leaf id (e.g. `med.clinical.kanja_chart`) |
| `register` | string | formal / polite / casual / terse |
| `document_type` | string | chat / email / form / transcript / ... |
| `entity_density` | string | sparse / medium / dense |
| `lens` | dict | local-diversification lens |
| `difficulty` | float | [0, 1] heuristic difficulty score |
| `difficulty_bucket` | string | easy / medium / hard |
| `operators_applied` | list[string] | complexification operators applied |

## Generation

Five Simula stages:

1. **Taxonomy** — 35 domains × 63 sub-domains × 382 base scenarios, expanded
   via register / density / document-type variants into 1,910 sampling nodes.
2. **Meta-prompts** — 5 canonical lenses (perspective × length × opening_cue
   × vocabulary × twist) per leaf → 1,910 meta-prompts.
3. **Complexification** — 5 operators (obfuscate / add_ambiguity / code_switch
   / couple_entities / add_near_pii). Target buckets {easy: .5, medium: .3, hard: .2}.
4. **Dual critic** — independent label-correctness + realism critics gate every
   sample; auto-correct path with one retry.
5. **Generation** — `gpt-4o-mini` produces XML-tagged JP text, parsed into
   char-offset spans, then complexified + critiqued.

## Splits

90 / 5 / 5 stratified on `scenario_id`.

## Intended use

Fine-tuning Japanese PII NER models. Not for production deployment without
a human-curated holdout (e.g. AI4Privacy or the upstream pleno held-out set).
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw/ja-mechanism-v1"))
    parser.add_argument("--repo", required=True, help="HF repo id, e.g. plenoai/pii-masking-jp-mechanism-v1")
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args()

    from datasets import Dataset, DatasetDict
    from huggingface_hub import HfApi

    def _load(split: str) -> Dataset:
        path = args.data_dir / f"{split}.jsonl"
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return Dataset.from_list(rows)

    ds = DatasetDict({
        "train": _load("train"),
        "validation": _load("dev"),
        "test": _load("test"),
    })
    print(f"[load] {ds}")

    ds.push_to_hub(args.repo, private=args.private)

    HfApi().upload_file(
        path_or_fileobj=DATASET_README.encode("utf-8"),
        path_in_repo="README.md",
        repo_id=args.repo,
        repo_type="dataset",
    )
    print(f"[done] https://huggingface.co/datasets/{args.repo}")


if __name__ == "__main__":
    main()
