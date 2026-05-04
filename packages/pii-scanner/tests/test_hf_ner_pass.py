"""Unit tests for the opt-in HF NER pass (#48/#98).

Pure-Python coverage only — torch / transformers loading is excluded so the
default (spaCy) install can run the test suite without the [hf] extra.
"""

from __future__ import annotations

import pytest

from pleno_pii_scanner.hf_ner_pass import (
    _DEFAULT_LABEL_THRESHOLDS,
    _decode_spans,
    _parse_thresholds,
    _resolve_model_source,
)


def _id2label() -> dict[int, str]:
    labels = ["O", "B-ORGANIZATION", "I-ORGANIZATION", "B-PERSON", "I-PERSON"]
    return {i: l for i, l in enumerate(labels)}


def test_parse_thresholds_defaults_match_v013_model_card():
    out = _parse_thresholds(None)
    assert out == _DEFAULT_LABEL_THRESHOLDS
    assert out["ORGANIZATION"] == 0.99


def test_parse_thresholds_overrides_via_env_value():
    out = _parse_thresholds("ORGANIZATION=0.85, PERSON=0.5")
    assert out == {"ORGANIZATION": 0.85, "PERSON": 0.5}


def test_parse_thresholds_rejects_malformed_pair():
    with pytest.raises(ValueError):
        _parse_thresholds("ORGANIZATION:0.85")


def test_decode_spans_emits_min_token_score():
    offsets = [(0, 0), (0, 3), (3, 6), (0, 0)]
    label_ids = [0, 1, 2, 0]
    scores = [0.99, 0.91, 0.62, 0.99]
    spans = _decode_spans(offsets, label_ids, scores, _id2label())
    assert spans == [(0, 6, "ORGANIZATION", 0.62)]


def test_decode_spans_separates_adjacent_distinct_labels():
    offsets = [(0, 3), (3, 6)]
    label_ids = [1, 3]  # B-ORG, B-PERSON
    scores = [0.9, 0.8]
    spans = _decode_spans(offsets, label_ids, scores, _id2label())
    assert [s[2] for s in spans] == ["ORGANIZATION", "PERSON"]
    assert [s[3] for s in spans] == [0.9, 0.8]


def test_resolve_model_source_defaults_to_hub_v013(monkeypatch):
    monkeypatch.delenv("PLENO_PII_SCANNER_HF_MODEL", raising=False)
    monkeypatch.delenv("PLENO_PII_SCANNER_HF_REVISION", raising=False)
    src, rev = _resolve_model_source()
    assert src == "0xhikae/ja-ner-onnx"
    assert rev == "v0.13.0"


def test_resolve_model_source_honors_env_overrides(monkeypatch):
    monkeypatch.setenv("PLENO_PII_SCANNER_HF_MODEL", "myorg/custom-ner")
    monkeypatch.setenv("PLENO_PII_SCANNER_HF_REVISION", "v9.9.9")
    src, rev = _resolve_model_source()
    assert src == "myorg/custom-ner"
    assert rev == "v9.9.9"
