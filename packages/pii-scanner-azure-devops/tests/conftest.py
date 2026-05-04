"""Shared fixtures for pii-scanner-azure-devops tests.

Hermetic by design: every fixture either uses `httpx.MockTransport`
(the API-level tests) or a stubbed `clone_fn` / `enumerate_fn` (the
connector-level tests). Nothing here opens a TCP socket.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from pleno_pii_scanner.sources import registry as _registry_mod


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Reset the connector registry around every test.

    Avoids cross-test pollution when factory tests register the SPEC
    and a later test re-registers it (DuplicateConnectorError).
    """
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[[str, dict[str, str]], Path]:
    """Return a factory that builds a fake cloned repo on disk."""

    def _make(name: str, files: dict[str, str]) -> Path:
        repo = tmp_path / name
        repo.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            full = repo / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
        return repo

    return _make
