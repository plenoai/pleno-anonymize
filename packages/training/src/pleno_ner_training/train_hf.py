"""HuggingFace Transformers TokenClassification で NER モデルを fine-tune する.

- base_model: tohoku-nlp/bert-base-japanese-v3
- BIOタグ 11クラス (5 entity types x 2 + O)
- seqeval によるエンティティレベル F1 評価
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from datasets import DatasetDict, load_from_disk
from seqeval.metrics import (
    classification_report,
    f1_score,
    precision_score,
    recall_score,
)
from transformers import (
    AutoModelForTokenClassification,
    AutoTokenizer,
    DataCollatorForTokenClassification,
    Trainer,
    TrainingArguments,
)

from pleno_ner_training.convert_to_hf_dataset import BIO_LABELS, IGNORE_INDEX


def compute_metrics(eval_preds, id2label: dict[int, str]):
    """seqeval によるエンティティレベル評価."""
    logits, labels = eval_preds
    predictions = np.argmax(logits, axis=-1)

    true_labels = []
    true_predictions = []

    for pred_seq, label_seq in zip(predictions, labels):
        true_label = []
        true_pred = []
        for p, l in zip(pred_seq, label_seq):
            if l == IGNORE_INDEX:
                continue
            true_label.append(id2label[l])
            true_pred.append(id2label[p])
        true_labels.append(true_label)
        true_predictions.append(true_pred)

    return {
        "precision": precision_score(true_labels, true_predictions),
        "recall": recall_score(true_labels, true_predictions),
        "f1": f1_score(true_labels, true_predictions),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="HuggingFace Transformers NER fine-tuning"
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        required=True,
        help="HuggingFace Dataset ディレクトリ",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="tohoku-nlp/bert-base-japanese-v3",
        help="ベースモデル名",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="出力ディレクトリ",
    )
    parser.add_argument("--learning-rate", type=float, default=5e-5)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--num-epochs", type=int, default=10)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--warmup-ratio", type=float, default=0.1)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--fp16",
        action="store_true",
        help="FP16混合精度学習を有効にする",
    )
    args = parser.parse_args()

    label2id = {label: i for i, label in enumerate(BIO_LABELS)}
    id2label = {i: label for i, label in enumerate(BIO_LABELS)}
    num_labels = len(BIO_LABELS)

    print(f"Labels ({num_labels}): {BIO_LABELS}")

    # データセット読み込み
    print(f"Loading dataset from {args.dataset}...")
    dataset = load_from_disk(str(args.dataset))
    assert isinstance(dataset, DatasetDict), "Expected DatasetDict"

    print(f"  train: {len(dataset['train'])}")
    print(f"  validation: {len(dataset['validation'])}")
    print(f"  test: {len(dataset['test'])}")

    # モデルとトークナイザーの読み込み
    print(f"Loading model: {args.model}")
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForTokenClassification.from_pretrained(
        args.model,
        num_labels=num_labels,
        id2label=id2label,
        label2id=label2id,
    )

    # Data collator (パディングとラベルの-100埋め)
    data_collator = DataCollatorForTokenClassification(
        tokenizer=tokenizer,
        label_pad_token_id=IGNORE_INDEX,
    )

    # 学習設定
    training_args = TrainingArguments(
        output_dir=str(args.output),
        learning_rate=args.learning_rate,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        num_train_epochs=args.num_epochs,
        weight_decay=args.weight_decay,
        warmup_ratio=args.warmup_ratio,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1",
        greater_is_better=True,
        seed=args.seed,
        fp16=args.fp16,
        logging_steps=50,
        report_to="none",
        remove_unused_columns=False,
    )

    # Trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset["train"],
        eval_dataset=dataset["validation"],
        data_collator=data_collator,
        compute_metrics=lambda p: compute_metrics(p, id2label),
    )

    # 学習
    print("Starting training...")
    train_result = trainer.train()

    # ベストモデルの保存
    trainer.save_model(str(args.output))
    tokenizer.save_pretrained(str(args.output))

    # テストセットでの評価
    print("\nEvaluating on test set...")
    test_results = trainer.evaluate(dataset["test"], metric_key_prefix="test")

    # 結果の詳細レポート
    print("\n=== Test Results ===")
    for key, value in sorted(test_results.items()):
        print(f"  {key}: {value:.4f}" if isinstance(value, float) else f"  {key}: {value}")

    # 結果をJSONに保存
    scores_path = args.output / "scores.json"
    scores = {
        "train": {
            k: v for k, v in train_result.metrics.items()
        },
        "test": test_results,
        "config": {
            "model": args.model,
            "num_labels": num_labels,
            "labels": BIO_LABELS,
            "learning_rate": args.learning_rate,
            "batch_size": args.batch_size,
            "num_epochs": args.num_epochs,
            "weight_decay": args.weight_decay,
            "warmup_ratio": args.warmup_ratio,
        },
    }
    with open(scores_path, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nScores saved to {scores_path}")


if __name__ == "__main__":
    main()
