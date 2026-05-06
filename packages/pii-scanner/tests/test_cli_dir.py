"""End-to-end-ish: invoke the CLI on the bundled fixtures dir."""

import json
from pathlib import Path

from click.testing import CliRunner

from pleno_pii_scanner.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def test_dir_finds_pii_in_positive_fixtures():
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "dir",
            str(FIXTURES / "positive"),
            "--report-format",
            "json",
            "--no-color",
            "--exit-zero",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    entities = {f["entity"] for f in data["findings"]}
    assert "PHONE_NUMBER" in entities
    assert "EMAIL_ADDRESS" in entities
    assert "CREDIT_CARD" in entities
    assert "BANK_ACCOUNT" in entities


def test_dir_inline_ignore_suppresses_negatives():
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "dir",
            str(FIXTURES / "negative"),
            "--report-format",
            "json",
            "--no-color",
            "--exit-zero",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["findings"] == [], f"unexpected findings: {data['findings']}"


def test_dir_exit_code_nonzero_on_findings():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["dir", str(FIXTURES / "positive"), "--report-format", "json", "--no-color"],
    )
    assert result.exit_code == 1


def test_sarif_output_shape():
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["dir", str(FIXTURES / "positive"), "--report-format", "sarif", "--exit-zero"],
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["version"] == "2.1.0"
    assert data["runs"][0]["tool"]["driver"]["name"] == "pleno-pii-scanner"
    assert len(data["runs"][0]["results"]) > 0
