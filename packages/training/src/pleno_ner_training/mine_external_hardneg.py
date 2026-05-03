"""External-corpus hard-negative mining for ORG precision (#64).

Why
---
`convert_to_hf_dataset.py --include-negatives` only sources zero-entity docs
from `data/raw/ja-v02/generated.json`. Those LLM-refusal templates barely
contain ORG-like surface forms, so the hardneg signal collapsed onto PERSON
and adversarial v0.12.0 ORG precision stayed at 0.085 (#48 follow-up).

This module mines additional **PII-free** docs from a public Japanese
corpus (default: `wikimedia/wikipedia` config `20231101.ja`, CC-BY-SA 4.0)
and writes them as `entities=[]` examples — the same schema accepted by
`load_and_convert(..., include_negatives=True)`. After concatenation with
`generated.json` the hardneg trainer sees a much higher density of
ORG-likely-FP surface (Wikipedia is dense in facility / event / product /
law names that look like ORGs but are non-PII descriptive prose).

License
-------
Wikipedia ja content is CC-BY-SA 4.0; using it as model training input is
covered by the existing pleno-anonymize attribution. livedoor-news (CC-BY-ND)
is intentionally NOT used because the ND clause restricts derivative use.

Output schema
-------------
A JSON array on disk, identical to `generated.json` shape so it can be
fed directly to `convert_to_hf_dataset.py`:

    [
      {"text": "...", "entities": []},
      ...
    ]

Defensive filtering
-------------------
We re-use `convert_to_hf_dataset.PII_HINT_RE` to drop sentences that look
PII-bearing. A doc that survives the regex but still contains PII would
become a label-miss negative — the exact failure mode #62 tried to fix.
The regex is intentionally over-broad on the safe side (drops ~10–20% of
Wikipedia paragraphs that mention 都/区/会社).

Dry-run
-------
`--dry-run` skips network and writes 5 hand-crafted ORG-FP-likely sentences
so CI / smoke tests can exercise the full pipeline without HF download.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Iterable, Iterator

from pleno_ner_training.convert_to_hf_dataset import PII_HINT_RE

# Tight character window: too short = no ORG-like context, too long =
# truncation waste in the 512-token tokenizer used downstream.
MIN_CHARS = 60
MAX_CHARS = 400

# Split paragraphs into sentence-ish chunks. Wikipedia ja paragraphs often
# run >1000 chars; keeping each chunk small means more independent hardneg
# examples per doc and avoids the 512-token truncation eating the tail.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？])\s*")

# Hand-crafted ORG-FP-likely sentences for `--dry-run`. Mirrors the kind of
# Wikipedia-style descriptive prose we expect to mine in the real run.
_DRY_RUN_SAMPLES: tuple[str, ...] = (
    "東京スカイツリーは墨田区押上に位置する自立式電波塔である。"
    "高さは634メートルで、世界第二位の高さを誇る。",
    "国際連合教育科学文化機関は、教育や科学、文化の発展と推進を目的として設立された。"
    "本部はパリに置かれている。",
    "京都大学基礎物理学研究所は素粒子論や統計力学などの理論物理学研究の中核拠点の一つである。",
    "日本標準時は明石市を通る東経135度の子午線を基準としている。"
    "NHKラジオの時報もこの基準時刻に従う。",
    "万国博覧会は19世紀以降、産業文化の交流を促進する場として各国で開催されてきた。",
)


def _split_sentences(text: str) -> Iterator[str]:
    """Split a paragraph into sentence-ish chunks on Japanese terminators."""
    for chunk in _SENTENCE_SPLIT_RE.split(text):
        chunk = chunk.strip()
        if chunk:
            yield chunk


def _accumulate_chunks(sentences: Iterable[str]) -> Iterator[str]:
    """Greedy-pack sentences into MIN_CHARS..MAX_CHARS sized doc chunks.

    Smaller than MIN_CHARS chunks are concatenated with the next; once a
    chunk exceeds MAX_CHARS we cut and start fresh. This keeps each emitted
    "doc" close to a coherent passage instead of single fragments.
    """
    buf: list[str] = []
    buf_len = 0
    for sent in sentences:
        if not sent:
            continue
        # Hard-skip absurdly long single sentences — they are usually
        # tables / list-prose dumped without periods.
        if len(sent) > MAX_CHARS * 2:
            continue
        buf.append(sent)
        buf_len += len(sent)
        if buf_len >= MIN_CHARS:
            joined = "".join(buf)
            if len(joined) <= MAX_CHARS:
                yield joined
            buf = []
            buf_len = 0
    # tail: only emit if it on its own clears MIN_CHARS
    if buf_len >= MIN_CHARS:
        joined = "".join(buf)
        if len(joined) <= MAX_CHARS:
            yield joined


def is_safe_negative(text: str) -> bool:
    """Reject docs that look PII-bearing.

    Re-uses the shared `PII_HINT_RE` so the policy stays in lock-step with
    `convert_to_hf_dataset` — anything that would later be regex-dropped
    there is also dropped here at mining time, which avoids silently
    shrinking the negative pool downstream.
    """
    if not text:
        return False
    if PII_HINT_RE.search(text):
        return False
    return True


def iter_wikipedia_chunks(
    dataset_name: str,
    config: str,
    *,
    streaming: bool,
    max_docs: int,
    seed: int,
) -> Iterator[str]:
    """Stream Wikipedia ja paragraphs and yield filtered hardneg chunks.

    Streaming is used by default so we never download the full ~5GB dump.
    Caller controls `max_docs` to bound work; we early-exit once enough
    surviving chunks have been emitted.
    """
    # Lazy import: keeps the unit-test surface clean of network deps.
    from datasets import load_dataset  # type: ignore

    ds = load_dataset(dataset_name, config, split="train", streaming=streaming)
    rng = random.Random(seed)
    yielded = 0
    for record in ds:
        text = record.get("text") or ""
        if not text:
            continue
        # Wikipedia ja ja_text is article-level; chunk into paragraphs first.
        for paragraph in text.split("\n"):
            for chunk in _accumulate_chunks(_split_sentences(paragraph)):
                if not is_safe_negative(chunk):
                    continue
                # Light shuffle: skip ~50% of survivors so we sample
                # across the dump rather than the first-N articles.
                if rng.random() < 0.5:
                    continue
                yield chunk
                yielded += 1
                if yielded >= max_docs:
                    return


def mine(
    *,
    dataset_name: str = "wikimedia/wikipedia",
    config: str = "20231101.ja",
    max_docs: int = 2000,
    seed: int = 42,
    dry_run: bool = False,
) -> list[dict]:
    """Mine hard-negative docs from an external corpus.

    Returns a list of `{"text": str, "entities": []}` objects in the
    pleno-anonymize raw-data schema.
    """
    if dry_run:
        survivors = [s for s in _DRY_RUN_SAMPLES if is_safe_negative(s)]
        return [{"text": s, "entities": []} for s in survivors[:max_docs]]

    docs: list[dict] = []
    for chunk in iter_wikipedia_chunks(
        dataset_name,
        config,
        streaming=True,
        max_docs=max_docs,
        seed=seed,
    ):
        docs.append({"text": chunk, "entities": []})
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Mine PII-free hard-negative docs from an external Japanese "
            "corpus (default: wikimedia/wikipedia 20231101.ja). Output JSON "
            "is schema-compatible with convert_to_hf_dataset.py."
        )
    )
    parser.add_argument(
        "--dataset",
        default="wikimedia/wikipedia",
        help="HuggingFace datasets identifier",
    )
    parser.add_argument(
        "--config",
        default="20231101.ja",
        help="Dataset config name (Wikipedia snapshot date)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output JSON path (e.g. data/raw/ja-v02/external_hardneg.json)",
    )
    parser.add_argument("--max-docs", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip network; write 5 hand-crafted ORG-FP-likely samples.",
    )
    args = parser.parse_args()

    docs = mine(
        dataset_name=args.dataset,
        config=args.config,
        max_docs=args.max_docs,
        seed=args.seed,
        dry_run=args.dry_run,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(docs, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(docs)} hardneg docs to {args.output}")


if __name__ == "__main__":
    main()
