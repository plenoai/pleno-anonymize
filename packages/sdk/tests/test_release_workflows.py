"""Guard the PyPI release workflows: only a tag push may publish.

Manual ``workflow_dispatch`` runs are build-only and never receive an OIDC
token; the tag-push ``publish`` job verifies the tag against the package
version and refuses to silently skip an already-published version.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"
RELEASES = {
    "release-pypi.yml": "sdk",
    "release-presidio-extras.yml": "presidio-extras",
}
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # PyYAML parses the bare `on:` key as boolean True.
    return workflow.get("on", workflow.get(True)) or {}


def _publish_steps(job: dict) -> list[dict]:
    return [
        s for s in job.get("steps") or [] if str(s.get("uses", "")).startswith(PUBLISH_ACTION)
    ]


@pytest.mark.parametrize("name", sorted(RELEASES))
def test_dispatch_has_no_dry_run_input(name: str) -> None:
    dispatch = _triggers(_load(name)).get("workflow_dispatch") or {}
    assert "dry_run" not in (dispatch.get("inputs") or {})


@pytest.mark.parametrize("name", sorted(RELEASES))
def test_only_tag_push_job_publishes(name: str) -> None:
    jobs = _load(name)["jobs"]
    assert set(jobs) == {"build", "publish"}

    build = jobs["build"]
    assert build["if"] == "github.event_name == 'workflow_dispatch'"
    assert "id-token" not in (build.get("permissions") or {})
    assert not _publish_steps(build)

    publish = jobs["publish"]
    assert "github.event_name == 'push'" in publish["if"]
    assert f"refs/tags/{RELEASES[name]}/v" in publish["if"]
    assert publish["permissions"]["id-token"] == "write"


@pytest.mark.parametrize("name", sorted(RELEASES))
def test_publish_verifies_version_and_never_skips_existing(name: str) -> None:
    publish = _load(name)["jobs"]["publish"]
    steps = publish["steps"]
    names = [s.get("name") for s in steps]
    assert "Verify tag matches package version" in names
    verify = steps[names.index("Verify tag matches package version")]
    assert "if" not in verify
    assert f"packages/{RELEASES[name]}/pyproject.toml" in verify["run"]

    (upload,) = _publish_steps(publish)
    assert "if" not in upload
    assert upload["with"]["skip-existing"] is False
    assert steps.index(verify) < steps.index(upload)
