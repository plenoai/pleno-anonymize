"""Real-text EN OOD eval against CoNLL-2003 (Reuters news, hand-annotated).

The English counterpart to `eval_stockmark_jp_real.py`. CoNLL-2003 has
4 entity categories: PER, ORG, LOC, MISC. Of these, only PER and LOC
are clean PII targets; ORG and MISC are out of scope for a PII NER.

We report two numbers, both char-IoU >= 0.5 label-agnostic with
1000-iter document-level bootstrap CIs:
- full: over all 4 CoNLL categories
- pii-subset: gold restricted to {PER, LOC}

CoNLL-2003 ships token sequences with IOB2 NER tags, not character
spans, so we reconstruct text by joining tokens with single spaces
(the standard convention for English CoNLL detokenisation) and derive
char offsets from the join. This matches how transformers tokenisers
will see the text at inference.

Usage::

    uv run --extra hf python scripts/eval_conll_en_real.py \\
        --model 0xhikae/pleno_anonymize_en \\
        --limit 300 --output ../../output/conll-en-pleno-en.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np


PII_RELEVANT = {"PER", "LOC"}

# Label-aware merge for pleno_anonymize_en predictions. Collapse
# contiguous sub-spans only when both labels map to the same coarse
# equivalence class; otherwise leave separate.
COARSE_CLASSES: dict[str, set[str]] = {
    "PERSON":  {"LASTNAME1", "LASTNAME2", "LASTNAME3", "GIVENNAME1", "GIVENNAME2", "TITLE", "USERNAME"},
    "ADDRESS": {"STREET", "CITY", "STATE", "POSTCODE", "BUILDING", "SECADDRESS", "COUNTRY", "GEOCOORD"},
    "DATE":    {"BOD", "DATE", "TIME"},
    "PHONE":   {"TEL"},
    "EMAIL":   {"EMAIL"},
    "ID":      {"IDCARD", "DRIVERLICENSE", "PASSPORT", "PASS", "SOCIALNUMBER"},
    "CARD":    {"CARDISSUER"},
    "IP":      {"IP"},
    "SEX":     {"SEX"},
}


def iou(a, b):
    s = max(a[0], b[0]); e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def tokens_to_text_and_gold(tokens: list[str], ner_tags: list[int], id2tag: dict[int, str]) -> tuple[str, list[tuple[int, int, str]]]:
    """Join tokens with spaces; reconstruct char-span gold from IOB2."""
    starts: list[int] = []
    cursor = 0
    parts: list[str] = []
    for tok in tokens:
        if parts:
            parts.append(" ")
            cursor += 1
        starts.append(cursor)
        parts.append(tok)
        cursor += len(tok)
    text = "".join(parts)

    gold: list[tuple[int, int, str]] = []
    cur_label: str | None = None
    cur_start = cur_end = -1
    for i, tag_id in enumerate(ner_tags):
        tag = id2tag[int(tag_id)]
        if tag == "O":
            if cur_label is not None:
                gold.append((cur_start, cur_end, cur_label))
                cur_label = None
            continue
        bio, _, lab = tag.partition("-")
        tok_s = starts[i]
        tok_e = starts[i] + len(tokens[i])
        if bio == "B" or cur_label != lab:
            if cur_label is not None:
                gold.append((cur_start, cur_end, cur_label))
            cur_label = lab
            cur_start = tok_s
            cur_end = tok_e
        else:
            cur_end = tok_e
    if cur_label is not None:
        gold.append((cur_start, cur_end, cur_label))
    return text, gold


def decode_label_aware(text, tok, mdl, id2lab):
    enc = tok(text, return_offsets_mapping=True, truncation=True,
              max_length=512, return_tensors="pt")
    offs = enc.pop("offset_mapping")[0].tolist()
    import inspect
    import torch
    accepted = set(inspect.signature(mdl.forward).parameters)
    kwargs = {k: v.to(mdl.device) for k, v in enc.items() if k in accepted}
    with torch.inference_mode():
        logits = mdl(**kwargs).logits[0]
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


def score(rows, iou_thr, gold_filter=None):
    per_doc = []
    for r in rows:
        gold = [(s, e, l) for s, e, l in r["gold"]
                if gold_filter is None or l in gold_filter]
        if not gold:
            continue
        pred = r["pred"]
        matched = set(); tp = fn = 0
        for g_s, g_e, _ in gold:
            best_i, best_iou = -1, 0.0
            for i, p in enumerate(pred):
                if i in matched: continue
                v = iou((g_s, g_e), (p[0], p[1]))
                if v > best_iou:
                    best_iou = v; best_i = i
            if best_i >= 0 and best_iou >= iou_thr:
                tp += 1; matched.add(best_i)
            else:
                fn += 1
        fp = len(pred) - len(matched)
        per_doc.append((tp, fp, fn))
    return np.array(per_doc) if per_doc else np.zeros((0, 3), dtype=int)


def bootstrap_ci(arr, iters=1000, seed=42):
    if len(arr) == 0:
        return {"P": 0, "R": 0, "F1": 0, "f1_ci_95": [0, 0], "n_docs": 0}
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
        "P": round(float(P), 4), "R": round(float(R), 4), "F1": round(float(F), 4),
        "f1_ci_95": [round(float(np.percentile(boot, 2.5)), 4),
                     round(float(np.percentile(boot, 97.5)), 4)],
        "n_docs": int(n),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", default="eriktks/conll2003")
    parser.add_argument("--split", default="test")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--min-gold", type=int, default=1,
                        help="skip docs with fewer than this many gold spans (CoNLL has many label-free sentences)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from datasets import load_dataset
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    mdl = AutoModelForTokenClassification.from_pretrained(args.model).eval()
    id2lab = mdl.config.id2label

    ds = load_dataset(args.dataset, split=args.split)
    # eriktks/conll2003 (script-based) was removed; tner/conll2003 is parquet
    # but lacks ClassLabel metadata. Detect which column carries tags and
    # supply a label list if absent.
    tag_col = "ner_tags" if "ner_tags" in ds.column_names else "tags"
    feat = ds.features[tag_col]
    if hasattr(feat, "feature") and hasattr(feat.feature, "names"):
        id2tag = {i: t for i, t in enumerate(feat.feature.names)}
    else:
        # tner/conll2003 label2id (verified on hub model card).
        id2tag = {0: "O", 1: "B-ORG", 2: "B-MISC", 3: "B-PER", 4: "I-PER",
                  5: "B-LOC", 6: "I-ORG", 7: "I-MISC", 8: "I-LOC"}

    rows_data = []
    seen_with_gold = 0
    t0 = time.perf_counter()
    for row in ds:
        text, gold = tokens_to_text_and_gold(row["tokens"], row[tag_col], id2tag)
        if len(gold) < args.min_gold:
            continue
        pred = decode_label_aware(text, tok, mdl, id2lab)
        rows_data.append({"gold": gold, "pred": pred})
        seen_with_gold += 1
        if seen_with_gold >= args.limit:
            break

    arr_full = score(rows_data, args.iou, gold_filter=None)
    arr_pii  = score(rows_data, args.iou, gold_filter=PII_RELEVANT)
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
        "all_categories": sorted({g[2] for r in rows_data for g in r["gold"]}),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
