"""JSON アノテーションデータを spaCy DocBin 形式に変換する.

- ja_core_news_sm のトークナイザーを使用
- char_span による トークン境界アライメント
- train/dev/test 分割
"""

import json
import random
import sys
from pathlib import Path

import spacy
from spacy.tokens import DocBin


def convert_to_docs(
    nlp: spacy.Language,
    data: list[dict],
) -> tuple[list, int, int]:
    """JSONデータを spaCy Doc オブジェクトに変換する.

    Returns:
        (docs, success_count, alignment_failure_count)
    """
    docs = []
    alignment_failures = 0
    total_entities = 0

    for item in data:
        text = item["text"]
        doc = nlp.make_doc(text)
        ents = []
        failed_in_doc = False

        for ent_data in item["entities"]:
            total_entities += 1
            span = doc.char_span(
                ent_data["start"],
                ent_data["end"],
                label=ent_data["label"],
                alignment_mode="expand",
            )
            if span is None:
                alignment_failures += 1
                failed_in_doc = True
                continue
            ents.append(span)

        # 重複スパンを除去（先に出現したものを優先）
        filtered_ents = []
        occupied = set()
        for span in ents:
            token_indices = set(range(span.start, span.end))
            if not token_indices & occupied:
                filtered_ents.append(span)
                occupied |= token_indices

        doc.ents = filtered_ents
        if not failed_in_doc or filtered_ents:
            docs.append(doc)

    return docs, total_entities, alignment_failures


def split_data(
    docs: list,
    train_ratio: float = 0.8,
    dev_ratio: float = 0.1,
    seed: int = 42,
) -> tuple[list, list, list]:
    """train/dev/test に分割する."""
    random.seed(seed)
    shuffled = list(docs)
    random.shuffle(shuffled)

    n = len(shuffled)
    train_end = int(n * train_ratio)
    dev_end = int(n * (train_ratio + dev_ratio))

    return shuffled[:train_end], shuffled[train_end:dev_end], shuffled[dev_end:]


def save_docbin(docs: list, path: Path) -> None:
    """DocBin として保存する."""
    db = DocBin()
    for doc in docs:
        db.add(doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    db.to_disk(str(path))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="JSON → spaCy DocBin 変換")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parents[2] / "data" / "raw" / "generated.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parents[2] / "data" / "processed",
    )
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print("Loading spaCy Japanese tokenizer...")
    nlp = spacy.blank("ja")

    print(f"Loading data from {args.input}...")
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    print(f"Converting {len(data)} documents...")
    docs, total_entities, failures = convert_to_docs(nlp, data)

    failure_rate = failures / total_entities * 100 if total_entities > 0 else 0
    print(f"\n=== Conversion Summary ===")
    print(f"Documents: {len(docs)}")
    print(f"Total entities: {total_entities}")
    print(f"Alignment failures: {failures} ({failure_rate:.1f}%)")
    if failure_rate > 5:
        print(
            "[WARN] Alignment failure rate > 5%. Check tokenizer compatibility.",
            file=sys.stderr,
        )

    train, dev, test = split_data(docs, seed=args.seed)
    print(f"Split: train={len(train)}, dev={len(dev)}, test={len(test)}")

    save_docbin(train, args.output_dir / "train.spacy")
    save_docbin(dev, args.output_dir / "dev.spacy")
    save_docbin(test, args.output_dir / "test.spacy")

    print(f"Output: {args.output_dir}")


if __name__ == "__main__":
    main()
