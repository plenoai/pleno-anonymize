"""Train the mechanism-v1 NER model.

Runs locally on CPU for smoke (--smoke-rows 200) or on RunPod GPU
(orchestrated separately via mcp__runpod__* MCP tools).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pleno_ner_training.mechanism.train import train  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw/ja-mechanism-v1"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/ja-ner-mechanism-v1"))
    parser.add_argument("--base-model", default="cl-tohoku/bert-base-japanese-v3")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--eval-only", action="store_true")
    args = parser.parse_args()

    metrics = train(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        base_model=args.base_model,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        eval_only=args.eval_only,
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
