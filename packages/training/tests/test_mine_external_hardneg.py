"""Tests for external-corpus hard-negative mining (#64).

The real run streams Wikipedia ja via HuggingFace datasets — that requires
network and several GB of metadata, so all live-streaming tests are guarded
behind `--dry-run`. Here we exercise:

- `--dry-run` end-to-end: hand-crafted ORG-FP-likely samples survive the
  PII filter, schema is compatible with `convert_to_hf_dataset`.
- `is_safe_negative` rejects PII-bearing strings using the shared regex
  (the lock-step contract with `convert_to_hf_dataset.PII_HINT_RE`).
- Sentence-chunking respects MIN_CHARS / MAX_CHARS bounds.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pleno_ner_training.convert_to_hf_dataset import load_and_convert
from pleno_ner_training.mine_external_hardneg import (
    MAX_CHARS,
    MIN_CHARS,
    _accumulate_chunks,
    _split_sentences,
    is_safe_negative,
    mine,
)


# ---------- safety filter ----------


def test_pii_bearing_strings_are_rejected():
    # Phone-like, addr-like, and 株式会社 strings should all be filtered.
    assert is_safe_negative("お客様の電話は03-1234-5678です") is False
    assert is_safe_negative("住所は東京都千代田区です") is False
    assert is_safe_negative("株式会社サンプルにて勤務") is False


def test_clean_descriptive_prose_passes():
    # Descriptive Wikipedia-style ORG-FP-likely sentence with no PII surface.
    assert is_safe_negative(
        "国際連合教育科学文化機関は教育や科学の発展を目的として設立された。"
    ) is True


def test_empty_string_rejected():
    assert is_safe_negative("") is False


# ---------- chunking ----------


def test_sentence_split_handles_japanese_terminators():
    text = "これは一文目。これは二文目！これは三文目？"
    parts = list(_split_sentences(text))
    assert parts == ["これは一文目。", "これは二文目！", "これは三文目？"]


def test_accumulate_chunks_respects_bounds():
    short = "あ。" * 5  # 10 chars, below MIN_CHARS -> dropped
    assert list(_accumulate_chunks(_split_sentences(short))) == []

    paragraph = "あ" * 80 + "。" + "い" * 80 + "。"
    chunks = list(_accumulate_chunks(_split_sentences(paragraph)))
    assert chunks, "should emit at least one chunk above MIN_CHARS"
    for c in chunks:
        assert MIN_CHARS <= len(c) <= MAX_CHARS


def test_accumulate_chunks_does_not_discard_over_max():
    # Two sentences whose individual length exceeds MAX_CHARS; together
    # they would overflow. Bug: the old code dropped the buffer silently
    # instead of cutting and starting fresh, losing both sentences.
    s1 = "a" * (MAX_CHARS - 1)  # just below MAX_CHARS, above MIN_CHARS
    s2 = "b" * (MAX_CHARS - 1)

    chunks = list(_accumulate_chunks([s1, s2]))
    assert len(chunks) == 2, (
        f"both sentences must be emitted separately; got {len(chunks)} chunk(s)"
    )
    for c in chunks:
        assert MIN_CHARS <= len(c) <= MAX_CHARS, f"chunk out of bounds: len={len(c)}"


# ---------- end-to-end dry-run ----------


def test_dry_run_emits_schema_compatible_docs():
    docs = mine(dry_run=True, max_docs=10)

    assert docs, "dry-run must emit at least one hand-crafted sample"
    for doc in docs:
        assert set(doc.keys()) == {"text", "entities"}
        assert isinstance(doc["text"], str) and doc["text"]
        assert doc["entities"] == []


def test_dry_run_output_is_loadable_by_convert_to_hf_dataset(
    tmp_path: Path,
):
    """The mined JSON must be drop-in for `load_and_convert(include_negatives=True)`.

    We don't need a real tokenizer here — `load_and_convert` is exercised
    elsewhere with the real DeBERTa tokenizer. We just confirm the JSON
    file shape matches what the converter expects (list of dicts with
    text / entities), by reading it back as JSON.
    """
    out = tmp_path / "external_hardneg.json"
    docs = mine(dry_run=True, max_docs=10)
    out.write_text(json.dumps(docs, ensure_ascii=False), encoding="utf-8")

    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(loaded, list)
    assert all("text" in d and "entities" in d for d in loaded)
    assert all(d["entities"] == [] for d in loaded)


def test_cli_dry_run_writes_file(tmp_path: Path):
    """Smoke-test the CLI entrypoint via subprocess.

    Catches argparse / __main__ wiring regressions that direct-import
    tests would miss.
    """
    out = tmp_path / "external_hardneg.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pleno_ner_training.mine_external_hardneg",
            "--output",
            str(out),
            "--dry-run",
            "--max-docs",
            "3",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.exists()
    docs = json.loads(out.read_text(encoding="utf-8"))
    assert 1 <= len(docs) <= 3
    assert all(d["entities"] == [] for d in docs)
    assert "Wrote" in result.stdout


# ---------- integration with convert_to_hf_dataset ----------


def test_mined_docs_consumed_as_negatives_by_loader(tmp_path: Path):
    """Concatenated hardneg + positive JSON should be loaded as negatives.

    Uses a stub tokenizer so we don't pull DeBERTa weights in unit tests.
    Confirms `load_and_convert(include_negatives=True)` happily ingests
    the mined entities=[] docs without raising.
    """

    class _StubTokenizer:
        def __call__(self, text, **kwargs):
            # 1 token per char; offsets are (i, i+1).
            n = min(len(text), kwargs.get("max_length", 512))
            return {
                "input_ids": list(range(n)),
                "attention_mask": [1] * n,
                "offset_mapping": [(i, i + 1) for i in range(n)],
            }

    mined = mine(dry_run=True, max_docs=5)
    payload = tmp_path / "merged.json"
    payload.write_text(json.dumps(mined, ensure_ascii=False), encoding="utf-8")

    converted = load_and_convert(
        payload,
        _StubTokenizer(),  # type: ignore[arg-type]
        max_length=512,
        include_negatives=True,
    )
    assert converted, "expected mined docs to be converted as all-O negatives"
    for ex in converted:
        # all-O: every label is 0 (LABEL2ID['O']).
        assert all(label == 0 for label in ex["labels"])
