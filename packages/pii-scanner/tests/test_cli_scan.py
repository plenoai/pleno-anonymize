"""Tests for `pleno-pii-scanner scan {kinds, run}`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from pleno_pii_scanner.cli import main
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner.sources.builtin import DIR_SPEC
from pleno_pii_scanner.sources.registry import register


@pytest.fixture(autouse=True)
def _isolated_registry():
    _registry_mod._reset_for_tests()
    register(DIR_SPEC)
    yield
    _registry_mod._reset_for_tests()


@pytest.fixture
def small_tree(tmp_path: Path) -> Path:
    # Separate subdir so test-helper files (TOML configs etc.) created
    # under tmp_path do not leak into the walker's enumeration.
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("hello\n")
    (root / "b.txt").write_text("world\n")
    return root


class TestKinds:
    def test_lists_registered(self) -> None:
        result = CliRunner().invoke(main, ["scan", "kinds"])
        assert result.exit_code == 0
        assert "dir" in result.output

    def test_empty_message(self) -> None:
        _registry_mod._reset_for_tests()
        result = CliRunner().invoke(main, ["scan", "kinds"])
        assert result.exit_code == 0
        assert "no connectors" in result.output


class TestRun:
    def test_runs_dir_via_inline_json(self, small_tree: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "scan",
                "run",
                "dir",
                "--config-json",
                json.dumps({"root": str(small_tree)}),
            ],
        )
        assert result.exit_code == 0
        # text output is on stderr; runner mixes them.
        assert "refs_seen=2" in result.output

    def test_runs_dir_via_toml_config(
        self, small_tree: Path, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "dir.toml"
        cfg.write_text(f'root = "{small_tree}"\n')
        result = CliRunner().invoke(
            main,
            [
                "scan",
                "run",
                "dir",
                "--config",
                str(cfg),
                "--report-format",
                "json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["source_kind"] == "dir"
        assert payload["refs_seen"] == 2

    def test_unknown_kind(self) -> None:
        result = CliRunner().invoke(
            main, ["scan", "run", "no-such-kind", "--config-json", "{}"]
        )
        assert result.exit_code != 0
        assert "unknown connector" in result.output.lower()

    def test_invalid_inline_json(self) -> None:
        result = CliRunner().invoke(
            main, ["scan", "run", "dir", "--config-json", "not-json"]
        )
        assert result.exit_code != 0
        assert "invalid --config-json" in result.output

    def test_inline_json_must_be_object(self) -> None:
        result = CliRunner().invoke(
            main, ["scan", "run", "dir", "--config-json", "[1,2,3]"]
        )
        assert result.exit_code != 0
        assert "object" in result.output

    def test_toml_invalid_syntax(self, tmp_path: Path) -> None:
        cfg = tmp_path / "bad.toml"
        cfg.write_text("this is = = invalid")
        result = CliRunner().invoke(
            main, ["scan", "run", "dir", "--config", str(cfg)]
        )
        assert result.exit_code != 0
        assert "not valid TOML" in result.output

    def test_no_config_falls_through(self, tmp_path: Path) -> None:
        # No --config and no --config-json → empty dict; dir factory
        # raises ValueError("requires 'root'"). Click's test runner
        # captures the raised exception on .exception when not handled
        # as a ClickException.
        result = CliRunner().invoke(main, ["scan", "run", "dir"])
        assert result.exit_code != 0
        assert result.exception is not None
        assert "root" in str(result.exception).lower()

    def test_scan_error_exits_two(self, small_tree: Path) -> None:
        # Point dir at a non-existent root: walker yields nothing but
        # discover succeeds. Force a real error by patching the
        # factory to return a connector whose discover throws.
        from pleno_pii_scanner.sources import registry as _registry_mod
        from pleno_pii_scanner.sources.builtin import DIR_SPEC
        from pleno_pii_scanner.sources.registry import (
            ConnectorSpec,
            register,
        )

        class _BadConnector:
            id = "bad"
            kind = "dir"

            def capabilities(self):
                from pleno_pii_scanner.sources.base import Capabilities

                return Capabilities()

            async def discover(self, _f, _c):
                raise RuntimeError("synthetic discover failure")
                yield  # pragma: no cover — make this an async generator

            async def fetch(self, _ref):
                return
                yield  # pragma: no cover

            async def close(self):
                return None

        _registry_mod._reset_for_tests()
        register(
            ConnectorSpec(
                kind="dir",
                version="test",
                factory=lambda _cfg: _BadConnector(),
                capabilities=DIR_SPEC.capabilities,
            )
        )
        result = CliRunner().invoke(
            main,
            ["scan", "run", "dir", "--config-json", "{}"],
        )
        assert result.exit_code == 2
        assert "synthetic discover failure" in result.output

    def test_filter_options_applied(self, small_tree: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "scan",
                "run",
                "dir",
                "--config-json",
                json.dumps({"root": str(small_tree)}),
                "--include",
                "a.*",
                "--report-format",
                "json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["refs_seen"] == 1
