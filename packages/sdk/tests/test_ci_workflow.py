"""Regression guard for the concurrent setup-uv cache-save warning.

setup-uv defaults ``cache-dependency-glob`` to ``**/uv.lock``, so every step
that does not override the key derives the *same* uv cache key. GitHub Actions
lets only one job reserve a key at a time; the losers log::

    Failed to save: Unable to reserve cache with key setup-uv-..., another
    job may be creating this cache.

The key is scoped to a repository *ref*, so the warning can be produced by two
jobs inside one workflow, by two concurrent runs of one workflow, or by a
different workflow dispatched against the same ref (e.g. a release workflow
manually run on ``main`` while CI runs).

That warning was observed first on the ``sdk`` job and then again on the
``training`` and ``extras`` jobs. The earlier fix set ``save-cache: false`` on
the extra jobs, but ``astral-sh/setup-uv`` has **no ``save-cache`` input** — it
was silently ignored, so every job kept saving the shared key. The workflow now
avoids the race for real: every uv job declares its own unique ``cache-suffix``
(a distinct key cannot collide), and the lint-only ``check`` job disables
caching entirely with ``enable-cache: false`` (while still declaring a suffix so
no step is left on the shared default key).

These tests fail if a future change leaves any step on the shared key, reuses a
suffix (within one workflow or across the whole repo), or reaches for the
non-existent ``save-cache`` switch again.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"
CI_YML = WORKFLOW_DIR / "ci.yml"
SETUP_UV_PREFIX = "astral-sh/setup-uv"


def _iter_setup_uv(path: Path) -> Iterator[tuple[str, dict]]:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            uses = step.get("uses", "")
            if isinstance(uses, str) and uses.startswith(SETUP_UV_PREFIX):
                yield job_name, step.get("with") or {}


def _ci_setup_uv_steps() -> list[tuple[str, dict]]:
    return list(_iter_setup_uv(CI_YML))


def _all_setup_uv_steps() -> list[tuple[str, str, dict]]:
    """Return ``(workflow_file, job_name, with)`` for every setup-uv step."""
    steps: list[tuple[str, str, dict]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        steps.extend((path.name, job, with_) for job, with_ in _iter_setup_uv(path))
    return steps


def _writes_cache(with_: dict) -> bool:
    """True if this setup-uv step may attempt to save a cache entry.

    ``enable-cache: false`` is setup-uv's only cache switch (there is no
    ``save-cache`` input); it disables both restore and save.
    """
    return with_.get("enable-cache") is not False


def _key(with_: dict) -> str:
    """The collision identity of a step's key (suffix, or the shared default)."""
    return with_.get("cache-suffix") or "<shared>"


def test_ci_yml_exists() -> None:
    assert CI_YML.is_file(), f"missing workflow: {CI_YML}"


def test_no_job_uses_nonexistent_save_cache_input() -> None:
    offenders = [
        f"{workflow}:{job}"
        for workflow, job, with_ in _all_setup_uv_steps()
        if "save-cache" in with_
    ]
    assert not offenders, (
        "`save-cache` is not an astral-sh/setup-uv input; it is silently "
        "ignored and does not stop a job from saving the cache. Give every "
        f"step a unique `cache-suffix` instead (offenders: {offenders})"
    )


def test_exactly_one_setup_uv_cache_writer() -> None:
    writers = [
        job_name
        for job_name, with_ in _ci_setup_uv_steps()
        if _writes_cache(with_) and _key(with_) == "<shared>"
    ]

    assert len(writers) <= 1, (
        "multiple jobs would race to save the same setup-uv cache key "
        "(default cache-dependency-glob is `**/uv.lock`): "
        f"{writers}. Give every job a unique `cache-suffix`, or "
        "`enable-cache: false` if it does not need the cache."
    )


def test_sdk_job_is_not_a_second_cache_writer() -> None:
    by_job = dict(_ci_setup_uv_steps())
    assert "sdk" in by_job, "expected an astral-sh/setup-uv step in the sdk job"
    with_ = by_job["sdk"]
    assert (
        with_.get("enable-cache") is False or with_.get("cache-suffix")
    ), "sdk job must not save the shared uv cache (see ci.yml comment)"


def test_training_job_is_not_a_second_cache_writer() -> None:
    by_job = dict(_ci_setup_uv_steps())
    assert "training" in by_job, (
        "expected an astral-sh/setup-uv step in the training job"
    )
    with_ = by_job["training"]
    assert (
        with_.get("enable-cache") is False or with_.get("cache-suffix")
    ), "training job must not save the shared uv cache (see ci.yml comment)"


def test_no_job_writes_the_shared_setup_uv_cache_key() -> None:
    # A setup-uv step without `enable-cache: false` and without a
    # `cache-suffix` saves the default `**/uv.lock` key. Any such step shares
    # that key with every other run on the same ref — including release
    # workflows dispatched against the same ref as CI — so there should be
    # none. This generalizes the old "exactly one writer" model: because
    # setup-uv cannot be made restore-only, the only safe number of writers is
    # zero.
    writers = [
        f"{workflow}:{job}"
        for workflow, job, with_ in _all_setup_uv_steps()
        if _writes_cache(with_) and _key(with_) == "<shared>"
    ]

    assert not writers, (
        "no job may write the shared setup-uv cache key (default "
        "cache-dependency-glob `**/uv.lock`), otherwise concurrent jobs race to "
        f"reserve it and log 'Unable to reserve cache'. Jobs still on the "
        f"shared key: {writers}. Give every uv job a unique `cache-suffix`, or "
        "set `enable-cache: false` if it does not need the cache."
    )


def test_every_setup_uv_step_has_a_unique_cache_suffix() -> None:
    suffixes: list[str] = []
    for workflow, job, with_ in _all_setup_uv_steps():
        suffix = with_.get("cache-suffix")
        assert suffix, (
            f"{workflow}:{job}: setup-uv step lacks a cache-suffix and would "
            "share the default `**/uv.lock` cache key with its siblings"
        )
        suffixes.append(suffix)

    assert len(suffixes) == len(set(suffixes)), (
        "cache-suffix values must be unique per step, otherwise two jobs "
        f"collide on the same key again: {suffixes}"
    )


def test_no_two_saving_steps_share_a_cache_key() -> None:
    by_key: dict[str, list[str]] = {}
    for workflow, job, with_ in _all_setup_uv_steps():
        if not _writes_cache(with_):
            continue
        by_key.setdefault(_key(with_), []).append(f"{workflow}:{job}")

    collisions = {key: jobs for key, jobs in by_key.items() if len(jobs) > 1}
    assert not collisions, (
        "these setup-uv steps compute the same cache key and may both try to "
        f"save it (causing 'Unable to reserve cache' warnings): {collisions}. "
        "Give each step a unique `cache-suffix`."
    )
