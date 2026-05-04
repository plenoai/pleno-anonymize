"""Shared pytest fixtures for pleno-pii-scanner-gitlab.

Two reusable building blocks:

  * `make_credential` builds a `Credential` for any of the three auth
    modes — keeps tests focused on behaviour, not boilerplate.
  * `clone_dir` materialises a fake "cloned" project directory with a
    handful of files so the dir-connector replay path exercises real
    filesystem code instead of being mocked through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pleno_pii_scanner.credentials.broker import Credential


def make_credential(
    *,
    mode: str = "pat",
    token: str = "glpat-test-token",
) -> Credential:
    """Build a Credential payload for the requested auth mode.

    OAuth uses `access_token` as the canonical key; PAT and project use
    `token`. We populate the canonical key to keep tests aligned with
    operator-visible config; the connector accepts either alias.
    """
    if mode == "oauth":
        payload = {"auth": "oauth", "access_token": token}
    else:
        payload = {"auth": mode, "token": token}
    return Credential(kind="gitlab", payload=payload)


@pytest.fixture
def clone_dir(tmp_path: Path) -> Path:
    """A fake shallow clone directory with one secret-bearing file.

    The path is unique per test (`tmp_path` is function-scoped) so
    parallel test runs do not collide. `close()` does not need to
    rmtree this — pytest cleans up `tmp_path` automatically.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("hello\n")
    (root / "secret.env").write_text("AWS_KEY=AKIAIOSFODNN7EXAMPLE\n")
    sub = root / "src"
    sub.mkdir()
    (sub / "app.py").write_text("password = 'hunter2'\n")
    return root
