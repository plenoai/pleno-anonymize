"""Guard the PyPI release workflows: only a matching tag push may publish.

``workflow_dispatch`` runs a build-only job with no OIDC token. The ``publish``
job is gated on a tag push, is the only job with ``id-token: write``, always
checks the tag against ``pyproject.toml`` before building, and never skips an
already-published version.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"
SETUP_UV_PREFIX = "astral-sh/setup-uv"
PYPI_PUBLISH_PREFIX = "pypa/gh-action-pypi-publish"

RELEASES = [
    ("release-pypi.yml", "release-pypi", "sdk"),
    ("release-presidio-extras.yml", "release-presidio-extras", "presidio-extras"),
]


def _load(filename: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / filename).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML parses the bare ``on`` key as boolean True.
    return workflow.get("on", workflow.get(True)) or {}


def _step_index(steps: list[dict], predicate) -> int:
    for i, step in enumerate(steps):
        if predicate(step):
            return i
    raise AssertionError("step not found")


def _uses(prefix: str):
    return lambda step: str(step.get("uses", "")).startswith(prefix)


def _named(name: str):
    return lambda step: step.get("name") == name


def _cache_suffix(job: dict) -> str:
    steps = job["steps"]
    return steps[_step_index(steps, _uses(SETUP_UV_PREFIX))]["with"]["cache-suffix"]


@pytest.mark.parametrize(("filename", "name", "prefix"), RELEASES)
def test_dispatch_has_no_dry_run_input(filename: str, name: str, prefix: str) -> None:
    triggers = _triggers(_load(filename))
    assert triggers["push"]["tags"] == [f"{prefix}/v*"]
    assert "workflow_dispatch" in triggers
    assert not (triggers["workflow_dispatch"] or {}).get("inputs")


@pytest.mark.parametrize(("filename", "name", "prefix"), RELEASES)
def test_build_job_is_build_only(filename: str, name: str, prefix: str) -> None:
    job = _load(filename)["jobs"]["build"]
    assert "if" not in job
    assert "id-token" not in (job.get("permissions") or {})
    assert not any(_uses(PYPI_PUBLISH_PREFIX)(s) for s in job["steps"])
    assert _cache_suffix(job) == f"{name}-build"


@pytest.mark.parametrize(("filename", "name", "prefix"), RELEASES)
def test_publish_job_requires_tag_push(filename: str, name: str, prefix: str) -> None:
    job = _load(filename)["jobs"]["publish"]
    expected_if = (
        f"github.event_name == 'push' && startsWith(github.ref, 'refs/tags/{prefix}/v')"
    )
    assert job["if"] == expected_if
    assert job["permissions"]["id-token"] == "write"
    assert _cache_suffix(job) == f"{name}-publish"


@pytest.mark.parametrize(("filename", "name", "prefix"), RELEASES)
def test_only_publish_job_has_oidc(filename: str, name: str, prefix: str) -> None:
    workflow = _load(filename)
    assert "id-token" not in (workflow.get("permissions") or {})
    with_oidc = [
        job_name
        for job_name, job in workflow["jobs"].items()
        if (job.get("permissions") or {}).get("id-token") == "write"
    ]
    assert with_oidc == ["publish"]


@pytest.mark.parametrize(("filename", "name", "prefix"), RELEASES)
def test_publish_verifies_version_and_never_skips(
    filename: str, name: str, prefix: str
) -> None:
    steps = _load(filename)["jobs"]["publish"]["steps"]
    verify = _step_index(steps, _named("Verify tag matches package version"))
    build = _step_index(steps, _named("Build wheel and sdist"))
    publish = _step_index(steps, _uses(PYPI_PUBLISH_PREFIX))
    assert verify < build < publish
    assert "if" not in steps[verify]
    assert "if" not in steps[publish]
    assert steps[publish]["with"]["skip-existing"] is False
