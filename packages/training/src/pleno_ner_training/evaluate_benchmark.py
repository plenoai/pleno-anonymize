"""ベンチマーク評価スクリプト.

学習パイプラインのテストデータではなく、独立したベンチマークデータで
モデルを評価する。外部モデルとの比較も同時に実施。

spaCy モデル経路と HuggingFace token-classification モデル経路の双方を
同じ spaCy `Scorer` (strict-span F1, micro aggregation) で測ることで
公平比較を可能にする (#69)。
"""

import json
import sys
import time
from pathlib import Path

import spacy
from spacy.scorer import Scorer
from spacy.tokens import Doc, DocBin
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


def _derive_hf_entry_key(model_path: str) -> str:
    """`output/hf-ja-v02-tiny-hardneg` → `hf_v02_tiny_hardneg`.

    scores.json key 命名を機械的に決めることで、ad-hoc 命名の衝突を防ぐ (#70)。"""
    name = Path(model_path).name
    # 言語タグ ja/en は scores.json が language ディレクトリで分離済みなので除く
    parts = name.split("-")
    cleaned = [p for p in parts if p not in {"ja", "en"}]
    return "_".join(cleaned) if cleaned else name.replace("-", "_")


def _bio_tags_to_char_spans(
    tags: list[str],
    offsets: list[tuple[int, int]],
) -> list[tuple[int, int, str]]:
    """BIOラベル列とトークン char-offsets から (start, end, label) スパンを抽出.

    HF token-classification の subword 出力を char offset 空間に逆変換する。
    特殊トークン (offset == (0, 0)) は無視する。"""
    spans: list[tuple[int, int, str]] = []
    cur_label: str | None = None
    cur_start: int | None = None
    cur_end: int | None = None

    for tag, (tok_start, tok_end) in zip(tags, offsets):
        # spaCy は (0,0) を special token / pad として扱う
        if tok_start == 0 and tok_end == 0:
            if cur_label is not None and cur_start is not None and cur_end is not None:
                spans.append((cur_start, cur_end, cur_label))
                cur_label, cur_start, cur_end = None, None, None
            continue

        if tag == "O" or tag == "":
            if cur_label is not None and cur_start is not None and cur_end is not None:
                spans.append((cur_start, cur_end, cur_label))
                cur_label, cur_start, cur_end = None, None, None
            continue

        # tag は "B-LABEL" or "I-LABEL"
        if "-" not in tag:
            continue
        prefix, label = tag.split("-", 1)

        if prefix == "B" or cur_label != label:
            # 前のスパンを確定
            if cur_label is not None and cur_start is not None and cur_end is not None:
                spans.append((cur_start, cur_end, cur_label))
            cur_label = label
            cur_start = tok_start
            cur_end = tok_end
        else:  # prefix == "I" and same label
            cur_end = tok_end

    if cur_label is not None and cur_start is not None and cur_end is not None:
        spans.append((cur_start, cur_end, cur_label))

    return spans


