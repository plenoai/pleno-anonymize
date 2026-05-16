"""Acceptance-criteria tests for the Simula-1/8 JP PII taxonomy."""

from __future__ import annotations

from pleno_ner_training.entity_types import NER_LABELS, PATTERN_LABELS
from pleno_ner_training.mechanism.taxonomy import (
    DOCUMENT_TYPES,
    ENTITY_DENSITIES,
    REGISTERS,
    build_seed_taxonomy,
    to_dict,
)


def test_seed_meets_ac_thresholds():
    tax = build_seed_taxonomy()
    s = tax.stats()
    assert s["domains"] >= 30, s
    assert s["scenarios"] >= 200, s


def test_seed_entity_label_coverage():
    tax = build_seed_taxonomy()
    seen = {e for scen in tax.leaves() for e in scen.expected_entities}
    canonical = set(NER_LABELS) | set(PATTERN_LABELS)
    missing = canonical - seen
    assert not missing, f"uncovered entity labels: {sorted(missing)}"


def test_seed_unique_scenario_ids():
    tax = build_seed_taxonomy()
    ids = [s.id for s in tax.leaves()]
    assert len(ids) == len(set(ids)), "duplicate scenario ids"


def test_seed_metadata_well_formed():
    tax = build_seed_taxonomy()
    for s in tax.leaves():
        assert s.registers, s.id
        for r in s.registers:
            assert r in REGISTERS, (s.id, r)
        assert s.document_type in DOCUMENT_TYPES, (s.id, s.document_type)
        assert s.entity_density in ENTITY_DENSITIES, (s.id, s.entity_density)
        assert s.expected_entities, s.id


def test_to_dict_is_serialisable():
    """Round-trip the dict form so save_yaml/save_json cannot silently break."""
    import json

    payload = to_dict(build_seed_taxonomy())
    json.dumps(payload, ensure_ascii=False)
    assert payload["stats"]["scenarios"] >= 200
    assert payload["domains"]


def test_variant_expansion_is_idempotent():
    """Calling build_seed_taxonomy twice must return identical stats — the
    variant-expansion pass must not double-apply when the seed is rebuilt."""
    a = build_seed_taxonomy().stats()
    b = build_seed_taxonomy().stats()
    assert a == b
