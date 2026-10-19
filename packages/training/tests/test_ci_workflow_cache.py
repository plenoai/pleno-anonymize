"""Guard against `astral-sh/setup-uv` cache-key collisions in CI.

Every job that installs with uv resolves against the same workspace lock files,
so setup-uv derives an identical cache key for each of them. The jobs run
concurrently, so they race to save that single key and GitHub emits

    Failed to save: Unable to reserve cache with key setup-uv-...
    another job may be creating this cache

(observed on the `extras` job of the CI/CD workflow). A per-job `cache-suffix`
namespaces the key so there is exactly one writer per key.

This test fails if a setup-uv step omits `cache-suffix`, or if two jobs in the
same workflow would resolve to the same effective suffix. It parses the
workflow YAML as text (no PyYAML dependency) and understands the
`${{ github.job }}` placeholder, which expands to the enclosing job id.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"

_SETUP_UV_RE = re.compile(r"^(\s*)-\s+uses:\s*astral-sh/setup-uv@\S+")
_JOB_RE = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_SUFFIX_RE = re.compile(r"^\s+cache-suffix:\s*(.+?)\s*$")


def _parse_setup_uv_steps(text: str) -> dict[str, list[str | None]]:
    """Map each job id to the `cache-suffix` of its setup-uv steps.

    `None` marks a setup-uv step with no `cache-suffix`. Assumes the flat
    `jobs:` mapping layout used by this repo (job ids at two-space indent).
    """
    lines = text.splitlines()
    in_jobs = False
    current_job: str | None = None
    steps: dict[str, list[str | None]] = {}

    for i, line in enumerate(lines):
        if line.rstrip() == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue

        job_match = _JOB_RE.match(line)
        if job_match:
            current_job = job_match.group(1)
            steps.setdefault(current_job, [])

        step_match = _SETUP_UV_RE.match(line)
        if not step_match or current_job is None:
            continue

        step_indent = len(step_match.group(1))
        suffix: str | None = None
        for nxt in lines[i + 1 :]:
            if nxt.strip() == "":
                continue
            indent = len(nxt) - len(nxt.lstrip())
            if indent <= step_indent:
                break
            suffix_match = _SUFFIX_RE.match(nxt)
            if suffix_match:
                suffix = suffix_match.group(1)
                break
        steps[current_job].append(suffix)

    return steps


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))


def test_workflow_files_present() -> None:
    assert _workflow_files(), f"no workflow files found under {WORKFLOWS_DIR}"


def test_every_setup_uv_step_has_cache_suffix() -> None:
    missing: list[str] = []
    for workflow in _workflow_files():
        steps = _parse_setup_uv_steps(workflow.read_text(encoding="utf-8"))
        for job, suffixes in steps.items():
            for index, suffix in enumerate(suffixes):
                if not suffix:
                    missing.append(
                        f"{workflow.name}: job {job!r} setup-uv step #{index + 1}"
                    )
    assert not missing, (
        "setup-uv steps are missing a cache-suffix — concurrent jobs collide on "
        "the same cache key and emit 'Unable to reserve cache' warnings:\n"
        + "\n".join(missing)
    )


def test_cache_suffixes_unique_per_workflow() -> None:
    collisions: list[str] = []
    for workflow in _workflow_files():
        steps = _parse_setup_uv_steps(workflow.read_text(encoding="utf-8"))
        owners: dict[str, list[str]] = {}
        for job, suffixes in steps.items():
            for suffix in suffixes:
                if not suffix:
                    continue
                resolved = suffix.replace("${{ github.job }}", job)
                owners.setdefault(resolved, []).append(job)
        for resolved, jobs in owners.items():
            if len(jobs) > 1:
                collisions.append(
                    f"{workflow.name}: suffix {resolved!r} used by jobs {jobs}"
                )
    assert not collisions, (
        "cache-suffix values must be unique within a workflow so concurrent jobs "
        "never share a cache key:\n" + "\n".join(collisions)
    )


def test_ci_setup_uv_jobs_are_covered() -> None:
    """Pin the exact jobs that regressed (`extras` emitted the warning)."""
    steps = _parse_setup_uv_steps(
        (WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8")
    )
    expected = {"check", "test", "sdk", "extras", "training"}
    assert expected <= steps.keys(), (
        f"ci.yml is missing jobs: {expected - steps.keys()}"
    )
    for job in sorted(expected):
        assert steps[job], f"ci.yml job {job!r} uses setup-uv without a cache-suffix"
