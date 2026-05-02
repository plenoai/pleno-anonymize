"""Tests for pleno_ner_training.baselines_ja (U3).

Strategy: avoid loading any heavy NLP models in the default test path. We
exercise:

- Static metadata of `BASELINE_REGISTRY` (5 entries, expected flags).
- `_results_to_predictions` pure helper with hand-built duck-typed result
  objects — covers ORG/DOB filtering, DATE_TIME→DATE_OF_BIRTH mapping, rank
  assignment, tie-break determinism, empty input.
- Custom builders' clear-error path when model artifacts are absent.

Heavy real-load tests (e.g. spacy.load("ja_core_news_md")) are gated behind
`@pytest.mark.slow` so default CI runs without `[bench]` extras installed.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pleno_ner_training.baselines_ja import (
    BASELINE_REGISTRY,
    CUSTOM_LABEL_MAP,
    PRESIDIO_LABEL_MAP,
    TARGET_LABELS,
    BaselineSpec,
    _results_to_predictions,
)


# --- duck-typed RecognizerResult stand-in for pure-helper tests --------------


@dataclass
class _FakeResult:
    start: int
    end: int
    entity_type: str
    score: float | None = None


# --- Static registry metadata ------------------------------------------------


def test_registry_has_exactly_five_entries():
    assert len(BASELINE_REGISTRY) == 5


def test_registry_keys_match_plan():
    assert set(BASELINE_REGISTRY.keys()) == {
        "ja_core_news_trf",
        "ja_ginza",
        "ja_core_news_md",
        "custom_cnn",
        "custom_bert",
    }


@pytest.mark.parametrize(
    "name,category,score_bearing",
    [
        ("ja_core_news_trf", "oss_presidio", True),
        ("ja_ginza", "oss_presidio", True),
        ("ja_core_news_md", "oss_presidio", False),
        ("custom_cnn", "custom", False),
        ("custom_bert", "custom", False),
    ],
)
def test_registry_metadata(name: str, category: str, score_bearing: bool):
    spec = BASELINE_REGISTRY[name]
    assert isinstance(spec, BaselineSpec)
    assert spec.name == name
    assert spec.category == category
    assert spec.score_bearing is score_bearing
    assert callable(spec.builder)


def test_registry_nonexistent_baseline_raises_keyerror():
    with pytest.raises(KeyError):
        BASELINE_REGISTRY["nonexistent"]


def test_target_labels_locked_to_org_and_dob():
    assert TARGET_LABELS == frozenset({"ORGANIZATION", "DATE_OF_BIRTH"})


def test_label_maps_project_to_target_set():
    # Every value in label maps must land in TARGET_LABELS (so filtering after
    # mapping produces ORG/DOB only).
    assert set(PRESIDIO_LABEL_MAP.values()) <= TARGET_LABELS
    assert set(CUSTOM_LABEL_MAP.values()) <= TARGET_LABELS


# --- _results_to_predictions: filtering, mapping, rank -----------------------


def test_results_to_predictions_empty_input_returns_empty_list():
    assert _results_to_predictions([], PRESIDIO_LABEL_MAP) == []


def test_results_to_predictions_filters_non_target_entities():
    results = [
        _FakeResult(0, 3, "PERSON", 0.9),  # not in target → drop
        _FakeResult(4, 8, "LOCATION", 0.8),  # not in target → drop
        _FakeResult(9, 13, "ORGANIZATION", 0.7),  # keep
    ]
    out = _results_to_predictions(results, PRESIDIO_LABEL_MAP)
    assert len(out) == 1
    assert out[0][:3] == (9, 13, "ORGANIZATION")


def test_results_to_predictions_maps_date_time_to_date_of_birth():
    results = [_FakeResult(0, 10, "DATE_TIME", 0.5)]
    out = _results_to_predictions(results, PRESIDIO_LABEL_MAP)
    assert out == [(0, 10, "DATE_OF_BIRTH", 0.5, 0)]


def test_results_to_predictions_assigns_rank_in_document_order():
    # Provide reversed input; expect rank to follow (start,end,label) order.
    results = [
        _FakeResult(20, 25, "ORGANIZATION", 0.4),
        _FakeResult(0, 5, "ORGANIZATION", 0.9),
        _FakeResult(10, 15, "DATE_TIME", 0.6),
    ]
    out = _results_to_predictions(results, PRESIDIO_LABEL_MAP)
    assert [p[0] for p in out] == [0, 10, 20]
    assert [p[4] for p in out] == [0, 1, 2]
    assert [p[2] for p in out] == ["ORGANIZATION", "DATE_OF_BIRTH", "ORGANIZATION"]


def test_results_to_predictions_score_none_propagates():
    results = [_FakeResult(0, 5, "ORGANIZATION", None)]
    out = _results_to_predictions(results, CUSTOM_LABEL_MAP)
    assert out == [(0, 5, "ORGANIZATION", None, 0)]


def test_results_to_predictions_deterministic_across_calls():
    # Same input twice → identical output (tie-break determinism).
    results = [
        _FakeResult(0, 5, "ORGANIZATION", 0.5),
        _FakeResult(10, 15, "DATE_TIME", 0.5),
        _FakeResult(0, 5, "ORGANIZATION", 0.5),  # duplicate (different obj)
    ]
    a = _results_to_predictions(list(results), PRESIDIO_LABEL_MAP)
    b = _results_to_predictions(list(results), PRESIDIO_LABEL_MAP)
    assert a == b


def test_results_to_predictions_custom_label_map_handles_short_aliases():
    # CUSTOM_LABEL_MAP accepts both ORGANIZATION/ORG and DATE_OF_BIRTH/DATE.
    results = [
        _FakeResult(0, 3, "ORG", None),
        _FakeResult(4, 14, "DATE", None),
    ]
    out = _results_to_predictions(results, CUSTOM_LABEL_MAP)
    assert {p[2] for p in out} == {"ORGANIZATION", "DATE_OF_BIRTH"}


# --- Custom builders' clear-error path (no artifacts present) ----------------


def test_custom_cnn_builder_raises_clear_error_when_no_artifacts(monkeypatch, tmp_path):
    # Redirect _TRAINING_OUTPUT to an empty tmp dir to guarantee no match.
    import pleno_ner_training.baselines_ja as mod

    monkeypatch.setattr(mod, "_TRAINING_OUTPUT", tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        mod._build_custom_cnn()
    assert "custom_cnn requires a trained CNN model" in str(excinfo.value)


def test_custom_bert_builder_raises_clear_error_when_no_artifacts(monkeypatch, tmp_path):
    import pleno_ner_training.baselines_ja as mod

    monkeypatch.setattr(mod, "_TRAINING_OUTPUT", tmp_path)
    with pytest.raises(FileNotFoundError) as excinfo:
        mod._build_custom_bert()
    msg = str(excinfo.value)
    assert "custom_bert" in msg
    assert "ja-v02-trf" in msg


# --- Lazy builder behaviour --------------------------------------------------


def test_builders_are_not_invoked_at_import_time():
    # Static metadata access must not have triggered model loading. We check
    # by ensuring spacy is not yet imported as a side-effect of importing the
    # module — but spacy may already be imported via other tests, so instead
    # we simply assert builders are still callables and BaselineSpec carries
    # them as references (not pre-built Predictor instances).
    for spec in BASELINE_REGISTRY.values():
        assert callable(spec.builder)
        # A pre-built Predictor would expose .predict directly — spec.builder
        # must remain a zero-arg callable.
        assert not hasattr(spec.builder, "predict")


# --- Slow real-load tests (gated behind -m slow) -----------------------------


@pytest.mark.slow
def test_real_load_ja_core_news_md_smoke():
    """Load the smallest OSS+Presidio variant end-to-end. Requires `[bench]`
    extras + `python -m spacy download ja_core_news_md`. Skipped by default."""
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("spacy")
    spec = BASELINE_REGISTRY["ja_core_news_md"]
    predictor = spec.builder()
    out = predictor.predict("山田太郎は2025年1月生まれ")
    # Cannot assert exact contents (model-dependent), only shape.
    assert isinstance(out, list)
    for pred in out:
        assert len(pred) == 5
        start, end, label, score, rank = pred
        assert isinstance(start, int)
        assert isinstance(end, int)
        assert label in TARGET_LABELS
        assert score is None or isinstance(score, float)
        assert isinstance(rank, int)


@pytest.mark.slow
def test_real_load_empty_input_returns_empty_list():
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("spacy")
    spec = BASELINE_REGISTRY["ja_core_news_md"]
    predictor = spec.builder()
    assert predictor.predict("") == []
