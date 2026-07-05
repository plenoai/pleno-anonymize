"""Artifact JSON schema + log.jsonl extension (U5).

This module locks the on-disk schema for two outputs:

1. ``ComparisonArtifact`` — ``experiments/artifacts/<run_id>/comparison.json``
   produced by U4's ``compare_baselines.f2_verdict_compute``. Pre-registered
   shape is in the plan's High-Level Technical Design section (JSON sketch).
   The model enforces P1-2 (partial-run hard gate: ``aggregates`` and
   ``verdict_per_entity`` MUST be omitted whenever ``partial_run`` is True),
   P1-4 (per-entity verdict 8 required fields), P0-3 (leakage_check shape),
   and P0-4 (anchor_pr_sha pin).

2. ``LogJsonlEntry`` — ``experiments/log.jsonl`` historical record. Loosely
   validates every row regardless of which of the pre-#293 shapes (or the
   post-#293 ``experiments/log_schema.json`` shape) it uses.

P1-1 verdict Literal source of truth
-------------------------------------
``verdict`` stays a closed Literal — ``{"KEEP", "DISCARD"}`` (legacy) plus
``{"KILL", "COMMIT", "NO_DECISION"}`` (baseline_comparison) plus
``"KEEP_PARTIAL"`` (see iter12) — because every schema this project has used
for log.jsonl, including #293's ``log_schema.json``, treats the decision
outcome as a genuinely closed set.

``intervention_type`` was originally a matching closed Literal, enumerated
from a 21-entry snapshot. It is now a plain ``str`` (see the field's
docstring below): the Literal drifted out of sync with reality at least
twice as new free-text categories were appended (``label_mapping``,
``"training_data (ceiling experiment, NOT shippable)"``), undetected because
``packages/training/tests/`` is not part of CI (only ``server/`` and
``packages/sdk/`` are — see ``.github/workflows/ci.yml``). #293's
``experiments/log_schema.json`` makes the same call deliberately from the
start: ``intervention_type`` is intentionally free-form there too, since new
intervention categories are expected as the project evolves.

Universal-only required fields (``id``, ``timestamp``, ``language``) keep
every historical entry parseable; everything else is Optional, with a
Literal constraint only where the underlying concept is genuinely closed
(``verdict``, the legacy ``type`` field).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# ComparisonArtifact (experiments/artifacts/<run_id>/comparison.json)
# ---------------------------------------------------------------------------


class VerdictPerEntity(BaseModel):
    """Per-entity verdict cell. P1-4: 8 required fields, no extras allowed.

    P1-2: this object is OMITTED ENTIRELY from artifact when partial_run=True.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["KILL", "COMMIT", "NO_DECISION"]
    r7_primary_gate: bool
    r8a_min_span_filter: bool
    r8b_p10_robust: bool
    r8c_dual_metric_agree: bool
    r7_diff_sign: Literal["oss_better", "custom_better", "tied"]
    r7_diff_ci_lo: float
    r7_diff_ci_hi: float
    n_eligible_templates: int


class LeakageCheck(BaseModel):
    """P0-3: leakage check report shape. Reproduced into artifact.metadata."""

    model_config = ConfigDict(extra="forbid")

    algorithm: Literal["SHA256-NFC"]
    manifest_hash: str
    doc_overlap_count: int
    template_overlap_count: int
    passed: bool


class ArtifactMetadata(BaseModel):
    """Pre-registration metadata pinned at measurement time.

    Fields default to empty/zero so transitional callers (U4, before all upstream
    units land) can populate incrementally without schema drift. Production
    runs MUST populate every field; this is enforced by F0a-F0d acceptance
    tests, not by pydantic.
    """

    model_config = ConfigDict(extra="forbid")

    corpus_hash: str = ""
    noise_floor_hash: str = ""
    recognizers_pack_git_sha: str = ""
    recognizers_pack_content_sha256: str = ""
    # Per-variant {version, wheel_sha256, score_availability}.
    variant_versions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    bootstrap_seed: int = 0
    tie_break_rule: str = ""
    k_values: list[int] = Field(default_factory=list)
    leakage_check: LeakageCheck
    anchor_pr_sha: str = ""  # P0-4: PR-merge SHA pin.


