"""Tests for `pleno-pii-scanner connectors list|describe`."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from pleno_pii_scanner.cli import main
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner.sources.builtin import (
    DIR_SPEC,
    GIT_SPEC,
    GITHUB_SPEC,
)
from pleno_pii_scanner.sources.registry import register


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Re-populate only the builtins so list/describe are deterministic."""
    _registry_mod._reset_for_tests()
    for spec in (DIR_SPEC, GIT_SPEC, GITHUB_SPEC):
        register(spec)
    yield
    _registry_mod._reset_for_tests()


class TestConnectorsList:
    def test_text_format(self) -> None:
        result = CliRunner().invoke(main, ["connectors", "list"])
        assert result.exit_code == 0
        assert "dir" in result.output
        assert "git" in result.output
        assert "github" in result.output

    def test_json_format(self) -> None:
        result = CliRunner().invoke(
            main, ["connectors", "list", "--format", "json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        kinds = {entry["kind"] for entry in payload}
        assert kinds == {"dir", "git", "github"}
        for entry in payload:
            assert "version" in entry
            assert "max_concurrent_fetches" in entry

    def test_empty_registry_message(self) -> None:
        _registry_mod._reset_for_tests()
        result = CliRunner().invoke(main, ["connectors", "list"])
        assert result.exit_code == 0
        assert "no connectors" in result.output


class TestConnectorsDescribe:
    def test_describe_known(self) -> None:
        result = CliRunner().invoke(main, ["connectors", "describe", "dir"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["kind"] == "dir"
        assert payload["capabilities"]["incremental"] is False
        assert payload["config_schema"] is None

    def test_describe_unknown(self) -> None:
        result = CliRunner().invoke(
            main, ["connectors", "describe", "no-such-kind"]
        )
        assert result.exit_code != 0
        assert "unknown connector" in result.output.lower()
