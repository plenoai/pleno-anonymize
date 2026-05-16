"""Push a trained NER model to Hugging Face Hub with a model card."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

MODEL_README = """\
---
language: ja
license: apache-2.0
pipeline_tag: token-classification
tags:
  - pii
  - japanese
  - simula
base_model: cl-tohoku/bert-base-japanese-v3
---

# ja_ner_ja-v2

Japanese PII NER fine-tuned on [plenoai/pii-masking-jp-mechanism-v1][ds],
a synthetic dataset built via the [Simula-style mechanism-design pipeline][doc].

[ds]: https://huggingface.co/datasets/plenoai/pii-masking-jp-mechanism-v1
[doc]: https://github.com/plenoai/pleno-anonymize/blob/main/docs/mechanism-design.md

## Use

```python
from transformers import AutoModelForTokenClassification, AutoTokenizer
tokenizer = AutoTokenizer.from_pretrained("plenoai/ja_ner_ja-v2")
model = AutoModelForTokenClassification.from_pretrained("plenoai/ja_ner_ja-v2")
```

## Labels

`PERSON`, `ADDRESS`, `ORGANIZATION`, `DATE_OF_BIRTH`, `BANK_ACCOUNT`,
`PHONE_NUMBER`, `MY_NUMBER`, `MY_NUMBER_CORPORATE`, `CREDIT_CARD`,
`PASSPORT`, `DRIVER_LICENSE`, `HEALTH_INSURANCE`, `RESIDENCE_CARD`,
`POSTAL_CODE`, `EMAIL_ADDRESS`, `IP_ADDRESS`, `URL`.

Encoded as BIO (`B-PERSON`, `I-PERSON`, …) over `cl-tohoku/bert-base-japanese-v3`.

## Benchmarks

See the latest entry in [docs/benchmark.md][bm] for the
`0xhikae/pii-masking-300k-ja` validation comparison (n=300, char-IoU ≥ 0.5,
label-agnostic).

[bm]: https://github.com/plenoai/pleno-anonymize/blob/main/docs/benchmark.md

## Limitations

Trained on synthetic data only. Real-world coverage is bounded by the
taxonomy (35 domains × 63 sub-domains × 382 scenarios). Performance on
out-of-distribution registers (legal Edo-period text, dialect-heavy
SMS, medical OCR) is not guaranteed.
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args()

    from huggingface_hub import HfApi, create_repo

    create_repo(args.repo, exist_ok=True, private=args.private)
    api = HfApi()
    api.upload_folder(repo_id=args.repo, folder_path=str(args.model_dir), repo_type="model")
    api.upload_file(
        path_or_fileobj=MODEL_README.encode("utf-8"),
        path_in_repo="README.md",
        repo_id=args.repo,
        repo_type="model",
    )
    print(f"[done] https://huggingface.co/{args.repo}")


if __name__ == "__main__":
    main()