class ComparisonArtifact(BaseModel):
    """Top-level artifact written to comparison.json.

    P1-2 hard gate: ``aggregates`` and ``verdict_per_entity`` MUST be ``None``
    when ``partial_run=True``. This is enforced by ``_partial_run_gate``
    (model_validator) raising ValidationError, and again at serialization time
    by ``write_artifact`` (which also checks the JSON output omits the keys).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    run_id: str
    metadata: ArtifactMetadata
    measurements: list[dict[str, Any]]  # per-row dicts; loose typing OK here.
    partial_run: bool
    failed_variants: list[str] = Field(default_factory=list)
    aggregates: Optional[dict[str, dict[str, Any]]] = None
    verdict_per_entity: Optional[dict[str, VerdictPerEntity]] = None

    @model_validator(mode="after")
    def _partial_run_gate(self) -> "ComparisonArtifact":
        """Hard gate (P1-2): partial_run=True ⇒ no aggregates, no verdicts."""
        if self.partial_run:
            if self.aggregates is not None:
                raise ValueError(
                    "partial_run=True implies aggregates must be None (P1-2)"
                )
            if self.verdict_per_entity is not None:
                raise ValueError(
                    "partial_run=True implies verdict_per_entity must be None (P1-2)"
                )
        else:
            if self.aggregates is None or self.verdict_per_entity is None:
                raise ValueError(
                    "partial_run=False requires aggregates and verdict_per_entity (P1-2)"
                )
        return self


# ---------------------------------------------------------------------------
# LogJsonlEntry (experiments/log.jsonl)
# ---------------------------------------------------------------------------


# Legacy values enumerated from the 21 pre-existing entries (P1-1).
# (`_LEGACY_INTERVENTIONS` / `_NEW_INTERVENTIONS` were removed alongside
# widening `intervention_type` to `str` below — #293.)
_LEGACY_VERDICTS = ("KEEP", "DISCARD")
_NEW_VERDICTS = ("KILL", "COMMIT", "NO_DECISION")
_LEGACY_TYPES = (
    "benchmark_evolution",
    "benchmark_expansion",
    "benchmark_refinement",
)


class LogJsonlEntry(BaseModel):
    """Backward-compatible log.jsonl row (P1-1).

    ``extra="allow"`` keeps all the historical free-form columns valid
    (``model_score_*``, ``negative_docs_*``, ``rationale`` etc.). Only the
    universal columns (``id``, ``timestamp``, ``language``) are required;
    everything else is Optional but, when present, MUST satisfy its Literal.
    """

    model_config = ConfigDict(extra="allow")

    # Universal columns (present in all 21 historical entries).
    id: str
    timestamp: str
    language: str

    # Hypothesis-test columns (13/21 entries).
    hypothesis: Optional[str] = None
    # `intervention_type` was originally a closed Literal enumerated from a
    # 21-entry snapshot (P1-1). It silently drifted out of sync at least
    # twice since (iter13's "label_mapping", iter14's free-text "training_data
    # (ceiling experiment, NOT shippable)") without failing CI, because
    # packages/training/tests/ is not wired into .github/workflows/ci.yml —
    # only server/ and packages/sdk/ run there. #293's log_schema.json
    # (experiments/log_schema.json) makes the same call deliberately: new
    # intervention categories are expected as the project evolves, so the
    # field is a plain string there, not an enum. Widened here to match —
    # `verdict` stays a Literal because both schemas treat it as a genuinely
    # closed decision set.
    intervention_type: Optional[str] = None
    changes: Optional[Any] = None
    metrics_before: Optional[Any] = None
    metrics_after: Optional[Any] = None
    delta: Optional[Any] = None
    verdict: Optional[
        Literal[
            "KEEP",
            "DISCARD",
            # KEEP_PARTIAL: hypothesis improved most entities but missed the
            # primary AC (e.g. iter12_aug_ext: 4/5 entities up, ORG precision
            # still floored). Keep the artifact for follow-up; do not tag.
            "KEEP_PARTIAL",
            "KILL",
            "COMMIT",
            "NO_DECISION",
        ]
    ] = None
    reason: Optional[str] = None
    duration_minutes: Optional[float] = None

    # Benchmark-evolution columns (8/21 entries) — explicit slot rather
    # than relying solely on extra="allow".
    type: Optional[
        Literal[
            "benchmark_evolution",
            "benchmark_expansion",
            "benchmark_refinement",
        ]
    ] = None

    # New U5 column for baseline_comparison entries.
    artifact_path: Optional[str] = None


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def write_artifact(comparison: ComparisonArtifact, path: Path) -> None:
    """Write a ComparisonArtifact to ``path`` as pretty-printed JSON.

    Stable key order via ``sort_keys=True``. P1-2 gate is doubly enforced:
    ``model_dump(exclude_none=True)`` strips ``aggregates`` and
    ``verdict_per_entity`` whenever they are None (the partial-run case),
    and an assertion verifies the keys are absent in the serialized output.
    """
    data = comparison.model_dump(exclude_none=True)
    if comparison.partial_run:
        assert "aggregates" not in data, "P1-2 violation: aggregates leaked"
        assert "verdict_per_entity" not in data, "P1-2 violation: verdict leaked"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def append_log_entry(
    entry: LogJsonlEntry,
    path: Path = Path("packages/training/experiments/log.jsonl"),
) -> None:
    """Append one entry to log.jsonl. Existing entries are never rewritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = entry.model_dump(exclude_none=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def parse_log_jsonl(path: Path) -> list[LogJsonlEntry]:
    """Parse all entries; raise ``pydantic.ValidationError`` on first malformed line."""
    entries: list[LogJsonlEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries.append(LogJsonlEntry.model_validate_json(line))
    return entries
