"""OOD eval: run a HF token-classification model against a local JSONL.

Same char-IoU >= 0.5, label-agnostic protocol as eval_mechanism_on_300k.py,
but reads gold spans from a JSONL file the model never saw at training.
Used to stress-test v2 against the synthetic v1 test split.
"""

from __future__ import annotations

import argparse
import json
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
    n_gold: int = 0
    n_pred: int = 0
    per_label_tp: dict = field(default_factory=dict)
    per_label_fn: dict = field(default_factory=dict)

    def p(self): d = self.tp + self.fp; return self.tp / d if d else 0.0
    def r(self): d = self.tp + self.fn; return self.tp / d if d else 0.0
    def f1(self):
        p, r = self.p(), self.r()
        return 2 * p * r / (p + r) if (p + r) else 0.0


def iou(a, b):
    s = max(a[0], b[0]); e = min(a[1], b[1])
    inter = max(0, e - s)
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union else 0.0


def decode(text, offsets, labels):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--data", type=Path, required=True, help="JSONL of {text, entities}")
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model)
    mdl = AutoModelForTokenClassification.from_pretrained(args.model).eval()
    id2lab = mdl.config.id2label

    c = Counts()
    for line in args.data.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        text = row["text"]
        gold = [(int(e["start"]), int(e["end"]), str(e["label"])) for e in row["entities"]]

        enc = tok(text, return_offsets_mapping=True, truncation=True, max_length=512, return_tensors="pt")
        offs = enc.pop("offset_mapping")[0].tolist()
        t0 = time.perf_counter()
        with torch.inference_mode():
            logits = mdl(**{k: v.to(mdl.device) for k, v in enc.items()}).logits[0]
        c.latency_ms += (time.perf_counter() - t0) * 1000
        labs = [id2lab[i] for i in logits.argmax(-1).tolist()]
        pred = decode(text, offs, labs)

        matched = set()
        for g_s, g_e, g_l in gold:
            c.per_label_fn.setdefault(g_l, 0); c.per_label_tp.setdefault(g_l, 0)
            best_i, best_iou = -1, 0.0
            for i, p in enumerate(pred):
                if i in matched:
                    continue
                v = iou((g_s, g_e), (p[0], p[1]))
                if v > best_iou:
                    best_iou = v; best_i = i
            if best_i >= 0 and best_iou >= args.iou:
                c.tp += 1; c.per_label_tp[g_l] += 1; matched.add(best_i)
            else:
                c.fn += 1; c.per_label_fn[g_l] += 1
        c.fp += len(pred) - len(matched)
        c.n_docs += 1; c.n_gold += len(gold); c.n_pred += len(pred)

    summary = {
        "model": args.model,
        "data": str(args.data),
        "n_docs": c.n_docs,
        "n_gold": c.n_gold,
        "n_pred": c.n_pred,
        "tp": c.tp, "fp": c.fp, "fn": c.fn,
        "precision": round(c.p(), 4),
        "recall": round(c.r(), 4),
        "f1": round(c.f1(), 4),
        "avg_latency_ms": round(c.latency_ms / max(c.n_docs, 1), 2),
        "per_label_recall": {
            l: round(c.per_label_tp[l] / max(c.per_label_tp[l] + c.per_label_fn[l], 1), 4)
            for l in c.per_label_tp
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
