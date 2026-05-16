# RunPod training for `ja_ner_ja` v2 (mechanism-v1)

This doc walks through running the Simula 6/8 fine-tune (#153) on RunPod
via the `mcp__runpod__*` MCP tools.

## Prerequisites

| Requirement | How |
|---|---|
| RunPod account + API key | `mcp__runpod__*` tools must already be configured |
| HF account + token with write access to `plenoai/` | dataset/model push at #155 |
| `data/raw/ja-mechanism-v1/` generated | run `make generate-mechanism-v1` (#152) first |

## Self-contained pod recipe

The pod is self-contained: it pulls the dataset from HF, trains, and
pushes the model back to HF. No file upload from this machine.

### 1. Push the dataset to HF (pre-step shared with #155)

```bash
cd packages/training
uv run --extra training --extra hf python scripts/push_dataset_to_hf.py \
    --data-dir data/raw/ja-mechanism-v1 \
    --repo plenoai/pii-masking-jp-mechanism-v1 \
    --private
```

### 2. Create the training pod

```python
mcp__runpod__create-pod(
    name="pleno-ja-ner-v2",
    imageName="runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
    gpuCount=1,
    gpuTypeIds=["NVIDIA GeForce RTX 4090"],
    cloudType="COMMUNITY",
    containerDiskInGb=40,
    volumeInGb=20,
    volumeMountPath="/workspace",
    env={
        "HF_TOKEN": "<write token>",
        "HF_DATASET": "plenoai/pii-masking-jp-mechanism-v1",
        "HF_MODEL": "plenoai/ja_ner_ja-v2",
        "BASE_MODEL": "cl-tohoku/bert-base-japanese-v3",
        "EPOCHS": "3",
        "STARTUP_CMD": "git clone https://github.com/plenoai/pleno-anonymize.git \
            && cd pleno-anonymize/packages/training \
            && pip install -e .[training,hf] \
            && python -c 'from datasets import load_dataset; load_dataset(\"$HF_DATASET\").save_to_disk(\"data/hf\")' \
            && python scripts/train_mechanism.py \
                --data-dir data/hf --output-dir output/ja-ner-mechanism-v1 \
                --base-model $BASE_MODEL --epochs $EPOCHS \
            && python scripts/push_model_to_hf.py \
                --model-dir output/ja-ner-mechanism-v1/model-best \
                --repo $HF_MODEL",
    },
    ports=["22/tcp"],
)
```

### 3. Monitor

```python
mcp__runpod__get-pod(podId="<id>", includeMachine=True)
```

The pod's stdout is on the RunPod dashboard. The pod self-terminates
when training completes (the startup command's last step is the model
push).

### 4. Cleanup

```python
mcp__runpod__delete-pod(podId="<id>")
```

## Cost estimate

| Configuration | Hourly | Wall-clock | Total |
|---|---:|---:|---:|
| RTX 4090 + 5k samples, 3 epochs | $0.34 | ~15 min | ~$0.10 |
| RTX 4090 + 30k samples, 3 epochs | $0.34 | ~90 min | ~$0.50 |
| A100 80GB + 30k samples, 5 epochs | $1.89 | ~30 min | ~$1.00 |

## Why not local

`CLAUDE.md` prohibits local training. M-series Macs can train this
model in ~30 min on MPS but: (a) blocks the dev machine, (b) prevents
reproducibility, (c) `xlm-roberta` Sliding Window attention requires
flash-attn which is CUDA-only.

## Failure modes

| Symptom | Fix |
|---|---|
| Pod stuck at `Creating` > 5 min | RunPod region saturated — switch `cloudType` to `SECURE` or another `gpuTypeIds` |
| OOM on RTX 4090 with bert-base | drop `batch_size` to 8 |
| HF push 403 | `HF_TOKEN` must have **write** scope, not just read |
