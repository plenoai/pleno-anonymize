"""Bootstrap 95% CI for char-IoU label-agnostic F1/P/R.

Reads per-document TP/FP/FN counts (or re-derives them from cached
predictions + gold) and resamples with replacement at the document
level to produce 95% confidence intervals.

Usage:
    python scripts/compute_ci_bootstrap.py \\
        --model packages/training/output/ja-ner-supervised-v2/model-best \\
        --data packages/training/data/raw/ja-300k-supervised/dev.jsonl \\
        --limit 300 --bootstrap 1000 \\
        --output output/v2-indist-ci.json
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np


def iou(a, b):
    s = max(a[0], b[0]); e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def decode(offsets, labels):
    spans = []
    cur_l = cur_s = cur_e = None
    for off, pred in zip(offsets, labels):
        if tuple(off) == (0, 0):
            continue
        if pred == "O":
            if cur_l is not None:
                spans.append((cur_s, cur_e, cur_l)); cur_l = cur_s = cur_e = None
            continue
        bio, _, lab = pred.partition("-")
        if bio == "B" or cur_l != lab:
            if cur_l is not None:
                spans.append((cur_s, cur_e, cur_l))
            cur_l = lab; cur_s = off[0]; cur_e = off[1]
        else:
            cur_e = off[1]
    if cur_l is not None:
        spans.append((cur_s, cur_e, cur_l))
    return spans


def score_doc(gold, pred, iou_thr):
    matched = set(); tp = fn = 0
    for g_s, g_e, g_l in gold:
        best_i, best_iou = -1, 0.0
        for i, p in enumerate(pred):
            if i in matched: continue
            v = iou((g_s, g_e), (p[0], p[1]))
            if v > best_iou: best_iou = v; best_i = i
        if best_i >= 0 and best_iou >= iou_thr:
            tp += 1; matched.add(best_i)
        else:
            fn += 1
    fp = len(pred) - len(matched)
    return tp, fp, fn


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    mdl = AutoModelForTokenClassification.from_pretrained(args.model).eval()
    id2lab = mdl.config.id2label

    per_doc = []  # list of (tp, fp, fn)
    t0 = time.perf_counter()
    rows = []
    for line in args.data.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        rows.append(json.loads(line))
        if args.limit and len(rows) >= args.limit: break

    for row in rows:
        text = row["text"]
        gold = [(int(e["start"]), int(e["end"]), str(e["label"])) for e in row["entities"]]
        enc = tok(text, return_offsets_mapping=True, truncation=True, max_length=512, return_tensors="pt")
        offs = enc.pop("offset_mapping")[0].tolist()
        with torch.inference_mode():
            logits = mdl(**{k: v.to(mdl.device) for k, v in enc.items()}).logits[0]
        labs = [id2lab[i] for i in logits.argmax(-1).tolist()]
        pred = decode(offs, labs)
        per_doc.append(score_doc(gold, pred, args.iou))

    arr = np.array(per_doc)  # (n_docs, 3) -- [tp, fp, fn]

    def metrics(idx):
        s = arr[idx].sum(axis=0)
        tp, fp, fn = s
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        return p, r, f

    n = len(arr)
    rng = np.random.default_rng(args.seed)
    boot = []
    for _ in range(args.bootstrap):
        idx = rng.integers(0, n, size=n)
        boot.append(metrics(idx))
    boot = np.array(boot)  # (B, 3) -- [P, R, F1]

    point = metrics(np.arange(n))
    pct = lambda col: (float(np.percentile(boot[:, col], 2.5)), float(np.percentile(boot[:, col], 97.5)))

    summary = {
        "model": args.model,
        "data": str(args.data),
        "n_docs": int(n),
        "iou": args.iou,
        "bootstrap_iters": args.bootstrap,
        "seed": args.seed,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "point_estimate": {
            "precision": round(point[0], 4),
            "recall":    round(point[1], 4),
            "f1":        round(point[2], 4),
        },
        "ci_95": {
            "precision": [round(x, 4) for x in pct(0)],
            "recall":    [round(x, 4) for x in pct(1)],
            "f1":        [round(x, 4) for x in pct(2)],
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
