"""Push a trained NER model checkpoint to Hugging Face Hub.

Uploads the local checkpoint folder and (optionally) sets visibility.
The README/model card is uploaded separately by the caller — this
script only handles the binary upload so the card can be authored
freely in markdown.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--repo", required=True, help="HF repo id, e.g. 0xhikae/pleno_anonymize_ja")
    parser.add_argument("--private", action="store_true")
    args = parser.parse_args()

    from huggingface_hub import HfApi, create_repo

    create_repo(args.repo, exist_ok=True, private=args.private)
    HfApi().upload_folder(
        repo_id=args.repo,
        folder_path=str(args.model_dir),
        repo_type="model",
    )
    print(f"[done] https://huggingface.co/{args.repo}")


if __name__ == "__main__":
    main()
