"""Benchmark a HuggingFace token-classification NER on ai4privacy datasets.

Uses the exact char-IoU >= 0.5, label-agnostic scoring protocol that
`packages/sdk/scripts/eval_pii_masking_300k.py` applies to the
SDK-shipped engines. Lets us compare any HF NER model against the
public ruler without plumbing it through the SDK first.

Example:
    uv run --extra training --extra hf python \\
        scripts/eval_on_300k.py \\
        --model 0xhikae/pleno_anonymize_ja \\
        --dataset 0xhikae/pii-masking-300k-ja \\
        --language Japanese --limit 300 \\
        --output ../../output/pii-300k-ja-eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    latency_ms: float = 0.0
    n_docs: int = 0
    n_gold_spans: int = 0
    n_pred_spans: int = 0
    per_label_tp: dict[str, int] = field(default_factory=dict)
    per_label_fn: dict[str, int] = field(default_factory=dict)

    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 0.0

    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 0.0

    def f1(self) -> float:
        p, r = self.precision(), self.recall()
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    s = max(a[0], b[0])
    e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def _parse_gold(row: dict) -> list[tuple[int, int, str]]:
    raw = row.get("span_labels") or row.get("entities") or row.get("privacy_mask")
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    spans: list[tuple[int, int, str]] = []
    for s in raw:
        if isinstance(s, list) and len(s) >= 3:
            spans.append((int(s[0]), int(s[1]), str(s[2])))
        elif isinstance(s, dict) and {"start", "end"} <= s.keys():
            spans.append((int(s["start"]), int(s["end"]), str(s.get("label", "?"))))
    return spans


def _decode_to_spans(text: str, tokens: list, predictions: list[str]) -> list[tuple[int, int, str]]:
    """Merge contiguous B-/I- tokens into char-offset spans."""
    spans: list[tuple[int, int, str]] = []
    cur_label: str | None = None
    cur_start: int | None = None
    cur_end: int | None = None
    for tok, pred in zip(tokens, predictions):
        offset = tok["offset"]
        if offset == (0, 0):
            continue
        if pred == "O":
            if cur_label is not None:
                spans.append((cur_start, cur_end, cur_label))
                cur_label = cur_start = cur_end = None
            continue
        bio, _, label = pred.partition("-")
        if bio == "B" or cur_label != label:
            if cur_label is not None:
                spans.append((cur_start, cur_end, cur_label))
            cur_label = label
            cur_start = offset[0]
            cur_end = offset[1]
        else:
            cur_end = offset[1]
    if cur_label is not None:
        spans.append((cur_start, cur_end, cur_label))
    return spans


def _predict(model, tokenizer, text: str) -> list[tuple[int, int, str]]:
    import torch

    enc = tokenizer(
        text,
        return_offsets_mapping=True,
        truncation=True,
        max_length=512,
        return_tensors="pt",
    )
    offsets = enc.pop("offset_mapping")[0].tolist()
    enc = {k: v.to(model.device) for k, v in enc.items()}
    with torch.inference_mode():
        logits = model(**enc).logits[0]
    pred_ids = logits.argmax(-1).tolist()
    id2label = model.config.id2label
    tokens = [{"offset": tuple(o)} for o in offsets]
    labels = [id2label[i] for i in pred_ids]
    return _decode_to_spans(text, tokens, labels)


def _score(gold, pred, iou_threshold, counts) -> None:
    matched_pred: set[int] = set()
    for g_start, g_end, g_label in gold:
        counts.per_label_fn.setdefault(g_label, 0)
        counts.per_label_tp.setdefault(g_label, 0)
        best_idx = -1
        best_iou = 0.0
        for i, p in enumerate(pred):
            if i in matched_pred:
                continue
            iou = _iou((g_start, g_end), (p[0], p[1]))
            if iou > best_iou:
                best_iou = iou
                best_idx = i
        if best_idx >= 0 and best_iou >= iou_threshold:
            counts.tp += 1
            counts.per_label_tp[g_label] += 1
            matched_pred.add(best_idx)
        else:
            counts.fn += 1
            counts.per_label_fn[g_label] += 1
    counts.fp += len(pred) - len(matched_pred)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, help="HF repo id or local path to a token-classification model")
    parser.add_argument("--dataset", default="0xhikae/pii-masking-300k-ja")
    parser.add_argument("--split", default="validation")
    parser.add_argument("--language", default="Japanese")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from datasets import load_dataset
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    print(f"[load] model={args.model}")
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForTokenClassification.from_pretrained(args.model)
    model.eval()

    print(f"[load] dataset={args.dataset} split={args.split}")
    ds = load_dataset(args.dataset, split=args.split, streaming=True)

    counts = Counts()
    seen = 0
    for row in ds:
        if args.language and row.get("language") != args.language:
            continue
        text = row.get("source_text") or row.get("text")
        if not text:
            continue
        gold = _parse_gold(row)
        t0 = time.perf_counter()
        try:
            pred = _predict(model, tokenizer, text)
        except Exception as e:  # noqa: BLE001
            print(f"[predict] error: {e}", file=sys.stderr)
            pred = []
        counts.latency_ms += (time.perf_counter() - t0) * 1000
        counts.n_docs += 1
        counts.n_gold_spans += len(gold)
        counts.n_pred_spans += len(pred)
        _score(gold, pred, args.iou, counts)
        seen += 1
        if seen >= args.limit:
            break

    summary = {
        "model": args.model,
        "dataset": args.dataset,
        "split": args.split,
        "language": args.language,
        "limit": args.limit,
        "iou": args.iou,
        "tp": counts.tp,
        "fp": counts.fp,
        "fn": counts.fn,
        "precision": round(counts.precision(), 4),
        "recall": round(counts.recall(), 4),
        "f1": round(counts.f1(), 4),
        "n_docs": counts.n_docs,
        "n_gold_spans": counts.n_gold_spans,
        "n_pred_spans": counts.n_pred_spans,
        "avg_latency_ms": round(counts.latency_ms / max(counts.n_docs, 1), 2),
        "per_label_recall": {
            l: round(counts.per_label_tp[l] / max(counts.per_label_tp[l] + counts.per_label_fn[l], 1), 4)
            for l in counts.per_label_tp
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
