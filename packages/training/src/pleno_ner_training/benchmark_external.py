"""外部NERモデルのベンチマーク.

spaCy組み込みモデルを我々のテストデータで評価し、
エンティティラベルをマッピングしてF1スコアを算出する。
JA / EN 両対応。
"""

import json
import time
from pathlib import Path

import spacy
from spacy.tokens import DocBin

LABEL_MAP_EN: dict[str, str] = {
    "PERSON": "PERSON",
    "ORG": "ORGANIZATION",
    "GPE": "ADDRESS",
    "LOC": "ADDRESS",
    "FAC": "ADDRESS",
    "DATE": "DATE_OF_BIRTH",
}

LABEL_MAP_JA: dict[str, str] = {
    "Person": "PERSON",
    "PERSON": "PERSON",
    "ORG": "ORGANIZATION",
    "Organization": "ORGANIZATION",
    "GPE": "ADDRESS",
    "LOC": "ADDRESS",
    "FAC": "ADDRESS",
    "City": "ADDRESS",
    "Country": "ADDRESS",
    "DATE": "DATE_OF_BIRTH",
    "Date": "DATE_OF_BIRTH",
}

LABEL_MAPS: dict[str, dict[str, str]] = {
    "en": LABEL_MAP_EN,
    "ja": LABEL_MAP_JA,
}

DEFAULT_MODELS: dict[str, list[str]] = {
    "en": ["en_core_web_sm", "en_core_web_md"],
    "ja": ["ja_core_news_sm", "ja_core_news_md", "ja_core_news_lg"],
}

TARGET_LABELS = {"PERSON", "ORGANIZATION", "ADDRESS", "DATE_OF_BIRTH", "BANK_ACCOUNT"}


def load_gold_docs(nlp: spacy.Language, test_path: Path) -> list:
    db = DocBin().from_disk(str(test_path))
    return list(db.get_docs(nlp.vocab))


def evaluate_external(model_name: str, test_path: Path, language: str = "en") -> dict:
    nlp = spacy.load(model_name)
    gold_nlp = spacy.blank(language)
    gold_docs = load_gold_docs(gold_nlp, test_path)
    label_map = LABEL_MAPS[language]

    counts: dict[str, dict[str, int]] = {
        label: {"tp": 0, "fp": 0, "fn": 0} for label in TARGET_LABELS
    }

    total_time = 0.0
    total_docs = 0

    for gold_doc in gold_docs:
        text = gold_doc.text
        gold_ents = {(ent.start_char, ent.end_char, ent.label_) for ent in gold_doc.ents}

        start = time.perf_counter()
        pred_doc = nlp(text)
        total_time += time.perf_counter() - start
        total_docs += 1

        pred_ents: set[tuple[int, int, str]] = set()
        for ent in pred_doc.ents:
            mapped = label_map.get(ent.label_)
            if mapped:
                pred_ents.add((ent.start_char, ent.end_char, mapped))

        for label in TARGET_LABELS:
            gold_set = {(s, e) for s, e, lbl in gold_ents if lbl == label}
            pred_set = {(s, e) for s, e, lbl in pred_ents if lbl == label}
            counts[label]["tp"] += len(gold_set & pred_set)
            counts[label]["fp"] += len(pred_set - gold_set)
            counts[label]["fn"] += len(gold_set - pred_set)

    results: dict[str, dict[str, float]] = {}
    for label, c in counts.items():
        tp, fp, fn = c["tp"], c["fp"], c["fn"]
        p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        results[label] = {"p": p, "r": r, "f": f}

    latency_ms = (total_time / total_docs * 1000) if total_docs else 0

    # モデルサイズ測定
    model_path = Path(nlp.path) if nlp.path else None
    model_size_mb = 0.0
    if model_path and model_path.exists():
        model_size_mb = sum(
            f.stat().st_size for f in model_path.rglob("*") if f.is_file()
        ) / (1024 * 1024)

    return {
        "per_entity": results,
        "latency_ms_per_doc": latency_ms,
        "model_size_mb": round(model_size_mb, 1),
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--language", default="en", choices=["ja", "en"])
    parser.add_argument("--models", nargs="+", default=None)
    parser.add_argument("--test-data", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    models = args.models or DEFAULT_MODELS[args.language]
    test_data = args.test_data or (
        Path(__file__).parents[2] / "data" / "processed" / args.language / "test.spacy"
    )

    all_results = {}
    for model_name in models:
        print(f"\nEvaluating {model_name}...")
        try:
            result = evaluate_external(model_name, test_data, language=args.language)
            all_results[model_name] = result
            print(f"  Latency: {result['latency_ms_per_doc']:.1f} ms/doc")
            for label, scores in result["per_entity"].items():
                print(f"  {label:<20} P={scores['p']:.4f} R={scores['r']:.4f} F1={scores['f']:.4f}")
        except OSError as e:
            print(f"  Skipped: {e}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
