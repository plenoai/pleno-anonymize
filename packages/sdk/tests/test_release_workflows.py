"""Regression guard: PyPI publishing requires a pushed version tag.

Both release workflows used to accept ``workflow_dispatch`` with
``dry_run: false``, and their tag-to-package-version check only ran for
``push``. A manual dispatch from a branch could therefore publish a package
without any version-tag push. These tests pin the fixed shape:

* manual dispatch has no inputs and only reaches a build-only job;
* ``id-token: write`` (PyPI trusted publishing) exists only on a job gated to
  a pushed tag of the right prefix;
* that job verifies the tag against the package version before publishing and
  never enables ``skip-existing`` (duplicate versions fail loudly).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"

RELEASE_WORKFLOWS = {
    "release-pypi.yml": "sdk",
    "release-presidio-extras.yml": "presidio-extras",
}


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) parses the bare ``on`` key as boolean True.
    return workflow.get("on", workflow.get(True)) or {}


def _publish_steps(job: dict) -> list[dict]:
    return [
        step
        for step in job.get("steps") or []
        if str(step.get("uses", "")).startswith(PUBLISH_ACTION)
    ]


@pytest.mark.parametrize(("name", "prefix"), RELEASE_WORKFLOWS.items())
def test_dispatch_has_no_publish_input(name: str, prefix: str) -> None:
    triggers = _triggers(_load(name))
    assert triggers["push"]["tags"] == [f"{prefix}/v*"]
    assert "workflow_dispatch" in triggers
    assert not (triggers["workflow_dispatch"] or {}).get("inputs")


@pytest.mark.parametrize(("name", "prefix"), RELEASE_WORKFLOWS.items())
def test_oidc_and_publish_only_on_tag_push(name: str, prefix: str) -> None:
    workflow = _load(name)
    assert "id-token" not in (workflow.get("permissions") or {})

    expected_if = (
        "github.event_name == 'push' && "
        f"startsWith(github.ref, 'refs/tags/{prefix}/v')"
    )
    publishing = []
    for job_name, job in workflow["jobs"].items():
        has_oidc = (job.get("permissions") or {}).get("id-token") == "write"
        publishes = bool(_publish_steps(job))
        if has_oidc or publishes:
            assert job.get("if") == expected_if, job_name
            publishing.append(job_name)
        else:
            assert job.get("if") == "github.event_name == 'workflow_dispatch'"
    assert len(publishing) == 1


@pytest.mark.parametrize(("name", "prefix"), RELEASE_WORKFLOWS.items())
def test_publish_job_verifies_version_and_rejects_duplicates(
    name: str, prefix: str
) -> None:
    workflow = _load(name)
    (job,) = [j for j in workflow["jobs"].values() if _publish_steps(j)]
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    verify = names.index("Verify tag matches package version")
    publish = steps.index(_publish_steps(job)[0])
    assert verify < publish
    # The check must be unconditional inside the tag-push job.
    assert "if" not in steps[verify]
    assert "if" not in steps[publish]
    assert steps[publish]["with"].get("skip-existing", False) is False
