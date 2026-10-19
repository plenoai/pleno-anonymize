"""Regression guard for the sisakulint findings on CI/CD workflows.

Two warnings used to be emitted by the ``sisakulint`` scan job:

* ``self-hosted-runner`` for every job in every workflow. This repository is
  public, so a self-hosted runner lets any pull request execute arbitrary code
  on persistent infrastructure. Every job must therefore select a
  GitHub-hosted runner.
* ``cache-poisoning`` (cache hierarchy exploitation) on ``website.yml``. That
  workflow can run from an external trigger (``workflow_dispatch``) and on
  pushes to the default branch, so a persistent dependency cache written there
  could be read by later builds. Workflows with that trigger shape must not
  enable dependency caching.

These tests parse the workflow YAML so a future edit cannot silently
reintroduce either warning. They intentionally encode the same scope as the
sisakulint rules (``actions/cache`` and ``actions/setup-*`` with ``cache:``
enabled); ``astral-sh/setup-uv`` is not treated as a cache action by the rule.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[3] / ".github" / "workflows"

GITHUB_HOSTED_PREFIXES = ("ubuntu-", "windows-", "macos-")
EXTERNAL_TRIGGERS = {"workflow_dispatch", "schedule", "repository_dispatch"}
# Cache-writing actions recognised by sisakulint's cache-poisoning rule.
CACHE_ACTIONS = {"actions/cache", "actions/cache/save", "actions/cache/restore"}


def _iter_workflows() -> Iterator[tuple[Path, dict]]:
    for path in sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(
        WORKFLOW_DIR.glob("*.yaml")
    ):
        yield path, yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _triggers(workflow: dict) -> dict | list | str | None:
    """Return the workflow's ``on:`` block.

    PyYAML implements YAML 1.1, where the bare key ``on`` parses as ``True``;
    accept both spellings so the test does not depend on the loader version.
    """
    if "on" in workflow:
        return workflow["on"]
    return workflow.get(True)


def _trigger_names(triggers: dict | list | str | None) -> set[str]:
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {name for name in triggers if isinstance(name, str)}
    if isinstance(triggers, dict):
        return {name for name in triggers if isinstance(name, str)}
    return set()


def _pushes_to_default_branch(triggers: dict | list | str | None) -> bool:
    if isinstance(triggers, (str, list)):
        return "push" in _trigger_names(triggers)
    if not isinstance(triggers, dict):
        return False
    push = triggers.get("push")
    if push is None:
        # `push:` with no value is impossible (the key itself means all
        # branches), but a missing key means this workflow does not push.
        return "push" in _trigger_names(triggers)
    if not isinstance(push, dict):
        return True
    branches = push.get("branches")
    if branches is None:
        # No branch filter -> every branch, including the default branch.
        return True
    if isinstance(branches, str):
        branches = [branches]
    return any(branch in {"main", "master"} for branch in branches)


def _labels(runs_on: object) -> list[str]:
    if isinstance(runs_on, str):
        return [runs_on]
    if isinstance(runs_on, list):
        return [label for label in runs_on if isinstance(label, str)]
    return []


def _is_cache_step(step: dict) -> bool:
    uses = step.get("uses")
    if not isinstance(uses, str):
        return False
    action = uses.split("@", 1)[0]
    if action in CACHE_ACTIONS:
        return True
    if action.startswith("actions/setup-"):
        cache = (step.get("with") or {}).get("cache")
        return cache not in (None, "", False, "false", "False")
    return False


def test_every_job_uses_a_github_hosted_runner() -> None:
    offenders: list[str] = []
    for path, workflow in _iter_workflows():
        for job_name, job in (workflow.get("jobs") or {}).items():
            runs_on = job.get("runs-on")
            if isinstance(runs_on, dict):
                # A runner group normally contains self-hosted runners and is
                # reported by the same sisakulint rule.
                offenders.append(f"{path.name}:{job_name} selects a runner group")
                continue
            labels = _labels(runs_on)
            if any(label.lower() == "self-hosted" for label in labels):
                offenders.append(f"{path.name}:{job_name} requests self-hosted")
                continue
            if not any(label.startswith(GITHUB_HOSTED_PREFIXES) for label in labels):
                offenders.append(
                    f"{path.name}:{job_name} is not on a GitHub-hosted runner: "
                    f"{runs_on!r}"
                )

    assert not offenders, (
        "public workflows must run on GitHub-hosted runners; self-hosted "
        "runners let pull requests execute arbitrary code on persistent "
        f"infrastructure (sisakulint self-hosted-runner): {offenders}"
    )


def test_no_dependency_cache_in_externally_triggered_default_branch_workflows() -> None:
    offenders: list[str] = []
    for path, workflow in _iter_workflows():
        triggers = _triggers(workflow)
        if not (
            _trigger_names(triggers) & EXTERNAL_TRIGGERS
            and _pushes_to_default_branch(triggers)
        ):
            continue
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if _is_cache_step(step):
                    offenders.append(f"{path.name}:{job_name} ({step.get('uses')})")

    assert not offenders, (
        "workflows that can be triggered externally and push to the default "
        "branch must not enable dependency caching: an externally triggered "
        "run could poison the cache that later builds read (sisakulint "
        f"cache-poisoning cache hierarchy): {offenders}"
    )
