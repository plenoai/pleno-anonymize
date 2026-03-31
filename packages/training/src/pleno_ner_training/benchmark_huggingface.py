"""HuggingFace NERモデルのベンチマーク.

HuggingFace token-classificationモデルを我々のテストデータで評価し、
spaCyベンチマークと同じJSON形式で結果を出力する。

BertJapaneseTokenizerはcharacter offsetを返さないため、
tokenのword列を連結してgold spanとの部分一致で評価する。
"""

import json
import time
from pathlib import Path

import spacy
from spacy.tokens import DocBin

TARGET_LABELS = {"PERSON", "ORGANIZATION", "ADDRESS", "DATE_OF_BIRTH", "BANK_ACCOUNT"}

HF_MODEL_CONFIGS: dict[str, dict] = {
    "jurabi/bert-ner-japanese": {
        "key": "bert_ner_ja",
        "label_map": {
            "人名": "PERSON",
            "法人名": "ORGANIZATION",
            "政治的組織名": "ORGANIZATION",
            "その他の組織名": "ORGANIZATION",
            "地名": "ADDRESS",
            "施設名": "ADDRESS",
        },
        "language": "ja",
    },
}


def load_gold_docs(language: str, test_path: Path) -> list:
    nlp = spacy.blank(language)
    db = DocBin().from_disk(str(test_path))
    return list(db.get_docs(nlp.vocab))


def _extract_spans_from_bio(
    tokens: list[dict],
    label_map: dict[str, str],
) -> list[tuple[str, str]]:
    """BIOタグ付きトークン列からエンティティスパン(mapped_label, text)を抽出."""
    spans: list[tuple[str, str]] = []
    current_label = None
    current_words: list[str] = []

    for tok in tokens:
        entity = tok["entity"]
        word = tok.get("word", "")
        # [UNK] や特殊トークンをスキップ
        if word in ("[UNK]", "[CLS]", "[SEP]", "[PAD]"):
            continue
        # ## サブワードプレフィックスを除去
        if word.startswith("##"):
            word = word[2:]

        if entity.startswith("B-"):
            # 前のスパンを確定
            if current_label and current_words:
                spans.append((current_label, "".join(current_words)))
            raw_label = entity[2:]
            current_label = label_map.get(raw_label)
            current_words = [word] if current_label else []
            if not current_label:
                current_label = None
        elif entity.startswith("I-") and current_label:
            current_words.append(word)
        else:
            if current_label and current_words:
                spans.append((current_label, "".join(current_words)))
            current_label = None
            current_words = []

    if current_label and current_words:
        spans.append((current_label, "".join(current_words)))

    return spans


def evaluate_hf_model(model_id: str, test_path: Path) -> dict:
    from transformers import pipeline

    config = HF_MODEL_CONFIGS[model_id]
    label_map = config["label_map"]
    language = config["language"]

    gold_docs = load_gold_docs(language, test_path)
    ner = pipeline("ner", model=model_id)

    counts: dict[str, dict[str, int]] = {
        label: {"tp": 0, "fp": 0, "fn": 0} for label in TARGET_LABELS
    }

    total_time = 0.0
    total_docs = 0

    for gold_doc in gold_docs:
        text = gold_doc.text
        gold_ents_by_label: dict[str, list[str]] = {label: [] for label in TARGET_LABELS}
        for ent in gold_doc.ents:
            if ent.label_ in TARGET_LABELS:
                gold_ents_by_label[ent.label_].append(ent.text)

        start = time.perf_counter()
        tokens = ner(text)
        total_time += time.perf_counter() - start
        total_docs += 1

        pred_spans = _extract_spans_from_bio(tokens, label_map)
        pred_by_label: dict[str, list[str]] = {label: [] for label in TARGET_LABELS}
        for mapped_label, span_text in pred_spans:
            if mapped_label in TARGET_LABELS:
                pred_by_label[mapped_label].append(span_text)

        for label in TARGET_LABELS:
            gold_texts = list(gold_ents_by_label[label])
            pred_texts = list(pred_by_label[label])

            # テキスト部分一致でマッチング (pred がgold を含む or gold が pred を含む)
            matched_gold: set[int] = set()
            matched_pred: set[int] = set()
            for gi, gt in enumerate(gold_texts):
                for pi, pt in enumerate(pred_texts):
                    if pi in matched_pred:
                        continue
                    if gt in pt or pt in gt:
                        matched_gold.add(gi)
                        matched_pred.add(pi)
                        break

            tp = len(matched_gold)
            fn = len(gold_texts) - tp
            fp = len(pred_texts) - len(matched_pred)
            counts[label]["tp"] += tp
            counts[label]["fn"] += fn
            counts[label]["fp"] += fp

    results: dict[str, dict[str, float]] = {}
    for label, c in counts.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        results[label] = {"p": p, "r": r, "f": f}

    latency_ms = (total_time / total_docs * 1000) if total_docs else 0

    # モデルサイズ測定
    model_size_mb = 0.0
    try:
        from huggingface_hub import model_info

        info = model_info(model_id, files_metadata=True)
        if info.siblings:
            model_size_mb = sum(s.size for s in info.siblings if s.size) / (1024 * 1024)
    except Exception:
        pass

    return {
        "per_entity": results,
        "latency_ms_per_doc": latency_ms,
        "model_size_mb": round(model_size_mb, 1),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="HuggingFace NERモデルベンチマーク")
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(HF_MODEL_CONFIGS.keys()),
        help="HuggingFace model IDs to evaluate",
    )
    parser.add_argument("--test-data", type=Path, default=None)
    parser.add_argument("--language", default="ja", choices=["ja", "en"])
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--merge-into",
        type=Path,
        default=None,
        help="Merge results into an existing external_scores.json",
    )
    args = parser.parse_args()

    test_data = args.test_data or (
        Path(__file__).parents[2] / "data" / "processed" / args.language / "test.spacy"
    )

    all_results = {}
    for model_id in args.models:
        if model_id not in HF_MODEL_CONFIGS:
            print(f"  Skipped {model_id}: no config defined in HF_MODEL_CONFIGS")
            continue
        print(f"\nEvaluating {model_id}...")
        result = evaluate_hf_model(model_id, test_data)
        short_key = HF_MODEL_CONFIGS[model_id].get("key", model_id.split("/")[-1])
        all_results[short_key] = result
        print(f"  Size: {result['model_size_mb']:.1f} MB")
        print(f"  Latency: {result['latency_ms_per_doc']:.1f} ms/doc")
        for label, scores in result["per_entity"].items():
            print(f"  {label:<20} P={scores['p']:.4f} R={scores['r']:.4f} F1={scores['f']:.4f}")

    if args.merge_into and args.merge_into.exists():
        with open(args.merge_into) as f:
            existing = json.load(f)
        existing.update(all_results)
        with open(args.merge_into, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"\nMerged into {args.merge_into}")
    elif args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
