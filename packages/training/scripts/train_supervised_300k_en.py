"""Supervised v2 (EN): train on local JSONL dump of ai4privacy/pii-masking-300k EN split.

WARNING: pii-masking-300k is evaluation-only for this project. Training on
it requires written permission from AI4Privacy (non-commercial license).

Mirrors `train_supervised_300k_ja.py` (same loader, same BIO encoding,
same Trainer config, same seed pinning) but defaults to a *lightweight*
English-only backbone — `distilbert-base-uncased` (~66M params, ~265 MB
fp32 / ~130 MB fp16) instead of `xlm-roberta-base` (~270M). The JP
recipe needs cross-lingual coverage; the EN model only needs English,
so the smaller distilbert is the natural lightweight choice and
roughly 3-4x smaller / faster at inference.

Target: ≥0.50 F1 (Smoke tier) on the EN validation split.

The dataset is public; dump locally with `scripts/dump_pii_300k.py`
and scp the JSONL to the pod. No token is needed for the public split.

Usage on RunPod::

    python scripts/train_supervised_300k_en.py \\
        --data-dir data/raw/en-300k-supervised \\
        --base-model distilbert-base-uncased \\
        --output-dir output/en-ner-supervised-v2 \\
        --epochs 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def _bio_labels(text: str, entities, offset_mapping, label2id, ignore_index=-100):
    labels = [label2id["O"]] * len(offset_mapping)
    for start, end, label in entities:
        bkey = f"B-{label}"
        ikey = f"I-{label}"
        if bkey not in label2id:
            continue
        first = True
        for i, (a, b) in enumerate(offset_mapping):
            if a == b == 0:
                labels[i] = ignore_index
                continue
            if a >= start and b <= end:
                labels[i] = label2id[bkey if first else ikey]
                first = False
    for i, (a, b) in enumerate(offset_mapping):
        if a == b == 0:
            labels[i] = ignore_index
    return labels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw/en-300k-supervised"),
                        help="dir containing train.jsonl and dev.jsonl (pre-dumped locally)")
    parser.add_argument("--base-model", default="distilbert-base-uncased",
                        help="HF model id; default = distilbert-base-uncased (lightweight EN)")
    parser.add_argument("--output-dir", type=Path, default=Path("output/en-ner-supervised-v2"))
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--train-limit", type=int, default=0, help="0 = use full train.jsonl")
    parser.add_argument("--i-have-written-permission", action="store_true",
                        help="Confirms you hold written permission from AI4Privacy to train on "
                             "pii-masking-300k (non-commercial license). Required to proceed.")
    args = parser.parse_args()

    if not args.i_have_written_permission:
        print(
            "ERROR: ai4privacy/pii-masking-300k is non-commercially licensed; training on it "
            "(and publishing any derivative model) requires written permission from AI4Privacy. "
            "This project treats the dataset as evaluation-only "
            "(see packages/sdk/src/pleno_anonymize/_models.py). Re-run with "
            "--i-have-written-permission only if you have obtained that permission in writing.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    import os
    import random
    import numpy as _np

    random.seed(args.seed)
    _np.random.seed(args.seed)
    os.environ["PYTHONHASHSEED"] = str(args.seed)
    try:
        import torch as _torch
        _torch.manual_seed(args.seed)
        if _torch.cuda.is_available():
            _torch.cuda.manual_seed_all(args.seed)
    except ImportError:
        pass

    from datasets import Dataset
    from transformers import (
        AutoModelForTokenClassification,
        AutoTokenizer,
        DataCollatorForTokenClassification,
        Trainer,
        TrainingArguments,
    )

    def _read_jsonl(path: Path, limit: int = 0) -> list[dict]:
        rows: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            ents = [(e["start"], e["end"], e["label"]) for e in r["entities"]]
            rows.append({"text": r["text"], "entities": ents})
            if limit and len(rows) >= limit:
                break
        return rows

    print(f"[load] {args.data_dir}/train.jsonl")
    train_rows = _read_jsonl(args.data_dir / "train.jsonl", args.train_limit)
    print(f"[load] train: {len(train_rows)} rows")
    print(f"[load] {args.data_dir}/dev.jsonl")
    dev_rows = _read_jsonl(args.data_dir / "dev.jsonl")
    print(f"[load] dev: {len(dev_rows)} rows")

    labels = sorted({e[2] for r in train_rows + dev_rows for e in r["entities"]})
    bio = ["O"] + [f"{p}-{l}" for l in labels for p in ("B", "I")]
    label2id = {b: i for i, b in enumerate(bio)}
    id2label = {i: b for b, i in label2id.items()}
    print(f"[labels] {len(labels)} entity labels, {len(bio)} BIO tags")

    print(f"[load] tokenizer {args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    if not getattr(tokenizer, "is_fast", False):
        raise RuntimeError("Need a fast tokenizer with offset mapping")

    def encode(rows):
        out_rows = []
        for r in rows:
            enc = tokenizer(
                r["text"],
                return_offsets_mapping=True,
                truncation=True,
                max_length=512,
                padding=False,
            )
            lab = _bio_labels(r["text"], r["entities"], enc["offset_mapping"], label2id)
            out_rows.append({
                "input_ids": enc["input_ids"],
                "attention_mask": enc["attention_mask"],
                "labels": lab,
            })
        return Dataset.from_list(out_rows)

    print("[encode] train")
    train_ds = encode(train_rows)
    print("[encode] dev")
    dev_ds = encode(dev_rows)

    model = AutoModelForTokenClassification.from_pretrained(
        args.base_model,
        num_labels=len(bio),
        id2label=id2label,
        label2id=label2id,
    )

    targs = TrainingArguments(
        output_dir=str(args.output_dir),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        learning_rate=args.lr,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        greater_is_better=True,
        logging_steps=100,
        save_total_limit=2,
        report_to=[],
        fp16=True,
        seed=args.seed,
        data_seed=args.seed,
    )

    def compute_metrics(eval_preds):
        from seqeval.metrics import f1_score, precision_score, recall_score
        logits, lab = eval_preds
        preds = np.argmax(logits, axis=-1)
        true_l, true_p = [], []
        for ps, ls in zip(preds, lab):
            tl, tp = [], []
            for p, l in zip(ps, ls):
                if l == -100:
                    continue
                tl.append(id2label[int(l)])
                tp.append(id2label[int(p)])
            true_l.append(tl)
            true_p.append(tp)
        return {
            "precision": precision_score(true_l, true_p),
            "recall": recall_score(true_l, true_p),
            "f1": f1_score(true_l, true_p),
        }

    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=train_ds,
        eval_dataset=dev_ds,
        processing_class=tokenizer,
        data_collator=DataCollatorForTokenClassification(tokenizer),
        compute_metrics=compute_metrics,
    )

    print("[train]")
    trainer.train()
    best = args.output_dir / "model-best"
    trainer.save_model(str(best))
    tokenizer.save_pretrained(str(best))
    print(f"[saved] {best}")

    metrics = trainer.evaluate()
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    print("[metrics]", metrics)


if __name__ == "__main__":
    main()
