"""Supervised v2: train on 0xhikae/pii-masking-300k-ja train split (Japanese).

Iteration 1 trained on 2,014 synthetic samples and hit F1 0.352 on the
validation split — domain-shift-limited. This script trains directly
on the train split of the same dataset (label-agnostic IoU eval on
validation is still fair: train/validation are disjoint by construction).

Usage on RunPod (one-shot, dataset is public):
    python scripts/train_supervised_300k_ja.py \\
        --base-model FacebookAI/xlm-roberta-base \\
        --output-dir output/ja-ner-supervised-v2 \\
        --epochs 2 --max-train 50000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def _parse_spans(raw) -> list[tuple[int, int, str]]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out: list[tuple[int, int, str]] = []
    for s in raw:
        if isinstance(s, list) and len(s) >= 3:
            out.append((int(s[0]), int(s[1]), str(s[2])))
        elif isinstance(s, dict) and {"start", "end"} <= s.keys():
            out.append((int(s["start"]), int(s["end"]), str(s.get("label", "?"))))
    return out


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
    parser.add_argument("--base-model", default="FacebookAI/xlm-roberta-base")
    parser.add_argument("--output-dir", type=Path, default=Path("output/ja-ner-supervised-v2"))
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=5e-5)
    parser.add_argument("--max-train", type=int, default=50_000)
    parser.add_argument("--max-dev", type=int, default=500)
    parser.add_argument("--dataset", default="0xhikae/pii-masking-300k-ja")
    args = parser.parse_args()

    from datasets import Dataset, load_dataset
    from transformers import (
        AutoModelForTokenClassification,
        AutoTokenizer,
        DataCollatorForTokenClassification,
        Trainer,
        TrainingArguments,
    )

    print(f"[load] {args.dataset} train split (streaming, language=Japanese)")
    ds_iter = load_dataset(args.dataset, split="train", streaming=True)
    train_rows: list[dict] = []
    for row in ds_iter:
        if row.get("language") != "Japanese":
            continue
        text = row.get("source_text") or row.get("text")
        if not text:
            continue
        spans = _parse_spans(row.get("span_labels"))
        if not spans:
            continue
        train_rows.append({"text": text, "entities": spans})
        if len(train_rows) >= args.max_train:
            break
    print(f"[load] train: {len(train_rows)} JP rows")

    print(f"[load] {args.dataset} validation split")
    ds_dev = load_dataset(args.dataset, split="validation", streaming=True)
    dev_rows: list[dict] = []
    for row in ds_dev:
        if row.get("language") != "Japanese":
            continue
        text = row.get("source_text") or row.get("text")
        if not text:
            continue
        spans = _parse_spans(row.get("span_labels"))
        if not spans:
            continue
        dev_rows.append({"text": text, "entities": spans})
        if len(dev_rows) >= args.max_dev:
            break
    print(f"[load] dev: {len(dev_rows)} JP rows")

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
        tokenizer=tokenizer,
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
