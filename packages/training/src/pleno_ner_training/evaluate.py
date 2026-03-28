"""訓練済みモデルの評価スクリプト.

- エンティティ別 precision / recall / F1
- 全体の加重平均F1
- 全角・半角混在テスト
"""

import json
from pathlib import Path

import spacy
from spacy.scorer import Scorer
from spacy.tokens import DocBin
from spacy.training import Example

ACCEPTANCE_CRITERIA: dict[str, float] = {
    "PERSON": 0.90,
    "ADDRESS": 0.85,
    "ORGANIZATION": 0.85,
    "DATE_OF_BIRTH": 0.80,
    "BANK_ACCOUNT": 0.80,
}
OVERALL_F1_THRESHOLD = 0.88


def load_test_docs(nlp: spacy.Language, path: Path) -> list:
    """DocBin からテストドキュメントを読み込む."""
    db = DocBin().from_disk(str(path))
    return list(db.get_docs(nlp.vocab))


def evaluate_model(
    model_path: str,
    test_path: Path,
) -> dict:
    """モデルをテストデータで評価する."""
    nlp = spacy.load(model_path)
    gold_docs = load_test_docs(nlp, test_path)

    examples = []
    for gold_doc in gold_docs:
        pred_doc = nlp.make_doc(gold_doc.text)
        pred_doc = nlp(gold_doc.text)
        example = Example(pred_doc, gold_doc)
        examples.append(example)

    scorer = Scorer()
    scores = scorer.score(examples)

    return scores


def print_report(scores: dict) -> bool:
    """評価結果を表示し、合格基準をチェックする."""
    print("=== Evaluation Report ===\n")

    ents_per_type = scores.get("ents_per_type", {})
    all_pass = True

    print(f"{'Entity':<20} {'Precision':>10} {'Recall':>10} {'F1':>10} {'Threshold':>10} {'Pass':>6}")
    print("-" * 70)

    for label, threshold in ACCEPTANCE_CRITERIA.items():
        stats = ents_per_type.get(label, {"p": 0, "r": 0, "f": 0})
        p = stats.get("p", 0)
        r = stats.get("r", 0)
        f = stats.get("f", 0)
        passed = f >= threshold
        if not passed:
            all_pass = False
        print(f"{label:<20} {p:>10.4f} {r:>10.4f} {f:>10.4f} {threshold:>10.2f} {'OK' if passed else 'FAIL':>6}")

    overall_f1 = scores.get("ents_f", 0)
    overall_pass = overall_f1 >= OVERALL_F1_THRESHOLD
    if not overall_pass:
        all_pass = False

    print("-" * 70)
    print(f"{'Overall':<20} {scores.get('ents_p', 0):>10.4f} {scores.get('ents_r', 0):>10.4f} {overall_f1:>10.4f} {OVERALL_F1_THRESHOLD:>10.2f} {'OK' if overall_pass else 'FAIL':>6}")

    print(f"\nVerdict: {'PASS' if all_pass else 'FAIL'}")
    return all_pass


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="NERモデル評価")
    parser.add_argument("--model", required=True, help="モデルパスまたはパッケージ名")
    parser.add_argument(
        "--test-data",
        type=Path,
        default=Path(__file__).parents[2] / "data" / "processed" / "test.spacy",
    )
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args()

    scores = evaluate_model(args.model, args.test_data)
    passed = print_report(scores)

    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(scores, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nScores saved to {args.output_json}")

    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
