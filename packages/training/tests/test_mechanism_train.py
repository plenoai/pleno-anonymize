"""Smoke tests for Simula 6/8 — training plumbing (#153).

Heavy operations (downloading the base model, GPU training) live in
the RunPod orchestration. Tests here cover the label-alignment logic
deterministically.
"""

from __future__ import annotations

from pleno_ner_training.mechanism.train import (
    BIO_LABELS,
    LABEL2ID,
    _bio_labels_for_tokens,
)


def test_bio_labels_for_tokens_aligns_entity_to_tokens():
    text = "山田太郎が連絡"
    entities = [(0, 4, "PERSON")]
    # Simulate a tokenisation where each char is its own token + CLS/SEP.
    offsets = [(0, 0)] + [(i, i + 1) for i in range(len(text))] + [(0, 0)]
    labels = _bio_labels_for_tokens(text, entities, offsets)
    decoded = [BIO_LABELS[i] if i >= 0 else "X" for i in labels]
    assert decoded[0] == "X"  # CLS masked
    # First PERSON token is B-, rest are I-.
    person_tokens = [decoded[i] for i in range(1, 5)]
    assert person_tokens[0] == "B-PERSON"
    assert all(t == "I-PERSON" for t in person_tokens[1:])
    # Subsequent non-entity tokens are O.
    assert decoded[5] == "O"
    assert decoded[-1] == "X"  # SEP masked


def test_bio_labels_for_tokens_handles_disjoint_spans():
    text = "山田に03に連絡"
    entities = [(0, 2, "PERSON"), (3, 5, "PHONE_NUMBER")]
    offsets = [(0, 0)] + [(i, i + 1) for i in range(len(text))] + [(0, 0)]
    labels = _bio_labels_for_tokens(text, entities, offsets)
    decoded = [BIO_LABELS[i] if i >= 0 else "X" for i in labels]
    # tokens map to positions 1..len(text); two B- markers, one each.
    b_marks = [(i, l) for i, l in enumerate(decoded) if l.startswith("B-")]
    assert len(b_marks) == 2


def test_label_inventory_covers_canonical_labels():
    from pleno_ner_training.entity_types import NER_LABELS, PATTERN_LABELS

    for label in NER_LABELS + PATTERN_LABELS:
        assert f"B-{label}" in LABEL2ID, label
        assert f"I-{label}" in LABEL2ID, label