def evaluate_hf_on_benchmark(
    model_path: str,
    benchmark_path: Path,
    language: str = "ja",
    max_length: int = 512,
) -> dict:
    """HuggingFace token-classification モデルをベンチマークで評価.

    spaCy Scorer 互換 (strict-span F1) で測るため:
      1. fast tokenizer の return_offsets_mapping=True で char offset 取得
      2. argmax で BIO ラベル列を作成
      3. char-offset 空間でスパンに集約
      4. spaCy Doc + Example に詰めて Scorer.score() に渡す
    """
    # 遅延 import: HF deps は [hf] extra にあり、spaCy 経路では不要
    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    if not getattr(tokenizer, "is_fast", False):
        raise RuntimeError(
            f"HF model at {model_path} must ship a fast tokenizer "
            "(return_offsets_mapping required)."
        )
    model = AutoModelForTokenClassification.from_pretrained(model_path)
    model.eval()
    id2label: dict[int, str] = {int(k): v for k, v in model.config.id2label.items()}

    # gold は spaCy DocBin。blank 言語 vocab で読めば十分 (NER labels は doc 内)
    blank_nlp = spacy.blank(language)
    gold_docs = load_benchmark_docs(blank_nlp, benchmark_path)

    examples: list[Example] = []
    total_time = 0.0
    negative_docs = 0
    clean_negative_docs = 0
    negative_fp_total = 0

    for gold_doc in gold_docs:
        text = gold_doc.text

        start = time.perf_counter()
        with torch.no_grad():
            enc = tokenizer(
                text,
                max_length=max_length,
                truncation=True,
                padding=False,
                return_offsets_mapping=True,
                return_tensors="pt",
            )
            offsets = enc.pop("offset_mapping")[0].tolist()
            logits = model(**enc).logits[0]
            tag_ids = logits.argmax(dim=-1).tolist()
        total_time += time.perf_counter() - start

        tags = [id2label.get(int(i), "O") for i in tag_ids]
        char_spans = _bio_tags_to_char_spans(tags, offsets)

        # gold doc と同じ text/tokens を持つ pred Doc を作る。
        # spaCy Scorer は ent の char offset を比較するので tokenization は同一でなくてよい。
        pred_doc = Doc(blank_nlp.vocab, words=[t.text for t in gold_doc], spaces=[t.whitespace_ != "" for t in gold_doc])
        pred_ents = []
        for s, e, label in char_spans:
            span = pred_doc.char_span(s, e, label=label, alignment_mode="expand")
            if span is not None:
                pred_ents.append(span)
        try:
            pred_doc.ents = pred_ents  # type: ignore[assignment]
        except ValueError:
            # 重複スパンが出たら長い方を優先 (greedy non-overlap)
            occupied: list[tuple[int, int]] = []
            unique = []
            for span in sorted(pred_ents, key=lambda s: (-(s.end - s.start), s.start)):
                if any(span.start < oe and os < span.end for os, oe in occupied):
                    continue
                unique.append(span)
                occupied.append((span.start, span.end))
            unique.sort(key=lambda s: s.start)
            pred_doc.ents = unique  # type: ignore[assignment]

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

    model_dir = Path(model_path)
    if model_dir.exists():
        size_bytes = sum(f.stat().st_size for f in model_dir.rglob("*") if f.is_file())
        scores["model_size_mb"] = round(size_bytes / (1024 * 1024), 1)

    return scores


def _align_pred_ents_to_gold(
    blank_nlp: spacy.Language,
    gold_doc: Doc,
    char_spans: list[tuple[int, int, str]],
) -> Doc:
    """Build a pred Doc with char_spans projected onto gold's tokenization.

    Mirrors the alignment logic in `evaluate_hf_on_benchmark`: char_span with
    alignment_mode="expand" snaps to nearest token boundary, then greedy
    non-overlap dedup keeps the longest spans. spaCy `Scorer` operates on this
    token-aligned representation.
    """
    pred_doc = Doc(
        blank_nlp.vocab,
        words=[t.text for t in gold_doc],
        spaces=[t.whitespace_ != "" for t in gold_doc],
    )
    pred_ents = []
    for s, e, label in char_spans:
        span = pred_doc.char_span(s, e, label=label, alignment_mode="expand")
        if span is not None:
            pred_ents.append(span)
    try:
        pred_doc.ents = pred_ents  # type: ignore[assignment]
    except ValueError:
        occupied: list[tuple[int, int]] = []
        unique = []
        for span in sorted(pred_ents, key=lambda s: (-(s.end - s.start), s.start)):
            if any(span.start < oe and os < span.end for os, oe in occupied):
                continue
            unique.append(span)
            occupied.append((span.start, span.end))
        unique.sort(key=lambda s: s.start)
        pred_doc.ents = unique  # type: ignore[assignment]
    return pred_doc


