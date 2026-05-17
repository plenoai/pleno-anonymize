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
    assert "pleno_anonymize_ja" in output
    assert "pleno_anonymize_en" in output


def test_model_install_command_falls_back_to_uv(monkeypatch) -> None:
    from pleno_anonymize import _models

    monkeypatch.setattr(_models.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(_models.shutil, "which", lambda name: "/opt/bin/uv")

    cmd = _models._install_command("https://example.test/model.whl", quiet=True)

    assert cmd == [
        "/opt/bin/uv",
        "pip",
        "install",
        "--python",
        _models.sys.executable,
        "--quiet",
        "https://example.test/model.whl",
    ]


def test_version_flag() -> None:
    parser = _build_parser()
    try:
        parser.parse_args(["--version"])
    except SystemExit as e:
        assert e.code == 0
    else:  # pragma: no cover - argparse must SystemExit on --version
        raise AssertionError("expected SystemExit")


def test_engine_flag_defaults_to_builtin() -> None:
    parser = _build_parser()
    ns = parser.parse_args(["analyze", "hello"])
    assert ns.engine == "builtin"


def test_engine_flag_accepts_openai_privacy_filter() -> None:
    parser = _build_parser()
    ns = parser.parse_args(["analyze", "--engine", "openai-privacy-filter", "x"])
    assert ns.engine == "openai-privacy-filter"


def test_engine_flag_rejects_unknown() -> None:
    parser = _build_parser()
    try:
        parser.parse_args(["analyze", "--engine", "bogus", "x"])
    except SystemExit as e:
        assert e.code == 2
    else:  # pragma: no cover - argparse must SystemExit on bad choice
        raise AssertionError("expected SystemExit")


def test_opf_engine_label_mapping_covers_all_native_labels() -> None:
    from pleno_anonymize._opf import OPF_LABEL_TO_PLENO

    expected_native = {
        "account_number",
        "private_address",
        "private_email",
        "private_person",
        "private_phone",
        "private_url",
        "private_date",
        "secret",
    }
    assert set(OPF_LABEL_TO_PLENO.keys()) == expected_native
