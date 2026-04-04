"""JSON アノテーションデータを HuggingFace Dataset 形式（BIOタグ付き）に変換する.

- ku-nlp/deberta-v2-base-japanese のtokenizerでトークン化 (SentencePiece, ブラウザ互換)
- 文字オフセットのエンティティをトークンレベルのBIOタグに変換
- train/dev/test 分割 (80/10/10, seed=42)
- Arrow形式で保存
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from datasets import Dataset, DatasetDict, ClassLabel, Features, Sequence, Value
from transformers import AutoTokenizer, PreTrainedTokenizerFast

# BIOラベル定義: O + 5エンティティ × 2 (B/I)
ENTITY_LABELS = ["ADDRESS", "BANK_ACCOUNT", "DATE_OF_BIRTH", "ORGANIZATION", "PERSON"]
BIO_LABELS = ["O"] + [
    f"{prefix}-{label}" for label in ENTITY_LABELS for prefix in ("B", "I")
]
LABEL2ID = {label: i for i, label in enumerate(BIO_LABELS)}
IGNORE_INDEX = -100


def align_labels_with_tokens(
    text: str,
    entities: list[dict],
    tokenizer: PreTrainedTokenizerFast,
    max_length: int = 512,
) -> dict | None:
    """テキストとエンティティをトークン化し、BIOタグにアライメントする.

    Returns:
        {"input_ids": [...], "attention_mask": [...], "labels": [...]} or None
    """
    encoding = tokenizer(
        text,
        max_length=max_length,
        truncation=True,
        padding=False,
        return_offsets_mapping=True,
    )
    offsets = encoding["offset_mapping"]
    input_ids = encoding["input_ids"]
    attention_mask = encoding["attention_mask"]

    # 文字位置 → エンティティラベルのマッピングを構築
    char_labels: list[tuple[str, bool]] = []  # (label, is_start)
    char_to_entity: dict[int, tuple[str, bool]] = {}
    for ent in entities:
        start, end, label = ent["start"], ent["end"], ent["label"]
        if label not in ENTITY_LABELS:
            continue
        for i in range(start, end):
            is_start = i == start
            char_to_entity[i] = (label, is_start)

    labels = []
    prev_label: str | None = None

    for idx, (tok_start, tok_end) in enumerate(offsets):
        # 特殊トークン → ignore
        if tok_start == 0 and tok_end == 0:
            labels.append(IGNORE_INDEX)
            prev_label = None
            continue

        # トークンがカバーする文字範囲のエンティティを判定
        # 最初の非空白文字のエンティティで決定
        token_label = None
        token_is_entity_start = False
        for char_idx in range(tok_start, tok_end):
            if char_idx in char_to_entity:
                entity_label, is_start = char_to_entity[char_idx]
                token_label = entity_label
                token_is_entity_start = is_start
                break

        if token_label is None:
            labels.append(LABEL2ID["O"])
            prev_label = None
        else:
            # B- if this is the start of entity OR the label changed from previous
            if token_is_entity_start or token_label != prev_label:
                labels.append(LABEL2ID[f"B-{token_label}"])
            else:
                labels.append(LABEL2ID[f"I-{token_label}"])
            prev_label = token_label

    if len(labels) != len(input_ids):
        return None

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
    }


def load_and_convert(
    input_path: Path,
    tokenizer: PreTrainedTokenizerFast,
    max_length: int = 512,
) -> list[dict]:
    """JSONファイルを読み込み、トークン化してBIOタグ付きデータに変換する."""
    with open(input_path, encoding="utf-8") as f:
        raw_data = json.load(f)

    converted = []
    skipped = 0

    for item in raw_data:
        text = item.get("text", "")
        entities = item.get("entities", [])

        # エンティティがないドキュメントはスキップ
        if not entities:
            skipped += 1
            continue

        result = align_labels_with_tokens(text, entities, tokenizer, max_length)
        if result is not None:
            converted.append(result)
        else:
            skipped += 1

    print(f"Converted: {len(converted)}, Skipped: {skipped}")
    return converted


def split_data(
    data: list[dict],
    train_ratio: float = 0.8,
    dev_ratio: float = 0.1,
    seed: int = 42,
) -> tuple[list[dict], list[dict], list[dict]]:
    """train/dev/test に分割する."""
    random.seed(seed)
    shuffled = list(data)
    random.shuffle(shuffled)

    n = len(shuffled)
    train_end = int(n * train_ratio)
    dev_end = int(n * (train_ratio + dev_ratio))

    return shuffled[:train_end], shuffled[train_end:dev_end], shuffled[dev_end:]


def create_dataset(data: list[dict]) -> Dataset:
    """リストからHuggingFace Datasetを作成する."""
    if not data:
        return Dataset.from_dict(
            {"input_ids": [], "attention_mask": [], "labels": []}
        )

    features = Features(
        {
            "input_ids": Sequence(Value("int32")),
            "attention_mask": Sequence(Value("int8")),
            "labels": Sequence(
                ClassLabel(names=BIO_LABELS),
            ),
        }
    )

    return Dataset.from_dict(
        {
            "input_ids": [d["input_ids"] for d in data],
            "attention_mask": [d["attention_mask"] for d in data],
            "labels": [d["labels"] for d in data],
        },
        features=features,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="JSON → HuggingFace Dataset (BIOタグ) 変換"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="入力JSONファイル",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="出力ディレクトリ (Arrow形式)",
    )
    parser.add_argument(
        "--tokenizer",
        type=str,
        default="ku-nlp/deberta-v2-base-japanese",
        help="HuggingFace tokenizer名",
    )
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"Loading tokenizer: {args.tokenizer}")
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)

    print(f"Loading and converting data from {args.input}...")
    data = load_and_convert(args.input, tokenizer, args.max_length)

    if not data:
        print("ERROR: No data was converted. Check input file.")
        raise SystemExit(1)

    train, dev, test = split_data(data, seed=args.seed)
    print(f"Split: train={len(train)}, dev={len(dev)}, test={len(test)}")

    dataset_dict = DatasetDict(
        {
            "train": create_dataset(train),
            "validation": create_dataset(dev),
            "test": create_dataset(test),
        }
    )

    args.output.mkdir(parents=True, exist_ok=True)
    dataset_dict.save_to_disk(str(args.output))
    print(f"Saved to {args.output}")

    # ラベル情報も保存 (学習時に参照)
    label_info_path = args.output / "label_info.json"
    with open(label_info_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "labels": BIO_LABELS,
                "label2id": LABEL2ID,
                "id2label": {v: k for k, v in LABEL2ID.items()},
                "num_labels": len(BIO_LABELS),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"Label info saved to {label_info_path}")


if __name__ == "__main__":
    main()
