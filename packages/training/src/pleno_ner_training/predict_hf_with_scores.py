"""HuggingFace token-classification predictor with per-token softmax scores.

Issue #68: We need to sweep confidence thresholds *post-hoc* without re-running
the model. To enable that we run inference once and persist:

- raw text + gold entities (mirrored from raw.json)
- per-doc list of decoded entity spans, each with the min per-token softmax
  score within the span (the "weakest link" — what an inference-time threshold
  filter would compare against)

Span extraction follows the standard BIO decoder: a `B-X` opens a span, any
following `I-X` (matching label) extends it, anything else closes it. The span
score is min(softmax_max_per_token) across the tokens that make up the span.

Why min, not mean: a threshold like "drop spans whose every token cleared 0.85"
is the natural translation of "filter low-confidence predictions"; mean would
keep a span whose final token is wildly uncertain. Min mirrors what a
production threshold-filter loop would do.

Char-offset reconstruction uses the tokenizer's `offset_mapping`. Special
tokens (offset==(0,0)) are skipped.

Outputs JSON shape:

    [
      {
        "text": "...",
        "entities": [{"start": s, "end": e, "label": L}, ...],   # gold
        "predictions": [
            {"start": s, "end": e, "label": L, "score": float, "tokens": int},
            ...
        ],
        "_meta": {...}                                            # passthrough
      },
      ...
    ]

This module is the single source of truth for "model output at every
threshold ≥ 0"; downstream sweep_threshold.py only filters & scores.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def decode_bio_spans_with_scores(
    offsets: list[tuple[int, int]],
    pred_label_ids: list[int],
    token_scores: list[float],
    id2label: dict[int, str],
) -> list[dict[str, Any]]:
    """Decode BIO token labels into character-offset spans with min-token score.

    Pure function — no model dependency. Tested directly.

    Tokens with offset (0, 0) are special tokens (CLS/SEP/PAD) and are skipped.
    Adjacent same-label `I-` tokens without a preceding `B-` of the same label
    are treated as `B-` (robust to mis-decoding); this matches seqeval's
    permissive default.
    """
    spans: list[dict[str, Any]] = []
    cur_label: str | None = None
    cur_start: int | None = None
    cur_end: int | None = None
    cur_min_score: float = 1.0
    cur_tokens: int = 0

    def _flush() -> None:
        nonlocal cur_label, cur_start, cur_end, cur_min_score, cur_tokens
        if cur_label is not None and cur_start is not None and cur_end is not None:
            spans.append(
                {
                    "start": int(cur_start),
                    "end": int(cur_end),
                    "label": cur_label,
                    "score": float(cur_min_score),
                    "tokens": int(cur_tokens),
                }
            )
        cur_label = None
        cur_start = None
        cur_end = None
        cur_min_score = 1.0
        cur_tokens = 0

    for (tok_start, tok_end), label_id, score in zip(
        offsets, pred_label_ids, token_scores
    ):
        if tok_start == 0 and tok_end == 0:
            # Special token — close any open span.
            _flush()
            continue
        label = id2label[int(label_id)]
        if label == "O":
            _flush()
            continue
        prefix, _, ent = label.partition("-")
        if not ent:
            # Malformed label — treat as O.
            _flush()
            continue
        if prefix == "B" or cur_label != ent:
            _flush()
            cur_label = ent
            cur_start = int(tok_start)
            cur_end = int(tok_end)
            cur_min_score = float(score)
            cur_tokens = 1
        else:
            # I- continuing the current entity.
            cur_end = int(tok_end)
            cur_min_score = min(cur_min_score, float(score))
            cur_tokens += 1

    _flush()
    return spans


def predict_doc(
    text: str,
    model: Any,
    tokenizer: Any,
    id2label: dict[int, str],
    max_length: int = 512,
) -> list[dict[str, Any]]:
    """Run model inference on `text` and return decoded scored spans.

    Heavy deps imported lazily inside this function so the module is importable
    without torch/transformers installed (smoke tests for the BIO decoder run
    without the [hf] extra).
    """
    import torch
    import torch.nn.functional as F  # noqa: N812

    enc = tokenizer(
        text,
        max_length=max_length,
        truncation=True,
        padding=False,
        return_offsets_mapping=True,
        return_tensors="pt",
    )
    offsets = [tuple(o) for o in enc.pop("offset_mapping")[0].tolist()]
    with torch.inference_mode():
        outputs = model(**enc)
    logits = outputs.logits[0]  # (seq_len, num_labels)
    probs = F.softmax(logits, dim=-1)
    scores, label_ids = probs.max(dim=-1)
    return decode_bio_spans_with_scores(
        offsets=offsets,
        pred_label_ids=label_ids.tolist(),
        token_scores=scores.tolist(),
        id2label=id2label,
    )


def predict_dataset(
    raw_docs: Iterable[dict[str, Any]],
    model_path: Path,
    max_length: int = 512,
) -> list[dict[str, Any]]:
    """Predict on every doc in `raw_docs` (raw.json schema)."""
    from transformers import AutoModelForTokenClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(model_path))
    model = AutoModelForTokenClassification.from_pretrained(str(model_path))
    model.eval()
    id2label = {int(k): v for k, v in model.config.id2label.items()}

    out: list[dict[str, Any]] = []
    for doc in raw_docs:
        text = doc.get("text", "")
        preds = predict_doc(text, model, tokenizer, id2label, max_length=max_length)
        out.append(
            {
                "text": text,
                "entities": doc.get("entities", []),
                "predictions": preds,
                "_meta": doc.get("_meta", {}),
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run HF NER and persist per-token softmax scores"
    )
    parser.add_argument("--model", type=Path, required=True, help="HF model dir")
    parser.add_argument("--input", type=Path, required=True, help="raw.json")
    parser.add_argument(
        "--output", type=Path, required=True, help="raw_with_scores.json output path"
    )
    parser.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        raw_docs = json.load(f)
    if not isinstance(raw_docs, list):
        raise SystemExit(f"Expected a list at {args.input}; got {type(raw_docs)}")

    print(f"Loading model from {args.model}")
    print(f"Predicting on {len(raw_docs)} docs from {args.input}")
    out = predict_dataset(raw_docs, args.model, max_length=args.max_length)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(out)} scored predictions to {args.output}")


if __name__ == "__main__":
    main()
