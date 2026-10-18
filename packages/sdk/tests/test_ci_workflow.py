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

``save-cache`` is not an input of ``astral-sh/setup-uv`` (the action only
exposes ``enable-cache``/``cache-suffix``), so an earlier attempt that set
``save-cache: false`` was silently ignored and the shared key was still saved.

The repository therefore gives every caching step — across every workflow, not
just ``ci.yml`` — a unique ``cache-suffix`` so no two steps can ever compute the
same key. The lint-only ``check`` job additionally disables caching entirely
with ``enable-cache: false`` (while still declaring a suffix so no step is left
on the shared default key). These tests fail if a future change reintroduces a
collision (a suffix-less saver or duplicate suffixes) or the unsupported
``save-cache`` input, anywhere under ``.github/workflows``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"
CI_YML = WORKFLOW_DIR / "ci.yml"
SETUP_UV_PREFIX = "astral-sh/setup-uv"
# `save-cache` is not part of setup-uv's action.yml, so it is silently ignored.
UNSUPPORTED_INPUTS = {"save-cache"}


def _iter_setup_uv(path: Path) -> Iterator[tuple[str, dict]]:
    """Yield ``(job_name, with)`` for every setup-uv step in ``path``."""
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


def test_no_unsupported_save_cache_input() -> None:
    """`save-cache` is not a setup-uv input, so it silently does nothing."""
    offenders = [
        f"{workflow}:{job}"
        for workflow, job, with_ in _all_setup_uv_steps()
        if UNSUPPORTED_INPUTS & with_.keys()
    ]
    assert not offenders, (
        "astral-sh/setup-uv has no `save-cache` input, so it is ignored and the "
        f"step still saves the shared key: {offenders}. Use `enable-cache: false` "
        "or a distinct `cache-suffix` instead."
    )


def test_every_caching_job_has_a_unique_cache_suffix() -> None:
    """Every step that enables caching must own a distinct cache key."""
    by_suffix: dict[str, str] = {}
    for workflow, job, with_ in _all_setup_uv_steps():
        if with_.get("enable-cache") is False:
            continue
        suffix = with_.get("cache-suffix")
        assert suffix, (
            f"{workflow}:{job} caches under the default shared key, which races "
            "with every other caching step; set a distinct `cache-suffix`"
        )
        assert suffix not in by_suffix, (
            f"{by_suffix[suffix]} and {workflow}:{job} share cache-suffix "
            f"{suffix!r}; their keys collide and the save race returns"
        )
        by_suffix[suffix] = f"{workflow}:{job}"


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


def test_exactly_one_setup_uv_cache_writer() -> None:
    writers = [
        job_name
        for job_name, with_ in _ci_setup_uv_steps()
        if _writes_cache(with_) and _key(with_) == "<shared>"
    ]

    assert len(writers) <= 1, (
        "multiple jobs would race to save the same setup-uv cache key "
        "(default cache-dependency-glob is `**/uv.lock`): "
        f"{writers}. Give every other step `enable-cache: false` or a unique "
        "`cache-suffix`."
    )


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


def test_sdk_job_is_not_a_second_cache_writer() -> None:
    by_job = dict(_ci_setup_uv_steps())
    assert "sdk" in by_job, "expected an astral-sh/setup-uv step in the sdk job"
    with_ = by_job["sdk"]
    assert (
        with_.get("enable-cache") is False
        or with_.get("save-cache", True) is False
        or with_.get("cache-suffix")
    ), "sdk job must not save the shared uv cache (see ci.yml comment)"


def test_extras_job_is_not_a_shared_key_writer() -> None:
    by_job = dict(_ci_setup_uv_steps())
    assert "extras" in by_job, "expected an astral-sh/setup-uv step in the extras job"
    with_ = by_job["extras"]
    assert with_.get("enable-cache") is False or with_.get("cache-suffix"), (
        "extras job must not save the shared uv cache (see ci.yml comment)"
    )


def test_training_job_is_not_a_second_cache_writer() -> None:
    by_job = dict(_ci_setup_uv_steps())
    assert "training" in by_job, (
        "expected an astral-sh/setup-uv step in the training job"
    )
    with_ = by_job["training"]
    assert (
        with_.get("enable-cache") is False or with_.get("cache-suffix")
    ), "training job must not save the shared uv cache (see ci.yml comment)"


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
