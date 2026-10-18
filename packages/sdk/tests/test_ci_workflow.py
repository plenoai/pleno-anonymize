"""Regression guard for the concurrent setup-uv cache-save warning.

setup-uv defaults ``cache-dependency-glob`` to ``**/uv.lock``, so every CI job
in ``.github/workflows/ci.yml`` derives the *same* uv cache key. The jobs run
in parallel; when more than one tries to save that key, GitHub Actions only
lets the first reserve it and the rest log::

    Failed to save: Unable to reserve cache with key setup-uv-..., another
    job may be creating this cache.

That warning was observed on the ``sdk`` job. The workflow now has exactly one
writer for the shared key (the ``test`` job); the other uv-sync jobs restore
with ``save-cache: false`` and the lint-only ``check`` job disables caching
entirely. These tests fail if a future change reintroduces a second writer.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CI_YML = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"
SETUP_UV_PREFIX = "astral-sh/setup-uv"


def _setup_uv_steps() -> list[tuple[str, dict]]:
    workflow = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    steps: list[tuple[str, dict]] = []
    for job_name, job in workflow.get("jobs", {}).items():
        for step in job.get("steps", []) or []:
            uses = step.get("uses", "")
            if isinstance(uses, str) and uses.startswith(SETUP_UV_PREFIX):
                steps.append((job_name, step.get("with", {}) or {}))
    return steps


def test_ci_yml_exists() -> None:
    assert CI_YML.is_file(), f"missing workflow: {CI_YML}"


def test_exactly_one_setup_uv_cache_writer() -> None:
    writers = []
    for job_name, with_ in _setup_uv_steps():
        if with_.get("enable-cache") is False:
            continue
        if with_.get("save-cache", True) is False:
            continue
        # A per-job suffix yields a distinct key, so it cannot collide.
        if with_.get("cache-suffix"):
            continue
        writers.append(job_name)

    assert len(writers) <= 1, (
        "multiple jobs would race to save the same setup-uv cache key "
        "(default cache-dependency-glob is `**/uv.lock`): "
        f"{writers}. Keep one writer and set `save-cache: false` on the rest."
    )


def test_sdk_job_is_not_a_second_cache_writer() -> None:
    by_job = dict(_setup_uv_steps())
    assert "sdk" in by_job, "expected an astral-sh/setup-uv step in the sdk job"
    with_ = by_job["sdk"]
    assert (
        with_.get("enable-cache") is False
        or with_.get("save-cache", True) is False
        or with_.get("cache-suffix")
    ), "sdk job must not save the shared uv cache (see ci.yml comment)"
