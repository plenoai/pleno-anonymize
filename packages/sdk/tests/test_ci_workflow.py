"""Regression guard for the concurrent setup-uv cache-save warning.

setup-uv defaults ``cache-dependency-glob`` to ``**/uv.lock``, so every CI job
in ``.github/workflows/ci.yml`` derives the *same* uv cache key. The jobs run
in parallel; when more than one tries to save that key, GitHub Actions only
lets the first reserve it and the rest log::

    Failed to save: Unable to reserve cache with key setup-uv-..., another
    job may be creating this cache.

That warning was observed first on the ``sdk`` job and then again on the
``training`` job. The earlier fix set ``save-cache: false`` on the extra jobs,
but ``astral-sh/setup-uv`` has **no ``save-cache`` input** — it was silently
ignored, so every job kept saving the shared key. The workflow now avoids the
race for real: every uv job declares its own unique ``cache-suffix`` (a
distinct key cannot collide), and the lint-only ``check`` job disables caching
entirely with ``enable-cache: false`` (while still declaring a suffix so no
step is left on the shared default key). These tests fail if a future change
leaves a job on the shared key or reaches for the non-existent ``save-cache``
switch again.
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


def test_no_job_uses_nonexistent_save_cache_input() -> None:
    offenders = []
    for job_name, with_ in _setup_uv_steps():
        if "save-cache" in with_:
            offenders.append(job_name)
    assert not offenders, (
        "`save-cache` is not an astral-sh/setup-uv input; it is silently "
        "ignored and does not stop a job from saving the cache. Use "
        f"`enable-cache: false` or a unique `cache-suffix` instead: {offenders}"
    )


def test_no_job_writes_the_shared_setup_uv_cache_key() -> None:
    writers = []
    for job_name, with_ in _setup_uv_steps():
        if with_.get("enable-cache") is False:
            continue
        # A per-job suffix yields a distinct key, so it cannot collide.
        if with_.get("cache-suffix"):
            continue
        writers.append(job_name)

    assert writers == [], (
        "no job may write the shared setup-uv cache key (default "
        "cache-dependency-glob `**/uv.lock`), otherwise concurrent jobs race to "
        f"reserve it and log 'Unable to reserve cache'. Give every uv job a "
        "unique `cache-suffix`, or set `enable-cache: false` if it does not "
        f"need the cache. Jobs still on the shared key: {writers}"
    )


def test_restore_jobs_have_unique_cache_suffixes() -> None:
    suffixes = []
    for _, with_ in _setup_uv_steps():
        if with_.get("cache-suffix"):
            suffixes.append(with_["cache-suffix"])
    assert len(suffixes) == len(set(suffixes)), (
        "cache-suffix values must be unique per job, otherwise two jobs "
        f"collide on the same key again: {suffixes}"
    )


def test_sdk_job_is_not_a_second_cache_writer() -> None:
    by_job = dict(_setup_uv_steps())
    assert "sdk" in by_job, "expected an astral-sh/setup-uv step in the sdk job"
    with_ = by_job["sdk"]
    assert (
        with_.get("enable-cache") is False or with_.get("cache-suffix")
    ), "sdk job must not save the shared uv cache (see ci.yml comment)"


def test_training_job_is_not_a_second_cache_writer() -> None:
    by_job = dict(_setup_uv_steps())
    assert "training" in by_job, (
        "expected an astral-sh/setup-uv step in the training job"
    )
    with_ = by_job["training"]
    assert (
        with_.get("enable-cache") is False or with_.get("cache-suffix")
    ), "training job must not save the shared uv cache (see ci.yml comment)"
