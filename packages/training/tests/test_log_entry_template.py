"""U7 verification: log_entry_template.json round-trips through LogJsonlEntry.

Guards the post-decision artifact templates created by U7 of plan
``docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md``.
The template MUST stay valid against U5's ``LogJsonlEntry`` pydantic model so
maintainers can append it verbatim (with TBD placeholders filled) to
``experiments/log.jsonl`` once measurement results land.
"""

from __future__ import annotations

import json
from pathlib import Path

from pleno_ner_training.artifact import LogJsonlEntry


def test_log_entry_templates_validate() -> None:
    template_path = (
        Path(__file__).resolve().parents[1]
        / "experiments"
        / "log_entry_template.json"
    )
    data = json.loads(template_path.read_text(encoding="utf-8"))
    assert len(data) == 2  # KILL + COMMIT
    verdicts_seen: set[str] = set()
    for entry in data:
        parsed = LogJsonlEntry.model_validate(entry)
        assert parsed.intervention_type == "baseline_comparison"
        assert parsed.verdict in {"KILL", "COMMIT"}
        assert parsed.artifact_path is not None
        assert parsed.language == "ja"
        verdicts_seen.add(parsed.verdict)
    assert verdicts_seen == {"KILL", "COMMIT"}
