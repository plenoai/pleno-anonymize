"""Shared fixtures: isolated registry.

Stubs `entry_points` so the global connector registry does not pick
up other installed wheels (which would bleed kinds across tests).
"""

from __future__ import annotations

import pytest

from pleno_pii_scanner.sources import registry as _registry_mod


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Replace `entry_points` with a no-op so tests stay deterministic
    regardless of which third-party connector wheels happen to be
    installed in the workspace."""
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()
