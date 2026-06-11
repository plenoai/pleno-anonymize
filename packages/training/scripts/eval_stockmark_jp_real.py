"""Real-text JP OOD eval against stockmark/ner-wikipedia-dataset.

Real Japanese Wikipedia sentences with 8 entity categories:
人名, 法人名, 政治的組織名, その他の組織名, 地名, 施設名, 製品名, イベント名.

Only 人名 and 地名 are clean PII targets. The rest (corporations, products,
events, facilities) are out-of-scope for a PII NER like v2 by design.

We report two numbers:
- full: char-IoU >= 0.5, label-agnostic over all 8 stockmark categories
- pii-subset: same, but restricting gold to {人名, 地名}

This is the first **real Japanese text** in the v2 eval suite. The
peer reviewer flagged the absence of real-text eval as the structural
Accept blocker; this addresses it (Wikipedia text is not full
production "chat / form / scanned" coverage, but it is genuine non-
LLM-synthesised JP).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

PII_RELEVANT = {"人名", "地名"}

# Label-aware merge: collapse contiguous v2 sub-spans only when both
# labels map to the same coarse equivalence class.
COARSE_CLASSES: dict[str, set[str]] = {
    "PERSON":        {"LASTNAME1", "LASTNAME2", "LASTNAME3", "GIVENNAME1", "GIVENNAME2", "TITLE", "USERNAME"},
    "ADDRESS":       {"STREET", "CITY", "STATE", "POSTCODE", "BUILDING", "SECADDRESS", "COUNTRY", "GEOCOORD"},
    "DATE_OF_BIRTH": {"BOD", "DATE", "TIME"},
    "PHONE":         {"TEL"},
    "EMAIL":         {"EMAIL"},
    "ID_CARD":       {"IDCARD", "DRIVERLICENSE", "PASSPORT", "PASS", "SOCIALNUMBER"},
    "CARD":          {"CARDISSUER"},
    "IP":            {"IP"},
    "SEX":           {"SEX"},
}


def iou(a, b):
    s = max(a[0], b[0]); e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def decode_label_aware(text, tok, mdl, id2lab, COARSE_CLASSES):
    enc = tok(text, return_offsets_mapping=True, truncation=True,
              max_length=512, return_tensors="pt")
    offs = enc.pop("offset_mapping")[0].tolist()
    import torch
    with torch.inference_mode():
        logits = mdl(**{k: v.to(mdl.device) for k, v in enc.items()}).logits[0]
    labs = [id2lab[i] for i in logits.argmax(-1).tolist()]

    typed: list[tuple[int, int, str]] = []
    cur_l = cur_s = cur_e = None
    for off, p in zip(offs, labs):
        if tuple(off) == (0, 0):
            continue
        if p == "O":
            if cur_l is not None:
                typed.append((cur_s, cur_e, cur_l))
                cur_l = cur_s = cur_e = None
            continue
        bio, _, lab = p.partition("-")
        if bio == "B" or cur_l != lab:
            if cur_l is not None:
                typed.append((cur_s, cur_e, cur_l))
            cur_l = lab; cur_s = off[0]; cur_e = off[1]
        else:
            cur_e = off[1]
    if cur_l is not None:
        typed.append((cur_s, cur_e, cur_l))

    def label_class(lbl):
        if lbl in COARSE_CLASSES: return lbl
        for cls, mem in COARSE_CLASSES.items():
            if lbl in mem: return cls
        return None

    merged: list[list] = []
    for s, e, lab in typed:
        cls = label_class(lab)
        if cls is None:
            merged.append([s, e, lab])
            continue
        if merged:
            prev = merged[-1]
            if label_class(prev[2]) == cls and (s - prev[1]) <= 2:
                prev[1] = e; prev[2] = cls
                continue
        merged.append([s, e, cls])
    return [(m[0], m[1], m[2]) for m in merged]


def score(rows, pred_fn, iou_thr, gold_filter=None):
    per_doc = []
    for r in rows:
        gold = [(int(s[0]), int(s[1]), str(s[2])) for s in r["gold"]
                if gold_filter is None or s[2] in gold_filter]
        pred = r["pred"] if gold_filter is None else [
            p for p in r["pred"] if p[2] in gold_filter
        ]
        if not gold:
            if pred:
                per_doc.append((0, len(pred), 0))
            continue
        matched = set()
        tp = fn = 0
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
    arr = np.array(per_doc) if per_doc else np.zeros((0, 3), dtype=int)
    return arr


def bootstrap_ci(arr, iters=1000, seed=42):
    if len(arr) == 0:
        return {"P": 0, "R": 0, "F1": 0, "f1_ci": [0, 0], "n": 0}
    s = arr.sum(axis=0); tp, fp, fn = s
    P = tp / (tp + fp) if tp + fp else 0
    R = tp / (tp + fn) if tp + fn else 0
    F = 2 * P * R / (P + R) if P + R else 0
    n = len(arr)
    rng = np.random.default_rng(seed); boot = []
    for _ in range(iters):
        idx = rng.integers(0, n, size=n)
        ss = arr[idx].sum(axis=0); t, f, fn2 = ss
        p = t / (t + f) if t + f else 0
        r = t / (t + fn2) if t + fn2 else 0
        boot.append(2 * p * r / (p + r) if p + r else 0)
    boot = np.array(boot)
    return {
        "P": round(P, 4), "R": round(R, 4), "F1": round(F, 4),
        "f1_ci_95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
        "n_docs": int(n),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", default="stockmark/ner-wikipedia-dataset")
    parser.add_argument("--split", default="train")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from datasets import load_dataset
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    mdl = AutoModelForTokenClassification.from_pretrained(args.model).eval()
    id2lab = mdl.config.id2label

    rows_data = []
    t0 = time.perf_counter()
    for i, row in enumerate(load_dataset(args.dataset, split=args.split, streaming=True)):
        if i >= args.limit: break
        text = row["text"]
        gold = [(e["span"][0], e["span"][1], e["type"]) for e in row["entities"]]
        pred = decode_label_aware(text, tok, mdl, id2lab, COARSE_CLASSES)
        rows_data.append({"gold": gold, "pred": pred})

    arr_full = score(rows_data, None, args.iou, gold_filter=None)
    arr_pii  = score(rows_data, None, args.iou, gold_filter=PII_RELEVANT)
    full_metrics = bootstrap_ci(arr_full)
    pii_metrics  = bootstrap_ci(arr_pii)

    summary = {
        "model": args.model,
        "dataset": args.dataset,
        "split": args.split,
        "limit": args.limit,
        "iou": args.iou,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "full_protocol": full_metrics,
        "pii_relevant_subset": {**pii_metrics, "categories": sorted(PII_RELEVANT)},
        "all_stockmark_categories": sorted({g[2] for r in rows_data for g in r["gold"]}),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
