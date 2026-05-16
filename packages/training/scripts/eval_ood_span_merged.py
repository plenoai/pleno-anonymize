"""OOD eval with **label-aware** span merging.

Only adjacent v2 predictions whose labels belong to the same coarse
equivalence class are merged. Adjacent predictions with semantically
unrelated labels stay separate.

This replaces an earlier label-blind implementation that merged any
contiguous run of non-O tokens regardless of label — peer reviewer
correctly flagged that as inflating F1 by collapsing distinct
entities (e.g. PERSON,PHONE adjacent in form text).

Equivalence classes are documented in `COARSE_CLASSES` below and
reflect how the v1 (pleno) OOD set's coarse labels relate to v2's
fine-grained ai4privacy labels.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np


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


def _label_to_class(label: str) -> str | None:
    # Already a coarse class name (post-merge): return as-is.
    if label in COARSE_CLASSES:
        return label
    for cls, members in COARSE_CLASSES.items():
        if label in members:
            return cls
    return None


def iou(a, b):
    s = max(a[0], b[0]); e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def predict_label_aware_merged(text, tok, mdl, id2lab, gap_chars=2):
    enc = tok(text, return_offsets_mapping=True, truncation=True,
              max_length=512, return_tensors="pt")
    offs = enc.pop("offset_mapping")[0].tolist()
    import torch
    with torch.inference_mode():
        logits = mdl(**{k: v.to(mdl.device) for k, v in enc.items()}).logits[0]
    labs = [id2lab[i] for i in logits.argmax(-1).tolist()]

    # Step 1: BIO decode preserving fine labels
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

    # Step 2: label-aware merge — only collapse within same coarse class
    merged: list[list] = []
    for s, e, lab in typed:
        cls = _label_to_class(lab)
        if cls is None:
            merged.append([s, e, lab])
            continue
        if merged:
            prev = merged[-1]
            prev_cls = _label_to_class(prev[2])
            if prev_cls == cls and (s - prev[1]) <= gap_chars:
                prev[1] = e
                prev[2] = cls
                continue
        merged.append([s, e, cls])

    return [(m[0], m[1], m[2]) for m in merged]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--bootstrap", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from transformers import AutoModelForTokenClassification, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.model)
    mdl = AutoModelForTokenClassification.from_pretrained(args.model).eval()
    id2lab = mdl.config.id2label

    per_doc = []
    t0 = time.perf_counter()
    for line in args.data.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        gold = [(int(e["start"]), int(e["end"]), str(e["label"])) for e in r["entities"]]
        pred = predict_label_aware_merged(r["text"], tok, mdl, id2lab)
        matched = set()
        tp = fn = 0
        for g_s, g_e, _g_l in gold:
            best_i, best_iou = -1, 0.0
            for i, p in enumerate(pred):
                if i in matched:
                    continue
                v = iou((g_s, g_e), (p[0], p[1]))
                if v > best_iou:
                    best_iou = v; best_i = i
            if best_i >= 0 and best_iou >= args.iou:
                tp += 1; matched.add(best_i)
            else:
                fn += 1
        fp = len(pred) - len(matched)
        per_doc.append((tp, fp, fn))

    arr = np.array(per_doc)
    n = len(arr)
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
        "coarse_classes": {k: sorted(v) for k, v in COARSE_CLASSES.items()},
        "merge_rule": "Adjacent v2 sub-spans merged only when both labels map to the same coarse equivalence class.",
        "point": {"P": round(P, 4), "R": round(R, 4), "F1": round(F, 4)},
        "f1_ci_95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
        "elapsed_sec": round(time.perf_counter() - t0, 1),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
