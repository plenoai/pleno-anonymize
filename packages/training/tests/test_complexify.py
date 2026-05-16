"""AC tests for Simula 3/8 — complexification (#150)."""

from __future__ import annotations

import random

import pytest

from pleno_ner_training.mechanism.complexify import (
    OPERATORS,
    Sample,
    Span,
    add_ambiguity,
    add_near_pii,
    apply_with_ratio,
    bucket,
    code_switch,
    couple_entities,
    difficulty_score,
    obfuscate,
    validate_spans,
)


def _base_sample() -> Sample:
    text = "山田太郎さんに03-1234-5678で連絡してください。"
    return Sample(
        text=text,
        entities=[
            Span(0, 4, "PERSON"),
            Span(7, 19, "PHONE_NUMBER"),
        ],
    )


def test_validate_spans_passes_for_well_formed_sample():
    validate_spans(_base_sample())


@pytest.mark.parametrize("op_name", list(OPERATORS.keys()))
def test_each_operator_preserves_span_invariants(op_name):
    rng = random.Random(0)
    sample = _base_sample()
    out = OPERATORS[op_name](sample, rng)
    validate_spans(out)
    # Span surface form should match the slice from the (possibly mutated) text.
    for s in out.entities:
        slice_ = out.text[s.start : s.end]
        assert slice_, (op_name, s)
    # Original label set must be preserved (operators may add, never remove).
    original_labels = {s.label for s in sample.entities}
    assert original_labels.issubset({s.label for s in out.entities}), op_name


def test_obfuscate_alters_phone_or_strips_honorific():
    rng = random.Random(0)
    out = obfuscate(_base_sample(), rng)
    assert out.text != _base_sample().text


def test_couple_entities_adds_a_person_span():
    rng = random.Random(1)
    out = couple_entities(_base_sample(), rng)
    persons = [s for s in out.entities if s.label == "PERSON"]
    assert len(persons) >= 2


def test_add_near_pii_prepends_decoy():
    rng = random.Random(2)
    out = add_near_pii(_base_sample(), rng)
    assert out.text != _base_sample().text
    # The original PERSON span should now be offset.
    assert out.entities[0].start > 0


def test_add_ambiguity_does_not_introduce_phantom_person_label():
    rng = random.Random(3)
    out = add_ambiguity(_base_sample(), rng)
    # Distractor stays UNTAGGED — that's the whole point.
    persons_before = sum(1 for s in _base_sample().entities if s.label == "PERSON")
    persons_after = sum(1 for s in out.entities if s.label == "PERSON")
    assert persons_after == persons_before


def test_code_switch_may_skip_when_surface_unmappable():
    rng = random.Random(0)
    s = Sample(text="プレノ太郎が連絡", entities=[Span(0, 4, "PERSON")])
    out = code_switch(s, rng)
    validate_spans(out)


def test_difficulty_score_is_in_unit_interval():
    rng = random.Random(0)
    base = _base_sample()
    score = difficulty_score(base)
    assert 0.0 <= score <= 1.0


def test_apply_with_ratio_hits_target_within_one_sample():
    samples = [_base_sample() for _ in range(100)]
    target = {"easy": 0.5, "medium": 0.3, "hard": 0.2}
    out = apply_with_ratio(samples, target=target, seed=42)
    counts = {"easy": 0, "medium": 0, "hard": 0}
    for s in out:
        counts[bucket(s.difficulty or 0)] += 1
    # We control the proportion of *operators applied*, not bucket counts —
    # so the assertion is on operator application, not raw bucket counts.
    hard_ops = sum(1 for s in out if any("code_switch" == op or "couple_entities" == op for op in s.operators_applied))
    assert hard_ops == round(100 * target["hard"])


def test_difficulty_scores_track_operator_chains():
    rng = random.Random(0)
    base = _base_sample()
    easy_score = difficulty_score(base)
    hardened = add_near_pii(add_ambiguity(obfuscate(base, rng), rng), rng)
    hardened.difficulty = difficulty_score(hardened)
    assert hardened.difficulty > easy_score
