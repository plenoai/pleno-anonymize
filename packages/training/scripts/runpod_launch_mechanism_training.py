"""Print the RunPod MCP call needed to launch the v2 training pod.

This script does **not** call the RunPod API itself — it emits a JSON
payload that the operator pastes into the `mcp__runpod__create-pod`
tool, then drops into the calling agent's conversation. Doing it this
way keeps the credential path explicit (HF_TOKEN never leaves the
operator's machine).

Usage:
    python scripts/runpod_launch_mechanism_training.py \\
        --hf-dataset plenoai/pii-masking-jp-mechanism-v1 \\
        --hf-model   plenoai/ja_ner_ja-v2 \\
        --base-model cl-tohoku/bert-base-japanese-v3 \\
        --epochs 3 \\
        --gpu "NVIDIA GeForce RTX 4090"

Then the agent invokes:
    mcp__runpod__create-pod(<paste payload here>)
"""

from __future__ import annotations

import argparse
import json
import textwrap


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hf-dataset", default="plenoai/pii-masking-jp-mechanism-v1")
    parser.add_argument("--hf-model", default="plenoai/ja_ner_ja-v2")
    parser.add_argument("--base-model", default="cl-tohoku/bert-base-japanese-v3")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--gpu", default="NVIDIA GeForce RTX 4090")
    parser.add_argument("--cloud-type", choices=("SECURE", "COMMUNITY"), default="COMMUNITY")
    parser.add_argument("--container-disk", type=int, default=40)
    parser.add_argument("--volume", type=int, default=20)
    args = parser.parse_args()

    startup_cmd = textwrap.dedent(f"""
        set -euo pipefail
        git clone https://github.com/plenoai/pleno-anonymize.git /workspace/repo
        cd /workspace/repo/packages/training
        pip install --upgrade pip
        pip install -e '.[training,hf]'
        export HF_HUB_ENABLE_HF_TRANSFER=1
        python -c "
        import os
        from datasets import load_dataset
        ds = load_dataset(os.environ['HF_DATASET'])
        ds.save_to_disk('data/hf')
        print('Loaded splits:', list(ds.keys()))
        "
        python -c "
        # Re-emit JSONL splits expected by train_mechanism.py
        import json, os
        from datasets import load_from_disk
        ds = load_from_disk('data/hf')
        out_dir = 'data/raw/ja-mechanism-v1'
        os.makedirs(out_dir, exist_ok=True)
        name_map = {{'train': 'train', 'validation': 'dev', 'test': 'test'}}
        for split, fname in name_map.items():
            if split not in ds:
                continue
            with open(f'{{out_dir}}/{{fname}}.jsonl', 'w', encoding='utf-8') as f:
                for row in ds[split]:
                    f.write(json.dumps(row, ensure_ascii=False) + '\\n')
        "
        python scripts/train_mechanism.py \\
            --data-dir data/raw/ja-mechanism-v1 \\
            --output-dir output/ja-ner-mechanism-v1 \\
            --base-model {args.base_model} \\
            --epochs {args.epochs}
        python scripts/push_model_to_hf.py \\
            --model-dir output/ja-ner-mechanism-v1/model-best \\
            --repo {args.hf_model}
        # Self-terminate by exit (Docker default behaviour).
    """).strip()

    payload = {
        "name": "pleno-ja-ner-v2-mechanism",
        "imageName": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
        "gpuCount": 1,
        "gpuTypeIds": [args.gpu],
        "cloudType": args.cloud_type,
        "containerDiskInGb": args.container_disk,
        "volumeInGb": args.volume,
        "volumeMountPath": "/workspace",
        "env": {
            "HF_TOKEN": "<paste your HF token with write access>",
            "HF_DATASET": args.hf_dataset,
            "HF_MODEL": args.hf_model,
            "BASE_MODEL": args.base_model,
            "EPOCHS": str(args.epochs),
            "STARTUP_CMD": startup_cmd,
        },
        "ports": ["22/tcp"],
    }

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("\n# Paste the above payload into mcp__runpod__create-pod.")


if __name__ == "__main__":
    main()