def evaluate_scored_predictions_on_benchmark(
    scored_predictions_path: Path,
    benchmark_path: Path,
    language: str = "ja",
    label_thresholds: dict[str, float] | None = None,
) -> dict:
    """Score `predict_hf_with_scores.py` output against a DocBin gold set.

    Decouples model inference from scoring: predict once → score many times at
    different per-label thresholds (#98 ORG-only floor sweep). Uses the same
    spaCy Scorer + char_span alignment as `evaluate_hf_on_benchmark`, so
    numbers are directly comparable to scores.json entries.

    `label_thresholds` filters spans whose `score < threshold[label]`. Labels
    missing from the dict default to 0.0 (no filtering).
    """
    label_thresholds = label_thresholds or {}
    blank_nlp = spacy.blank(language)
    gold_docs = load_benchmark_docs(blank_nlp, benchmark_path)

    with open(scored_predictions_path, encoding="utf-8") as f:
        scored = json.load(f)
    if not isinstance(scored, list):
        raise SystemExit(
            f"Expected list at {scored_predictions_path}; got {type(scored)}"
        )
    if len(scored) != len(gold_docs):
        raise SystemExit(
            f"Doc count mismatch: scored={len(scored)} gold={len(gold_docs)}"
        )

    examples: list[Example] = []
    negative_docs = 0
    clean_negative_docs = 0
    negative_fp_total = 0

    for gold_doc, pred_record in zip(gold_docs, scored):
        # Sanity check: text alignment. raw.json was produced from the same
        # corpus as test.spacy, so texts must match exactly.
        if pred_record.get("text", "") != gold_doc.text:
            raise SystemExit(
                "Doc text mismatch — scored predictions do not align with gold "
                "DocBin. Re-run predict_hf_with_scores against the same raw.json."
            )
        char_spans: list[tuple[int, int, str]] = []
        for p in pred_record.get("predictions", []):
            label = str(p["label"])
            score = float(p.get("score", 0.0))
            if score < float(label_thresholds.get(label, 0.0)):
                continue
            char_spans.append((int(p["start"]), int(p["end"]), label))

        pred_doc = _align_pred_ents_to_gold(blank_nlp, gold_doc, char_spans)
        examples.append(Example(pred_doc, gold_doc))
        if not gold_doc.ents:
            negative_docs += 1
            negative_fp_total += len(pred_doc.ents)
            if not pred_doc.ents:
                clean_negative_docs += 1

    scores = Scorer().score(examples)
    scores["num_docs"] = len(gold_docs)
    scores["negative_docs"] = negative_docs
    scores["negative_clean_docs"] = clean_negative_docs
    scores["negative_doc_clean_rate"] = (
        clean_negative_docs / negative_docs if negative_docs else 0
    )
    scores["negative_fp_total"] = negative_fp_total
    scores["label_thresholds"] = dict(label_thresholds)
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
    parser.add_argument("--model", default=None, help="spaCy model path")
    parser.add_argument(
        "--hf-model",
        default=None,
        help="HuggingFace token-classification model path (directory). "
             "Mutually optional with --model; one of the two must be set.",
    )
    parser.add_argument(
        "--hf-entry-key",
        default=None,
        help="scores.json key for the HF model (default: derived from "
             "directory name, e.g. hf-ja-v02-tiny-hardneg → hf_v02_tiny_hardneg)",
    )
    parser.add_argument("--language", default="ja", choices=["ja", "en"])
    parser.add_argument("--version", default=LATEST_BENCHMARK_VERSION, choices=BENCHMARK_VERSIONS)
    parser.add_argument("--benchmark-data", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge into the existing scores.json instead of overwriting "
             "(use to add an HF entry alongside the existing spaCy entry).",
    )
    args = parser.parse_args()

    if not args.model and not args.hf_model:
        parser.error("at least one of --model / --hf-model is required")

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

    if args.model:
        print(f"Evaluating spaCy {args.model}...")
        results["pleno_ner"] = evaluate_on_benchmark(args.model, benchmark_path)

    if args.hf_model:
        print(f"Evaluating HF {args.hf_model}...")
        hf_scores = evaluate_hf_on_benchmark(
            args.hf_model, benchmark_path, language=args.language
        )
        entry_key = args.hf_entry_key or _derive_hf_entry_key(args.hf_model)
        results[entry_key] = hf_scores

    print_benchmark_report(results, args.language, config.suite_kind)

    # 結果保存
    output_path = args.output_json or (
        data_root / "benchmark" / args.version / args.language / "scores.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # scores から non-serializable な値を除去 + triad metadata 注入 (#70)
    serializable = {}
    for model_name, scores in results.items():
        if model_name == "_version":
            continue
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
            # 比較表で metric/aggregation/corpus を一意に identify するための triad
            "corpus": f"{args.version}/{args.language}",
            "metric": "strict_span_f1",
            "aggregation": "micro",
        }

    if args.merge and output_path.exists():
        try:
            with open(output_path, encoding="utf-8") as f:
                existing = json.load(f)
        except json.JSONDecodeError:
            existing = {}
        existing.update(serializable)
        serializable = existing

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nScores saved to {output_path}")


if __name__ == "__main__":
    main()
