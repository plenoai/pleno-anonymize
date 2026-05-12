"""Benchmark pleno-anonymize backends against ai4privacy/pii-masking-300k.

Compares the default `builtin` engine (Presidio + spaCy NER) against
`openai-privacy-filter` (the OPF open-source model) on the validation split
of https://huggingface.co/datasets/ai4privacy/pii-masking-300k.

Span-level scoring is label-agnostic: a predicted span counts as a true
positive if it overlaps a gold span with character IoU >= ``--iou`` (default
0.5). We deliberately avoid label-matching because the dataset uses 27+
fine-grained classes that don't line up 1:1 with either backend's
taxonomy — answering "did we mask any sensitive token here?" is what the
proxy actually cares about.

Usage:
    uv run python scripts/eval_pii_masking_300k.py \
        --engines builtin openai-privacy-filter \
        --language English \
        --limit 1000 \
        --output ../../output/pii-300k-eval.json

Requires:
    pip install "pleno-anonymize[openai]" datasets
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Allow `uv run python scripts/...` without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pleno_anonymize import Finding, PlenoAnonymize  # noqa: E402


@dataclass(slots=True)
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    latency_ms: float = 0.0
    n_docs: int = 0
    n_gold_spans: int = 0
    n_pred_spans: int = 0
    errors: int = 0
    per_label_tp: dict[str, int] = field(default_factory=dict)
    per_label_fn: dict[str, int] = field(default_factory=dict)

    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    def f1(self) -> float:
        p, r = self.precision(), self.recall()
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "tp": self.tp,
            "fp": self.fp,
            "fn": self.fn,
            "precision": round(self.precision(), 4),
            "recall": round(self.recall(), 4),
            "f1": round(self.f1(), 4),
            "n_docs": self.n_docs,
            "n_gold_spans": self.n_gold_spans,
            "n_pred_spans": self.n_pred_spans,
            "errors": self.errors,
            "avg_latency_ms": round(self.latency_ms / max(self.n_docs, 1), 2),
            "per_label_recall": {
                label: round(
                    self.per_label_tp.get(label, 0)
                    / max(
                        self.per_label_tp.get(label, 0)
                        + self.per_label_fn.get(label, 0),
                        1,
                    ),
                    4,
                )
                for label in sorted(
                    set(self.per_label_tp) | set(self.per_label_fn)
                )
            },
        }


def _iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    s = max(a[0], b[0])
    e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def _parse_gold(row: dict) -> list[tuple[int, int, str]]:
    """Pull (start, end, label) tuples from ai4privacy's `span_labels`."""
    raw = row.get("span_labels")
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
    return spans


def _score(
    gold: list[tuple[int, int, str]],
    pred: list[Finding],
    iou_threshold: float,
    counts: Counts,
) -> None:
    matched_pred: set[int] = set()
    for g_start, g_end, g_label in gold:
        counts.per_label_fn.setdefault(g_label, 0)
        counts.per_label_tp.setdefault(g_label, 0)
        best_idx = -1
        best_iou = 0.0
        for i, p in enumerate(pred):
            if i in matched_pred:
                continue
            iou = _iou((g_start, g_end), (p.start, p.end))
            if iou > best_iou:
                best_iou = iou
                best_idx = i
        if best_iou >= iou_threshold:
            counts.tp += 1
            counts.per_label_tp[g_label] += 1
            matched_pred.add(best_idx)
        else:
            counts.fn += 1
            counts.per_label_fn[g_label] += 1
    counts.fp += len(pred) - len(matched_pred)


def _iter_dataset(language: str, limit: int) -> Iterable[dict]:
    from datasets import load_dataset  # type: ignore[import-not-found]

    ds = load_dataset(
        "ai4privacy/pii-masking-300k", split="validation", streaming=True
    )
    yielded = 0
    for row in ds:
        if language and row.get("language") != language:
            continue
        yield row
        yielded += 1
        if yielded >= limit:
            return


def _eval_engine(
    name: str,
    engine,
    rows: list[dict],
    iou_threshold: float,
    pleno_language: str,
) -> Counts:
    counts = Counts()
    for row in rows:
        text = row["source_text"]
        gold = _parse_gold(row)
        counts.n_docs += 1
        counts.n_gold_spans += len(gold)
        t0 = time.perf_counter()
        try:
            findings = engine.analyze(text, language=pleno_language)
        except Exception as exc:  # pragma: no cover - benchmark loop
            counts.errors += 1
            sys.stderr.write(f"[{name}] error on doc: {exc}\n")
            continue
        counts.latency_ms += (time.perf_counter() - t0) * 1000
        counts.n_pred_spans += len(findings)
        _score(gold, findings, iou_threshold, counts)
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Benchmark pleno engines on ai4privacy/pii-masking-300k"
    )
    p.add_argument(
        "--engines",
        nargs="+",
        default=["builtin", "openai-privacy-filter"],
        choices=("builtin", "openai-privacy-filter"),
    )
    p.add_argument("--language", default="English", help="dataset language filter")
    p.add_argument(
        "--pleno-language",
        default="en",
        choices=("ja", "en"),
        help="language passed to pleno engines",
    )
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--iou", type=float, default=0.5)
    p.add_argument("--output", default=None, help="write JSON results here")
    p.add_argument(
        "--opf-device",
        default=None,
        choices=("cpu", "cuda"),
        help="device hint for openai-privacy-filter (auto-detected if omitted)",
    )
    args = p.parse_args(argv)

    sys.stderr.write(
        f"loading up to {args.limit} {args.language!r} validation rows ...\n"
    )
    rows = list(_iter_dataset(args.language, args.limit))
    sys.stderr.write(f"loaded {len(rows)} rows\n")

    results: dict[str, dict[str, object]] = {}
    for name in args.engines:
        sys.stderr.write(f"[{name}] warming up ...\n")
        engine = PlenoAnonymize(
            engine=name,
            languages=(args.pleno_language,),
            opf_device=args.opf_device,
        )
        counts = _eval_engine(name, engine, rows, args.iou, args.pleno_language)
        results[name] = counts.to_dict()
        sys.stderr.write(
            f"[{name}] P={counts.precision():.3f} R={counts.recall():.3f} "
            f"F1={counts.f1():.3f} avg={counts.latency_ms / max(counts.n_docs, 1):.1f}ms\n"
        )

    payload = {
        "dataset": "ai4privacy/pii-masking-300k",
        "split": "validation",
        "language": args.language,
        "limit": args.limit,
        "iou_threshold": args.iou,
        "n_docs": len(rows),
        "results": results,
    }
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json_text + "\n", encoding="utf-8")
        sys.stderr.write(f"wrote {out}\n")
    else:
        print(json_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
