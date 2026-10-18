"""Guard against the setup-uv cache-save race that warned on the `test` job.

Issue: the CI/CD `test` job logged

    Failed to save: Unable to reserve cache with key setup-uv-...,
    another job may be creating this cache.

Root cause: `check`, `test`, `sdk`, `extras` and `training` run in parallel
and every `astral-sh/setup-uv` step derives the *same* cache key (same
OS/arch/Python and the same hash over `uv.lock`). GitHub only lets one job
reserve a key, so the losers emit the warning above. Giving each step a
distinct ``cache-suffix`` makes the save keys unique while setup-uv's
restore-keys still let every job warm from the others' caches.

This test parses the workflow YAML by hand so it needs no PyYAML (the
training CI job only installs pytest + the dev group). It fails if a
workflow ever runs more than one setup-uv step without unique suffixes,
which is exactly the regression that produced the warning.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github/workflows"

_USES_RE = re.compile(r"uses:\s*astral-sh/setup-uv@")
_CACHE_SUFFIX_RE = re.compile(r"cache-suffix:\s*(.+?)\s*$")


def _setup_uv_cache_suffixes(text: str) -> list[str | None]:
    """Return the cache-suffix (or None) of every setup-uv step in a workflow."""
    lines = text.splitlines()
    suffixes: list[str | None] = []
    for index, line in enumerate(lines):
        if not _USES_RE.search(line):
            continue
        step_indent = len(line) - len(line.lstrip())
        suffix: str | None = None
        for following in lines[index + 1 :]:
            stripped = following.strip()
            if not stripped:
                continue
            indent = len(following) - len(following.lstrip())
            # A line at or left of the step's dash starts the next step/block.
            if indent <= step_indent:
                break
            match = _CACHE_SUFFIX_RE.search(following)
            if match:
                suffix = match.group(1)
        suffixes.append(suffix)
    return suffixes


def test_no_parallel_setup_uv_cache_save_race():
    workflows_with_multiple = {}
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        suffixes = _setup_uv_cache_suffixes(workflow.read_text(encoding="utf-8"))
        if len(suffixes) <= 1:
            # A single setup-uv step cannot race itself.
            continue
        workflows_with_multiple[workflow.name] = suffixes

    assert workflows_with_multiple, (
        "expected at least one workflow with multiple setup-uv steps"
    )

    for name, suffixes in workflows_with_multiple.items():
        missing = [i for i, suffix in enumerate(suffixes) if not suffix]
        assert not missing, (
            f"{name}: setup-uv steps at positions {missing} lack a cache-suffix; "
            "parallel jobs will race to save the same cache key"
        )
        assert len(suffixes) == len(set(suffixes)), (
            f"{name}: duplicate setup-uv cache-suffix values {suffixes}; "
            "each parallel job needs a unique key to avoid the "
            "'Unable to reserve cache' warning"
        )


def test_ci_workflow_suffixes_are_distinct():
    suffixes = _setup_uv_cache_suffixes(
        (WORKFLOW_DIR / "ci.yml").read_text(encoding="utf-8")
    )
    assert len(suffixes) == 5, f"expected 5 setup-uv steps in ci.yml, got {suffixes}"
    assert all(suffixes), f"ci.yml setup-uv step missing cache-suffix: {suffixes}"
    assert len(suffixes) == len(set(suffixes)), (
        f"duplicate suffixes in ci.yml: {suffixes}"
    )
