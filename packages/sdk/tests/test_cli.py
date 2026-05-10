"""CLI argument parser smoke tests — no engine is constructed."""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from pleno_anonymize._cli import _build_parser, main


def test_parser_supports_all_subcommands() -> None:
    parser = _build_parser()
    for sub in ("scan", "analyze", "redact", "models", "health"):
        ns = parser.parse_args([sub] if sub != "models" else [sub, "status"])
        assert ns.command == sub


def test_models_status_lists_all_known_models() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["models", "status"])
    assert rc == 0
    output = buf.getvalue()
    assert "ja_ner_ja" in output
    assert "en_ner_en" in output


def test_version_flag() -> None:
    parser = _build_parser()
    try:
        parser.parse_args(["--version"])
    except SystemExit as e:
        assert e.code == 0
    else:  # pragma: no cover - argparse must SystemExit on --version
        raise AssertionError("expected SystemExit")
