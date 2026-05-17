"""spaCy classic baseline on CoNLL-2003 for the EN real-text eval.

Counterpart to `eval_stockmark_spacy.py`. Reuses CoNLL detokenisation
and char-IoU scoring from `eval_conll_en_real.py` so the two are
strictly comparable.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

from eval_conll_en_real import (  # type: ignore[import-not-found]
    PII_RELEVANT,
    bootstrap_ci,
    score,
    tokens_to_text_and_gold,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="en_core_web_lg",
                        help="spaCy model name (en_core_web_lg, en_core_web_trf, etc.)")
    parser.add_argument("--dataset", default="eriktks/conll2003")
    parser.add_argument("--split", default="test")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--iou", type=float, default=0.5)
    parser.add_argument("--min-gold", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from datasets import load_dataset
    import spacy

    nlp = spacy.load(args.model)
    ds = load_dataset(args.dataset, split=args.split)
    tag_col = "ner_tags" if "ner_tags" in ds.column_names else "tags"
    feat = ds.features[tag_col]
    if hasattr(feat, "feature") and hasattr(feat.feature, "names"):
        id2tag = {i: t for i, t in enumerate(feat.feature.names)}
    else:
        id2tag = {0: "O", 1: "B-ORG", 2: "B-MISC", 3: "B-PER", 4: "I-PER",
                  5: "B-LOC", 6: "I-ORG", 7: "I-MISC", 8: "I-LOC"}

    # spaCy labels for PER are "PERSON"; LOC is "GPE" or "LOC"; map back
    # to CoNLL convention so gold_filter and full-protocol scoring work.
    SPACY_TO_CONLL = {
        "PERSON": "PER",
        "GPE": "LOC", "LOC": "LOC",
        "ORG": "ORG",
        "NORP": "MISC", "PRODUCT": "MISC", "EVENT": "MISC", "WORK_OF_ART": "MISC",
        "FAC": "LOC", "LAW": "MISC", "LANGUAGE": "MISC",
    }

    rows_data = []
    latencies = []
    seen_with_gold = 0
    t0 = time.perf_counter()
    for row in ds:
        text, gold = tokens_to_text_and_gold(row["tokens"], row[tag_col], id2tag)
        if len(gold) < args.min_gold:
            continue
        t1 = time.perf_counter()
        doc = nlp(text)
        latencies.append((time.perf_counter() - t1) * 1000)
        pred = [(ent.start_char, ent.end_char, SPACY_TO_CONLL.get(ent.label_, ent.label_))
                for ent in doc.ents]
        rows_data.append({"gold": gold, "pred": pred})
        seen_with_gold += 1
        if seen_with_gold >= args.limit:
            break

    summary = {
        "model": args.model,
        "dataset": args.dataset,
        "split": args.split,
        "limit": args.limit,
        "iou": args.iou,
        "elapsed_sec": round(time.perf_counter() - t0, 1),
        "avg_latency_ms": round(sum(latencies) / max(len(latencies), 1), 2),
        "full_protocol": bootstrap_ci(score(rows_data, args.iou, gold_filter=None)),
        "pii_relevant_subset": {
            **bootstrap_ci(score(rows_data, args.iou, gold_filter=PII_RELEVANT)),
            "categories": sorted(PII_RELEVANT),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
