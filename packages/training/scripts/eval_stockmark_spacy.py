"""Run spaCy ja_core_news_lg on stockmark Wikipedia NER for baseline."""
from __future__ import annotations
import argparse, json, time
from pathlib import Path
import numpy as np

PII_RELEVANT = {"人名", "地名"}


def iou(a, b):
    s = max(a[0], b[0]); e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def score(rows, iou_thr, gold_filter=None):
    per_doc = []
    for r in rows:
        gold = [(s[0], s[1], s[2]) for s in r["gold"]
                if gold_filter is None or s[2] in gold_filter]
        if not gold: continue
        pred = r["pred"]
        matched = set(); tp = fn = 0
        for g_s, g_e, _ in gold:
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
        per_doc.append((tp, fp, fn))
    return np.array(per_doc) if per_doc else np.zeros((0, 3), dtype=int)


def boot(arr, iters=1000, seed=42):
    if not len(arr): return {"P": 0, "R": 0, "F1": 0, "f1_ci_95": [0, 0], "n": 0}
    s = arr.sum(axis=0); tp, fp, fn = s
    P = tp / (tp + fp) if tp + fp else 0
    R = tp / (tp + fn) if tp + fn else 0
    F = 2 * P * R / (P + R) if P + R else 0
    n = len(arr)
    rng = np.random.default_rng(seed); bs = []
    for _ in range(iters):
        idx = rng.integers(0, n, size=n)
        ss = arr[idx].sum(axis=0); t, f, fn2 = ss
        p = t / (t + f) if t + f else 0
        r = t / (t + fn2) if t + fn2 else 0
        bs.append(2 * p * r / (p + r) if p + r else 0)
    bs = np.array(bs)
    return {
        "P": round(P, 4), "R": round(R, 4), "F1": round(F, 4),
        "f1_ci_95": [round(float(np.percentile(bs, 2.5)), 4),
                     round(float(np.percentile(bs, 97.5)), 4)],
        "n_docs": int(n),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="ja_core_news_lg")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from datasets import load_dataset
    import spacy
    nlp = spacy.load(args.model)

    rows_data = []
    t0 = time.perf_counter()
    for i, row in enumerate(load_dataset("stockmark/ner-wikipedia-dataset", split="train", streaming=True)):
        if i >= args.limit: break
        text = row["text"]
        gold = [(e["span"][0], e["span"][1], e["type"]) for e in row["entities"]]
        doc = nlp(text)
        pred = [(ent.start_char, ent.end_char, ent.label_) for ent in doc.ents]
        rows_data.append({"gold": gold, "pred": pred})

    summary = {
        "model": args.model,
        "dataset": "stockmark/ner-wikipedia-dataset",
        "limit": args.limit,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "full_protocol": boot(score(rows_data, 0.5)),
        "pii_relevant_subset": {**boot(score(rows_data, 0.5, PII_RELEVANT)),
                                "categories": sorted(PII_RELEVANT)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
