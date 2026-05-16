"""Train a JP PII NER transformer on the mechanism-v1 dataset.

Reads `data/raw/ja-mechanism-v1/{train,dev,test}.jsonl`, tokenises with
the base model's fast tokenizer, aligns char-offset spans to token-
level BIO labels, and fine-tunes via HuggingFace Trainer with seqeval.

Designed to run on either a single GPU pod (RunPod, ~10-20 min for
5k samples on an RTX 4090) or CPU for smoke tests. Per CLAUDE.md
the production run is on RunPod, orchestrated via the
`mcp__runpod__*` MCP tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from pleno_ner_training.entity_types import NER_LABELS, PATTERN_LABELS

LABELS = NER_LABELS + PATTERN_LABELS
BIO_LABELS: list[str] = ["O"] + [f"{prefix}-{label}" for label in LABELS for prefix in ("B", "I")]
LABEL2ID = {label: i for i, label in enumerate(BIO_LABELS)}
ID2LABEL = {i: label for label, i in LABEL2ID.items()}
IGNORE_INDEX = -100


@dataclass
class Example:
    text: str
    entities: list[tuple[int, int, str]]


def load_jsonl(path: Path) -> list[Example]:
    out: list[Example] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        out.append(
            Example(
                text=rec["text"],
                entities=[(e["start"], e["end"], e["label"]) for e in rec["entities"]],
            )
        )
    return out


def _bio_labels_for_tokens(text: str, entities: list[tuple[int, int, str]], offset_mapping: list[tuple[int, int]]) -> list[int]:
    """Convert char-offset spans to per-token BIO label ids."""
    labels = [LABEL2ID["O"]] * len(offset_mapping)
    for start, end, label in entities:
        if label not in LABELS:
            continue
        first = True
        for i, (tok_start, tok_end) in enumerate(offset_mapping):
            if tok_start == tok_end == 0:
                # Special token (CLS/SEP/PAD).
                labels[i] = IGNORE_INDEX
                continue
            # Token must be wholly inside the entity span.
            if tok_start >= start and tok_end <= end:
                tag = "B-" if first else "I-"
                labels[i] = LABEL2ID[f"{tag}{label}"]
                first = False
    return labels


def build_hf_dataset(examples: list[Example], tokenizer) -> "datasets.Dataset":  # noqa: F821
    """Tokenise + label-align. Returns a HuggingFace Dataset."""
    from datasets import Dataset

    rows = []
    for ex in examples:
        enc = tokenizer(
            ex.text,
            return_offsets_mapping=True,
            truncation=True,
            max_length=512,
            padding=False,
        )
        labels = _bio_labels_for_tokens(ex.text, ex.entities, enc["offset_mapping"])
        # Mask special tokens.
        for i, (a, b) in enumerate(enc["offset_mapping"]):
            if a == b == 0:
                labels[i] = IGNORE_INDEX
        rows.append({
            "input_ids": enc["input_ids"],
            "attention_mask": enc["attention_mask"],
            "labels": labels,
        })
    return Dataset.from_list(rows)


def compute_metrics(eval_preds):
    from seqeval.metrics import f1_score, precision_score, recall_score

    logits, labels = eval_preds
    preds = np.argmax(logits, axis=-1)
    true_labels, true_preds = [], []
    for pred_seq, label_seq in zip(preds, labels):
        tl, tp = [], []
        for p, l in zip(pred_seq, label_seq):
            if l == IGNORE_INDEX:
                continue
            tl.append(ID2LABEL[int(l)])
            tp.append(ID2LABEL[int(p)])
        true_labels.append(tl)
        true_preds.append(tp)
    return {
        "precision": precision_score(true_labels, true_preds),
        "recall": recall_score(true_labels, true_preds),
        "f1": f1_score(true_labels, true_preds),
    }


def train(
    data_dir: Path,
    output_dir: Path,
    base_model: str = "cl-tohoku/bert-base-japanese-v3",
    epochs: int = 3,
    batch_size: int = 16,
    lr: float = 5e-5,
    eval_only: bool = False,
) -> dict:
    """Run the training loop and return final metrics."""
    from transformers import (
        AutoModelForTokenClassification,
        AutoTokenizer,
        DataCollatorForTokenClassification,
        Trainer,
        TrainingArguments,
    )

    tokenizer = AutoTokenizer.from_pretrained(base_model)
    if not getattr(tokenizer, "is_fast", False):
        raise RuntimeError(f"{base_model} requires a fast tokenizer; pick a model with one")

    train_ex = load_jsonl(data_dir / "train.jsonl")
    dev_ex = load_jsonl(data_dir / "dev.jsonl") or train_ex[: max(1, len(train_ex) // 20)]
    test_ex = load_jsonl(data_dir / "test.jsonl") or dev_ex

    print(f"[load] train={len(train_ex)} dev={len(dev_ex)} test={len(test_ex)}")

    train_ds = build_hf_dataset(train_ex, tokenizer)
    dev_ds = build_hf_dataset(dev_ex, tokenizer)
    test_ds = build_hf_dataset(test_ex, tokenizer)

    model = AutoModelForTokenClassification.from_pretrained(
        base_model,
        num_labels=len(BIO_LABELS),
        id2label=ID2LABEL,
        label2id=LABEL2ID,
    )

    args = TrainingArguments(
        output_dir=str(output_dir),
        num_train_epochs=epochs,
        per_device_train_batch_size=batch_size,
        per_device_eval_batch_size=batch_size,
        learning_rate=lr,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        greater_is_better=True,
        logging_steps=50,
        save_total_limit=2,
        report_to=[],
    )

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=dev_ds,
        tokenizer=tokenizer,
        data_collator=DataCollatorForTokenClassification(tokenizer),
        compute_metrics=compute_metrics,
    )

    if not eval_only:
        trainer.train()
        trainer.save_model(str(output_dir / "model-best"))
        tokenizer.save_pretrained(str(output_dir / "model-best"))

    metrics = trainer.evaluate(eval_dataset=test_ds)
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[metrics] {metrics}")
    return metrics
