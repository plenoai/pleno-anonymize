"""Smoke tests for #69 / #70: HF model loader in evaluate_benchmark.

The actual HF model `output/hf-ja-v02-tiny-hardneg` is produced on RunPod and
is NOT in the repo, so we cannot run a real end-to-end eval here. Instead we:

- Test pure helpers (`_bio_tags_to_char_spans`, `_derive_hf_entry_key`).
- Verify `--hf-model` is wired into argparse.
- Verify the loader fails with a clean error when the model dir does not exist
  (so a typo gives a useful error instead of silently using spaCy).
- Verify scores.json triad metadata is injected for HF entries (#70 AC).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pleno_ner_training.evaluate_benchmark import (
    _bio_tags_to_char_spans,
    _derive_hf_entry_key,
)

TRAINING_ROOT = Path(__file__).resolve().parents[1]
SCORES_JSON = TRAINING_ROOT / "data" / "benchmark" / "v0.12.0" / "ja" / "scores.json"


# ---------------------------------------------------------------------------
# _bio_tags_to_char_spans: pure helper, no HF deps required
# ---------------------------------------------------------------------------


def test_bio_to_char_spans_simple_b_i_sequence():
    # text = "山田太郎 さん", tags B-PERSON I-PERSON O O
    tags = ["B-PERSON", "I-PERSON", "O", "O"]
    offsets = [(0, 2), (2, 4), (5, 7)]
    # mismatched length defensively: zip stops at shorter, so trim
    tags = tags[: len(offsets)]
    spans = _bio_tags_to_char_spans(tags, offsets)
    assert spans == [(0, 4, "PERSON")]


def test_bio_to_char_spans_two_adjacent_b_b():
    # B-B without I means two separate entities
    tags = ["B-PERSON", "B-PERSON"]
    offsets = [(0, 2), (2, 4)]
    spans = _bio_tags_to_char_spans(tags, offsets)
    assert spans == [(0, 2, "PERSON"), (2, 4, "PERSON")]


def test_bio_to_char_spans_skips_special_tokens():
    # Special tokens have offset (0, 0) and must NOT be merged into spans
    tags = ["O", "B-PERSON", "I-PERSON", "O"]
    offsets = [(0, 0), (0, 2), (2, 4), (0, 0)]
    spans = _bio_tags_to_char_spans(tags, offsets)
    assert spans == [(0, 4, "PERSON")]


def test_bio_to_char_spans_label_change_implicit_b():
    # "I-X" right after "I-Y" must close Y and open X (defensive parse)
    tags = ["B-PERSON", "I-ORGANIZATION"]
    offsets = [(0, 2), (2, 4)]
    spans = _bio_tags_to_char_spans(tags, offsets)
    assert spans == [(0, 2, "PERSON"), (2, 4, "ORGANIZATION")]


def test_bio_to_char_spans_empty_returns_empty():
    assert _bio_tags_to_char_spans([], []) == []


def test_bio_to_char_spans_only_o_tags():
    spans = _bio_tags_to_char_spans(["O", "O"], [(0, 1), (1, 2)])
    assert spans == []


# ---------------------------------------------------------------------------
# _derive_hf_entry_key: scores.json key naming
# ---------------------------------------------------------------------------


def test_derive_hf_entry_key_strips_language_tag():
    assert _derive_hf_entry_key("output/hf-ja-v02-tiny-hardneg") == "hf_v02_tiny_hardneg"


def test_derive_hf_entry_key_baseline():
    assert _derive_hf_entry_key("output/hf-ja-v02-tiny") == "hf_v02_tiny"


def test_derive_hf_entry_key_en_variant():
    assert _derive_hf_entry_key("/abs/path/hf-en-v01") == "hf_v01"


def test_derive_hf_entry_key_no_language_tag_passthrough():
    assert _derive_hf_entry_key("custom-model") == "custom_model"


# ---------------------------------------------------------------------------
# CLI surface: --hf-model is parseable
# ---------------------------------------------------------------------------


def test_cli_hf_model_arg_in_help():
    out = subprocess.run(
        [sys.executable, "-m", "pleno_ner_training.evaluate_benchmark", "--help"],
        cwd=TRAINING_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--hf-model" in out.stdout
    assert "--hf-entry-key" in out.stdout
    assert "--merge" in out.stdout


def test_cli_requires_at_least_one_model():
    """No --model and no --hf-model → argparse error exit 2."""
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "pleno_ner_training.evaluate_benchmark",
            "--version",
            "v0.12.0",
        ],
        cwd=TRAINING_ROOT,
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0
    assert "--model" in out.stderr or "--hf-model" in out.stderr


# ---------------------------------------------------------------------------
# scores.json shape (#70 AC: existing entry preserved + triad metadata)
# ---------------------------------------------------------------------------


def test_scores_json_preserves_pleno_ner_entry():
    """#70 AC: 既存 spaCy entry は破壊しない."""
    if not SCORES_JSON.exists():
        pytest.skip(f"{SCORES_JSON} not present")
    data = json.loads(SCORES_JSON.read_text(encoding="utf-8"))
    assert "pleno_ner" in data
    assert "ents_f" in data["pleno_ner"]


def test_scores_json_has_hf_entries_with_triad():
    """#70 AC: 最低 2 つの HF entry + triad metadata."""
    if not SCORES_JSON.exists():
        pytest.skip(f"{SCORES_JSON} not present")
    data = json.loads(SCORES_JSON.read_text(encoding="utf-8"))
    hf_keys = [k for k in data if k.startswith("hf_")]
    assert len(hf_keys) >= 2, f"need >=2 HF entries, got {hf_keys}"
    for key in hf_keys:
        entry = data[key]
        assert "corpus" in entry, f"{key} missing triad.corpus"
        assert "metric" in entry, f"{key} missing triad.metric"
        assert "aggregation" in entry, f"{key} missing triad.aggregation"
        # Required score fields
        for field in ("ents_p", "ents_r", "ents_f", "ents_per_type"):
            assert field in entry, f"{key} missing {field}"


# ---------------------------------------------------------------------------
# evaluate_hf_on_benchmark fails fast on missing model dir
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("transformers") is None,
    reason="transformers not installed (only in [hf] extra)",
)
def test_evaluate_hf_missing_model_raises(tmp_path: Path):
    """Calling with a non-existent model path should raise, not crash silently.

    We don't care about the exact exception type — just that it surfaces."""
    from pleno_ner_training.evaluate_benchmark import evaluate_hf_on_benchmark

    benchmark = tmp_path / "fake.spacy"
    benchmark.write_bytes(b"not a real docbin")
    with pytest.raises(Exception):
        evaluate_hf_on_benchmark(
            str(tmp_path / "does-not-exist"), benchmark, language="ja"
        )
