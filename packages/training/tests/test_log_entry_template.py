"""log_entry_template.json validates against log_schema.json (#293).

The template is the canonical "shape to copy" reference for anyone writing a
log.jsonl row by hand (or reviewing what scripts/log_experiment.py produces).
It must always be schema-valid, and it must demonstrate both the common
hypothesis-test shape and the baseline_comparison shape (metrics_before={}
is a legitimate special case, not an oversight — see log_schema.json's
description of that field).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

TRAINING_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = TRAINING_ROOT / "experiments" / "log_entry_template.json"
SCHEMA_PATH = TRAINING_ROOT / "experiments" / "log_schema.json"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_log_entry_templates_validate() -> None:
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)
    data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert isinstance(data, list)
    assert len(data) == 2  # hypothesis-test example + baseline_comparison example

    for entry in data:
        errors = list(validator.iter_errors(entry))
        assert not errors, f"{entry.get('id')} failed schema validation: {errors}"


def test_log_entry_templates_cover_both_intervention_shapes() -> None:
    data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    intervention_types = {entry["intervention_type"] for entry in data}
    verdicts = {entry["verdict"] for entry in data}

    assert "baseline_comparison" in intervention_types
    # baseline_comparison example has no natural "before" state.
    baseline_comparison_entry = next(
        e for e in data if e["intervention_type"] == "baseline_comparison"
    )
    assert baseline_comparison_entry["metrics_before"] == {}
    assert "NO_DECISION" in verdicts
    assert "KEEP" in verdicts
