"""Dump a language slice of ai4privacy/pii-masking-300k to local JSONL.

WARNING: the dumped data is for evaluation only; do not train on it
without written permission from AI4Privacy (non-commercial license).

Mirror of how the JP supervised v2 pipeline reads its data
(`train_supervised_300k_ja.py`). The trainer reads from local
``train.jsonl`` / ``dev.jsonl`` so the dataset load happens once on a
host that has HF auth + bandwidth, and the pod only needs a tarball.

Each output line is::

    {"text": "...", "entities": [{"start": s, "end": e, "label": L}, ...]}

Usage::

    uv run --extra hf python scripts/dump_pii_300k.py \\
        --dataset ai4privacy/pii-masking-300k \\
        --language English \\
        --output-dir data/raw/en-300k-supervised
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _parse_spans(raw) -> list[tuple[int, int, str]]:
    """Same shape-tolerant parser used by eval_on_300k.py."""
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    out: list[tuple[int, int, str]] = []
    for s in raw:
        if isinstance(s, list) and len(s) >= 3:
            out.append((int(s[0]), int(s[1]), str(s[2])))
        elif isinstance(s, dict) and {"start", "end"} <= s.keys():
            out.append((int(s["start"]), int(s["end"]), str(s.get("label", "?"))))
    return out


def _row_to_record(row: dict) -> dict | None:
    text = row.get("source_text") or row.get("text")
    if not text:
        return None
    raw = row.get("privacy_mask") or row.get("span_labels") or row.get("entities")
    spans = _parse_spans(raw)
    return {
        "text": text,
        "entities": [{"start": s, "end": e, "label": l} for s, e, l in spans],
    }


def _dump_split(ds, language: str, limit: int | None, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for row in ds:
            if language and row.get("language") != language:
                continue
            rec = _row_to_record(row)
            if rec is None:
                continue
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
            if limit and written >= limit:
                break
    return written


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", default="ai4privacy/pii-masking-300k")
    p.add_argument("--language", default="English", help='e.g. "English", "Japanese", "French"')
    p.add_argument("--train-split", default="train")
    p.add_argument("--dev-split", default="validation")
    p.add_argument("--train-limit", type=int, default=0, help="0 = no limit")
    p.add_argument("--dev-limit", type=int, default=0, help="0 = no limit")
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()

    print(
        "[warning] dumped data is for evaluation only; do not train on it without "
        "written permission from AI4Privacy (non-commercial license)."
    )

    from datasets import load_dataset

    print(f"[load] {args.dataset} language={args.language}")
    train_ds = load_dataset(args.dataset, split=args.train_split, streaming=True)
    dev_ds = load_dataset(args.dataset, split=args.dev_split, streaming=True)

    train_out = args.output_dir / "train.jsonl"
    dev_out = args.output_dir / "dev.jsonl"

    n_train = _dump_split(train_ds, args.language, args.train_limit or None, train_out)
    print(f"[write] {train_out} rows={n_train}")
    n_dev = _dump_split(dev_ds, args.language, args.dev_limit or None, dev_out)
    print(f"[write] {dev_out} rows={n_dev}")


if __name__ == "__main__":
    main()
