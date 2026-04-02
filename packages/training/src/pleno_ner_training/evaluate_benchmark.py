"""ベンチマーク評価スクリプト.

学習パイプラインのテストデータではなく、独立したベンチマークデータで
モデルを評価する。外部モデルとの比較も同時に実施。
"""

import json
import sys
import time
from pathlib import Path

import spacy
from spacy.scorer import Scorer
from spacy.tokens import DocBin
from spacy.training import Example

from pleno_ner_training.benchmark_config import (
    BENCHMARK_CONFIGS,
    BENCHMARK_VERSIONS,
    LATEST_BENCHMARK_VERSION,
)


def load_benchmark_docs(nlp: spacy.Language, path: Path) -> list:
    """ベンチマークDocBinを読み込む."""
    db = DocBin().from_disk(str(path))
    return list(db.get_docs(nlp.vocab))


def evaluate_on_benchmark(
    model_path: str,
    benchmark_path: Path,
) -> dict:
    """モデルをベンチマークデータで評価する."""
    nlp = spacy.load(model_path)
    gold_docs = load_benchmark_docs(nlp, benchmark_path)

    examples = []
    total_time = 0.0
    negative_docs = 0
    clean_negative_docs = 0
    negative_fp_total = 0
    for gold_doc in gold_docs:
        start = time.perf_counter()
        pred_doc = nlp(gold_doc.text)
        total_time += time.perf_counter() - start
        examples.append(Example(pred_doc, gold_doc))
        if not gold_doc.ents:
            negative_docs += 1
            negative_fp_total += len(pred_doc.ents)
            if not pred_doc.ents:
                clean_negative_docs += 1

    scores = Scorer().score(examples)
    scores["latency_ms_per_doc"] = round(
        (total_time / len(gold_docs) * 1000) if gold_docs else 0, 1
    )
    scores["num_docs"] = len(gold_docs)
    scores["negative_docs"] = negative_docs
    scores["negative_clean_docs"] = clean_negative_docs
    scores["negative_doc_clean_rate"] = (
        clean_negative_docs / negative_docs if negative_docs else 0
    )
    scores["negative_fp_total"] = negative_fp_total

    # モデルサイズ
    model_dir = Path(nlp.path) if nlp.path else Path(model_path)
    if model_dir.exists():
        size_bytes = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        scores["model_size_mb"] = round(size_bytes / (1024 * 1024), 1)

    return scores


def print_benchmark_report(
    results: dict[str, dict],
    language: str,
    suite_kind: str,
) -> None:
    """全モデルのベンチマーク結果を比較表示する."""
    version = results.pop("_version", "")
    print(f"\n{'=' * 90}")
    print(f"  Benchmark {version} Results ({language}) [{suite_kind}]")
    print(f"{'=' * 90}\n")

    labels = ["PERSON", "ADDRESS", "ORGANIZATION", "DATE_OF_BIRTH", "BANK_ACCOUNT"]

    # ヘッダー
    model_names = list(results.keys())
    col_width = 14
    header = f"{'Entity':<20}"
    for name in model_names:
        header += f" {name:>{col_width}}"
    print(header)
    print("-" * (20 + (col_width + 1) * len(model_names)))

    # エンティティ別F1
    for label in labels:
        row = f"{label:<20}"
        for name in model_names:
            ents = results[name].get("ents_per_type", {})
            f1 = ents.get(label, {}).get("f", 0)
            row += f" {f1 * 100:>{col_width - 1}.1f}%"
        print(row)

    # 全体
    print("-" * (20 + (col_width + 1) * len(model_names)))
    row = f"{'Overall F1':<20}"
    for name in model_names:
        f1 = results[name].get("ents_f", 0)
        row += f" {f1 * 100:>{col_width - 1}.1f}%"
    print(row)

    row = f"{'Precision':<20}"
    for name in model_names:
        p = results[name].get("ents_p", 0)
        row += f" {p * 100:>{col_width - 1}.1f}%"
    print(row)

    row = f"{'Recall':<20}"
    for name in model_names:
        r = results[name].get("ents_r", 0)
        row += f" {r * 100:>{col_width - 1}.1f}%"
    print(row)

    # メタ情報
    print()
    row = f"{'Docs':<20}"
    for name in model_names:
        n = results[name].get("num_docs", 0)
        row += f" {n:>{col_width}}"
    print(row)

    row = f"{'Latency (ms/doc)':<20}"
    for name in model_names:
        lat = results[name].get("latency_ms_per_doc", 0)
        row += f" {lat:>{col_width}.1f}"
    print(row)

    row = f"{'Size (MB)':<20}"
    for name in model_names:
        size = results[name].get("model_size_mb", 0)
        row += f" {size:>{col_width}.1f}"
    print(row)

    row = f"{'Neg Clean Rate':<20}"
    for name in model_names:
        rate = results[name].get("negative_doc_clean_rate", 0)
        row += f" {rate * 100:>{col_width - 1}.1f}%"
    print(row)

    row = f"{'Neg FP Total':<20}"
    for name in model_names:
        total = results[name].get("negative_fp_total", 0)
        row += f" {total:>{col_width}}"
    print(row)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Benchmark evaluation")
    parser.add_argument("--model", required=True, help="Primary model path")
    parser.add_argument("--language", default="ja", choices=["ja", "en"])
    parser.add_argument("--version", default=LATEST_BENCHMARK_VERSION, choices=BENCHMARK_VERSIONS)
    parser.add_argument("--benchmark-data", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    args = parser.parse_args()
    config = BENCHMARK_CONFIGS[args.version]

    data_root = Path(__file__).parents[2] / "data"
    benchmark_path = args.benchmark_data or (
        data_root / "benchmark" / args.version / args.language / "test.spacy"
    )

    if not benchmark_path.exists():
        print(f"[ERROR] Benchmark data not found: {benchmark_path}", file=sys.stderr)
        print(f"Run: make benchmark-{args.version.replace('.', '')}-generate first", file=sys.stderr)
        raise SystemExit(1)

    results: dict[str, dict] = {"_version": args.version}

    # プライマリモデル評価
    print(f"Evaluating {args.model}...")
    results["pleno_ner"] = evaluate_on_benchmark(args.model, benchmark_path)

    print_benchmark_report(results, args.language, config.suite_kind)

    # 結果保存
    output_path = args.output_json or (
        data_root / "benchmark" / args.version / args.language / "scores.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # scores から non-serializable な値を除去
    serializable = {}
    for model_name, scores in results.items():
        serializable[model_name] = {
            "ents_p": scores.get("ents_p", 0),
            "ents_r": scores.get("ents_r", 0),
            "ents_f": scores.get("ents_f", 0),
            "ents_per_type": scores.get("ents_per_type", {}),
            "latency_ms_per_doc": scores.get("latency_ms_per_doc", 0),
            "model_size_mb": scores.get("model_size_mb", 0),
            "num_docs": scores.get("num_docs", 0),
            "negative_docs": scores.get("negative_docs", 0),
            "negative_clean_docs": scores.get("negative_clean_docs", 0),
            "negative_doc_clean_rate": scores.get("negative_doc_clean_rate", 0),
            "negative_fp_total": scores.get("negative_fp_total", 0),
            "suite_kind": config.suite_kind,
            "purpose": config.purpose,
        }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nScores saved to {output_path}")


if __name__ == "__main__":
    main()
