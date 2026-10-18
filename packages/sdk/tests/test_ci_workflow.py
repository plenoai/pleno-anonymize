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

The repository therefore has exactly one designated writer for the shared
(suffix-less) key: the ``test`` job in ``.github/workflows/ci.yml``. Every other
setup-uv step must either disable caching (``enable-cache: false``), be
restore-only (``save-cache: false``), or carry a unique ``cache-suffix`` so its
key cannot collide with the shared one. These tests fail if a future change
reintroduces a second saver for the shared key anywhere under
``.github/workflows``.
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
    """True if this setup-uv step may attempt to save a cache entry."""
    return (
        with_.get("enable-cache") is not False
        and with_.get("save-cache", True) is not False
    )


def _key(with_: dict) -> str:
    """The collision identity of a step's key (suffix, or the shared default)."""
    return with_.get("cache-suffix") or "<shared>"


def test_ci_yml_exists() -> None:
    assert CI_YML.is_file(), f"missing workflow: {CI_YML}"


def test_exactly_one_setup_uv_cache_writer() -> None:
    writers = [
        job_name
        for job_name, with_ in _ci_setup_uv_steps()
        if _writes_cache(with_) and _key(with_) == "<shared>"
    ]

    assert len(writers) <= 1, (
        "multiple jobs would race to save the same setup-uv cache key "
        "(default cache-dependency-glob is `**/uv.lock`): "
        f"{writers}. Keep one writer and set `save-cache: false` / a unique "
        "`cache-suffix` on the rest."
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


def test_only_test_job_writes_the_shared_cache_key() -> None:
    writers = [
        f"{workflow}:{job}"
        for workflow, job, with_ in _all_setup_uv_steps()
        if _writes_cache(with_) and _key(with_) == "<shared>"
    ]
    assert writers == ["ci.yml:test"], (
        "the shared (suffix-less) setup-uv cache key must have exactly one "
        "writer across all workflows, or concurrent runs race for the cache "
        f"reservation; got {writers}. Set `save-cache: false` (restore-only) "
        "or a unique `cache-suffix` on every other step."
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
