"""AC tests for Simula 2/8 — meta-prompts (#149)."""

from __future__ import annotations

from collections import defaultdict

from pleno_ner_training.mechanism.meta_prompts import (
    CANONICAL_LENSES,
    build_meta_prompts,
    estimate_dup_rate,
)
from pleno_ner_training.mechanism.taxonomy import build_seed_taxonomy


def test_at_least_five_prompts_per_leaf():
    tax = build_seed_taxonomy()
    prompts = build_meta_prompts(tax)
    by_scen: dict[str, int] = defaultdict(int)
    for p in prompts:
        by_scen[p.scenario_id] += 1
    leaves = {s.id for s in tax.leaves()}
    assert set(by_scen) == leaves
    for sid, n in by_scen.items():
        assert n >= 5, (sid, n)


def test_duplicate_rate_below_floor():
    tax = build_seed_taxonomy()
    prompts = build_meta_prompts(tax)
    assert estimate_dup_rate(prompts) < 0.05


def test_unique_ids():
    tax = build_seed_taxonomy()
    prompts = build_meta_prompts(tax)
    ids = [p.id for p in prompts]
    assert len(ids) == len(set(ids))


def test_instruction_mentions_expected_entities():
    tax = build_seed_taxonomy()
    prompts = build_meta_prompts(tax)
    for p in prompts:
        for label in p.expected_entities:
            assert label in p.instruction, (p.id, label)


def test_canonical_lenses_span_axes():
    perspectives = {l.perspective for l in CANONICAL_LENSES}
    lengths = {l.length_hint for l in CANONICAL_LENSES}
    cues = {l.opening_cue for l in CANONICAL_LENSES}
    twists = {l.twist for l in CANONICAL_LENSES}
    vocabs = {l.vocabulary for l in CANONICAL_LENSES}
    assert len(perspectives) >= 2
    assert len(lengths) == 3
    assert len(cues) >= 4
    assert len(twists) >= 4
    assert len(vocabs) >= 3
