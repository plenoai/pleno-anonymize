"""Run a classic JP NER (spaCy / GiNZA) against a JSONL test set.

Same char-IoU >= 0.5 label-agnostic protocol as eval_on_300k.py.
Reviewer asked for >=1 non-PII-specific JP NER baseline to widen the
comparison set.

Usage:
    python scripts/eval_classic_baseline.py \\
        --model ja_core_news_lg \\
        --data /tmp/v2-ood-extended.jsonl \\
        --output output/spacy-ja-ood-extended.json
"""
from __future__ import annotations
import argparse, json, time
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np


def iou(a, b):
    s = max(a[0], b[0]); e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="spaCy model name (ja_core_news_lg, ja_ginza, etc.)")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import spacy
    nlp = spacy.load(args.model)

    rows = []
    for line in args.data.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
        if args.limit and len(rows) >= args.limit:
            break

    per_doc = []
    latencies = []
    for r in rows:
        text = r["text"]
        gold = [(int(e["start"]), int(e["end"]), str(e["label"])) for e in r["entities"]]
        t0 = time.perf_counter()
        doc = nlp(text)
        latencies.append((time.perf_counter() - t0) * 1000)
        pred = [(ent.start_char, ent.end_char, ent.label_) for ent in doc.ents]
        matched = set(); tp = fn = 0
        for g_s, g_e, _g_l in gold:
            best_i, best_iou = -1, 0.0
            for i, p in enumerate(pred):
                if i in matched: continue
                v = iou((g_s, g_e), (p[0], p[1]))
                if v > best_iou:
                    best_iou = v; best_i = i
            if best_i >= 0 and best_iou >= args.iou:
                tp += 1; matched.add(best_i)
            else:
                fn += 1
        fp = len(pred) - len(matched)
        per_doc.append((tp, fp, fn))

    arr = np.array(per_doc); n = len(arr)
    s = arr.sum(axis=0); tp, fp, fn = s
    P = tp / (tp + fp) if tp + fp else 0
    R = tp / (tp + fn) if tp + fn else 0
    F = 2 * P * R / (P + R) if P + R else 0

    rng = np.random.default_rng(args.seed); boot = []
    for _ in range(args.bootstrap):
        idx = rng.integers(0, n, size=n)
        ss = arr[idx].sum(axis=0); t, f, fn2 = ss
        p = t / (t + f) if t + f else 0
        r = t / (t + fn2) if t + fn2 else 0
        boot.append(2 * p * r / (p + r) if p + r else 0)
    boot = np.array(boot)

    summary = {
        "model": args.model,
        "data": str(args.data),
        "n_docs": int(n),
        "iou": args.iou,
        "bootstrap_iters": args.bootstrap,
        "point": {"P": round(P, 4), "R": round(R, 4), "F1": round(F, 4)},
        "f1_ci_95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
        "avg_latency_ms": round(sum(latencies) / max(len(latencies), 1), 2),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
