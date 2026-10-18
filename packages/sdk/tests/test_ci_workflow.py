"""Regression guard for the concurrent setup-uv cache-save warning.

setup-uv defaults ``cache-dependency-glob`` to ``**/uv.lock``, so every CI job
in ``.github/workflows/ci.yml`` derives the *same* uv cache key. The jobs run
in parallel; when more than one tries to save that key, GitHub Actions only
lets the first reserve it and the rest log::

    Failed to save: Unable to reserve cache with key setup-uv-..., another
    job may be creating this cache.

That warning was observed on the ``extras`` job. The previous attempt at a fix
relied on ``save-cache: false`` — but setup-uv has no such input (it only
exposes ``enable-cache`` and ``cache-suffix``), so the flag was silently
ignored and the job still tried to save the shared key.

The workflow now gives every uv-caching job a distinct ``cache-suffix`` so no
two jobs derive the same key, and the lint-only ``check`` job disables caching
entirely. These tests fail if a future change reintroduces a collision or
re-adds the unsupported input.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CI_YML = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"
SETUP_UV_PREFIX = "astral-sh/setup-uv"
# `save-cache` is not part of setup-uv's action.yml, so it is ignored.
UNSUPPORTED_INPUTS = {"save-cache"}


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


def test_no_unsupported_save_cache_input() -> None:
    """`save-cache` is not a setup-uv input, so it silently does nothing."""
    offenders = [
        job_name
        for job_name, with_ in _setup_uv_steps()
        if UNSUPPORTED_INPUTS & with_.keys()
    ]
    assert not offenders, (
        "astral-sh/setup-uv has no `save-cache` input, so it is ignored and the "
        f"job still saves the shared key: {offenders}. Use `enable-cache: false` "
        "or a distinct `cache-suffix` instead."
    )


def test_every_caching_job_has_a_unique_cache_suffix() -> None:
    """Every job that enables caching must own a distinct cache key."""
    by_suffix: dict[str, str] = {}
    for job_name, with_ in _setup_uv_steps():
        if with_.get("enable-cache") is False:
            continue
        suffix = with_.get("cache-suffix")
        assert suffix, (
            f"job {job_name!r} caches under the default shared key, which races "
            "with the other jobs; set a distinct `cache-suffix`"
        )
        assert suffix not in by_suffix, (
            f"jobs {by_suffix[suffix]!r} and {job_name!r} share cache-suffix "
            f"{suffix!r}; their keys collide and the save race returns"
        )
        by_suffix[suffix] = job_name


def test_extras_job_is_not_a_shared_key_writer() -> None:
    by_job = dict(_setup_uv_steps())
    assert "extras" in by_job, "expected an astral-sh/setup-uv step in the extras job"
    with_ = by_job["extras"]
    assert with_.get("enable-cache") is False or with_.get("cache-suffix"), (
        "extras job must not save the shared uv cache (see ci.yml comment)"
    )
