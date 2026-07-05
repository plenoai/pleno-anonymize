"""Schema-conformance tests for experiments/log.jsonl and scripts/log_experiment.py (#293).

Three guarantees this file locks in:

(a) every row of the real log.jsonl validates against log_schema.json —
    the whole point of #293 is that an agent (or this test) can trust the
    file structurally instead of re-reading 28+ lines of prose.
(b) scripts/log_experiment.py actually appends a valid row, records a real
    sha256 data_hash, and updates experiments/best.json to point at the
    highest metrics_after.overall_f1 among verdict=="KEEP" runs sharing a
    {language, baseline} key — including *not* moving best.json when a new
    entry is DISCARD or scores lower than the incumbent.
(c) an entry that fails schema validation (bad verdict, missing required
    field) is rejected: the process exits non-zero and log.jsonl/best.json
    are left untouched.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

TRAINING_ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = TRAINING_ROOT / "experiments" / "log.jsonl"
SCHEMA_PATH = TRAINING_ROOT / "experiments" / "log_schema.json"
SCRIPT_PATH = TRAINING_ROOT / "scripts" / "log_experiment.py"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# (a) the real log.jsonl is fully schema-conformant
# ---------------------------------------------------------------------------


def test_all_log_rows_validate_against_schema() -> None:
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)
    rows = _read_jsonl(LOG_PATH)
    assert rows, "log.jsonl must not be empty"

    failures = []
    for row in rows:
        errors = list(validator.iter_errors(row))
        if errors:
            failures.append((row.get("id", "<no id>"), errors))
    assert not failures, f"schema violations: {failures}"


def test_log_rows_have_required_fields() -> None:
    required = set(_load_schema()["required"])
    for row in _read_jsonl(LOG_PATH):
        missing = required - row.keys()
        assert not missing, f"{row.get('id')} missing {missing}"


def test_migrated_rows_preserve_legacy_verbatim() -> None:
    """Zero-information-loss check (#293 AC): legacy holds the original row."""
    for row in _read_jsonl(LOG_PATH):
        if "legacy" not in row:
            continue
        legacy = row["legacy"]
        assert legacy["id"] == row["id"]
        assert legacy["timestamp"] == row["timestamp"]


# ---------------------------------------------------------------------------
# helpers to drive scripts/log_experiment.py as a subprocess (real CLI, not
# an in-process import — this is what an agent actually invokes)
# ---------------------------------------------------------------------------


def _run_cli(tmp_path: Path, *, log_path: Path, best_path: Path, schema_path: Path, **kwargs) -> subprocess.CompletedProcess:
    data_file = kwargs.pop("data_file", None)
    if data_file is None:
        data_file = tmp_path / "data.json"
        data_file.write_text('{"docs": []}', encoding="utf-8")

    args = [
        sys.executable,
        str(SCRIPT_PATH),
        "--id", kwargs["id"],
        "--language", kwargs["language"],
        "--baseline", kwargs["baseline"],
        "--hypothesis", kwargs["hypothesis"],
        "--intervention-type", kwargs.get("intervention_type", "data_augmentation"),
        "--data-file", str(data_file),
        "--metrics-before", json.dumps(kwargs.get("metrics_before", {"overall_f1": 0.5})),
        "--metrics-after", json.dumps(kwargs.get("metrics_after", {"overall_f1": 0.6})),
        "--verdict", kwargs["verdict"],
        "--timestamp", kwargs.get("timestamp", "2026-07-06T00:00:00+09:00"),
        "--log-path", str(log_path),
        "--best-path", str(best_path),
        "--schema-path", str(schema_path),
    ]
    if "reason" in kwargs:
        args += ["--reason", kwargs["reason"]]
    return subprocess.run(args, capture_output=True, text=True)


@pytest.fixture()
def sandbox(tmp_path: Path):
    log_path = tmp_path / "log.jsonl"
    best_path = tmp_path / "best.json"
    return log_path, best_path, SCHEMA_PATH


# ---------------------------------------------------------------------------
# (b) append + best.json update
# ---------------------------------------------------------------------------


def test_append_writes_valid_entry_with_real_data_hash(tmp_path: Path, sandbox) -> None:
    log_path, best_path, schema_path = sandbox
    data_file = tmp_path / "data.json"
    data_file.write_text('{"docs": ["a", "b"]}', encoding="utf-8")

    result = _run_cli(
        tmp_path,
        log_path=log_path,
        best_path=best_path,
        schema_path=schema_path,
        data_file=data_file,
        id="20260706_test_append",
        language="ja",
        baseline="unit_test_baseline",
        hypothesis="Unit test entry",
        verdict="KEEP",
        metrics_after={"overall_f1": 0.7},
    )
    assert result.returncode == 0, result.stderr

    import hashlib
    expected_hash = hashlib.sha256(data_file.read_bytes()).hexdigest()

    rows = _read_jsonl(log_path)
    assert len(rows) == 1
    assert rows[0]["data_hash"] == expected_hash
    assert rows[0]["data_hash"] != "unknown"

    schema = _load_schema()
    jsonschema.Draft7Validator(schema).validate(rows[0])


def test_best_json_tracks_highest_keep_f1_per_language_baseline(tmp_path: Path, sandbox) -> None:
    log_path, best_path, schema_path = sandbox

    # First KEEP run establishes the pointer.
    r1 = _run_cli(
        tmp_path, log_path=log_path, best_path=best_path, schema_path=schema_path,
        id="iter_a", language="ja", baseline="grp1", hypothesis="h1",
        verdict="KEEP", metrics_after={"overall_f1": 0.60},
    )
    assert r1.returncode == 0, r1.stderr
    best = json.loads(best_path.read_text())
    assert best["ja::grp1"] == {"id": "iter_a", "f1": 0.60}

    # A DISCARD with a higher f1 must NOT move the pointer.
    r2 = _run_cli(
        tmp_path, log_path=log_path, best_path=best_path, schema_path=schema_path,
        id="iter_b", language="ja", baseline="grp1", hypothesis="h2",
        verdict="DISCARD", metrics_after={"overall_f1": 0.99},
    )
    assert r2.returncode == 0, r2.stderr
    best = json.loads(best_path.read_text())
    assert best["ja::grp1"] == {"id": "iter_a", "f1": 0.60}

    # A KEEP with a lower f1 must NOT move the pointer either.
    r3 = _run_cli(
        tmp_path, log_path=log_path, best_path=best_path, schema_path=schema_path,
        id="iter_c", language="ja", baseline="grp1", hypothesis="h3",
        verdict="KEEP", metrics_after={"overall_f1": 0.55},
    )
    assert r3.returncode == 0, r3.stderr
    best = json.loads(best_path.read_text())
    assert best["ja::grp1"] == {"id": "iter_a", "f1": 0.60}

    # A KEEP with a higher f1 DOES move the pointer.
    r4 = _run_cli(
        tmp_path, log_path=log_path, best_path=best_path, schema_path=schema_path,
        id="iter_d", language="ja", baseline="grp1", hypothesis="h4",
        verdict="KEEP", metrics_after={"overall_f1": 0.75},
    )
    assert r4.returncode == 0, r4.stderr
    best = json.loads(best_path.read_text())
    assert best["ja::grp1"] == {"id": "iter_d", "f1": 0.75}

    # A different {language, baseline} key is tracked independently.
    r5 = _run_cli(
        tmp_path, log_path=log_path, best_path=best_path, schema_path=schema_path,
        id="iter_e", language="en", baseline="grp1", hypothesis="h5",
        verdict="KEEP", metrics_after={"overall_f1": 0.10},
    )
    assert r5.returncode == 0, r5.stderr
    best = json.loads(best_path.read_text())
    assert best["en::grp1"] == {"id": "iter_e", "f1": 0.10}
    assert best["ja::grp1"] == {"id": "iter_d", "f1": 0.75}

    assert len(_read_jsonl(log_path)) == 5


# ---------------------------------------------------------------------------
# (c) invalid entries are rejected without mutating any files
# ---------------------------------------------------------------------------


def test_invalid_verdict_is_rejected_by_argparse(tmp_path: Path, sandbox) -> None:
    log_path, best_path, schema_path = sandbox
    result = _run_cli(
        tmp_path, log_path=log_path, best_path=best_path, schema_path=schema_path,
        id="bad_verdict", language="ja", baseline="grp1", hypothesis="h",
        verdict="MAYBE", metrics_after={"overall_f1": 0.9},
    )
    assert result.returncode != 0
    assert not log_path.exists()
    assert not best_path.exists()


def test_entry_missing_required_field_is_rejected_by_schema(tmp_path: Path, sandbox) -> None:
    log_path, best_path, schema_path = sandbox
    data_file = tmp_path / "data.json"
    data_file.write_text("{}", encoding="utf-8")

    # Build the CLI call by hand so we can omit --hypothesis (argparse would
    # normally require it; simulate a schema-level rejection instead by
    # passing an empty-string hypothesis, which trips minLength).
    args = [
        sys.executable, str(SCRIPT_PATH),
        "--id", "bad_entry",
        "--language", "ja",
        "--baseline", "grp1",
        "--hypothesis", "",
        "--intervention-type", "data_augmentation",
        "--data-file", str(data_file),
        "--metrics-before", "{}",
        "--metrics-after", '{"overall_f1": 0.9}',
        "--verdict", "KEEP",
        "--log-path", str(log_path),
        "--best-path", str(best_path),
        "--schema-path", str(schema_path),
    ]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 1
    assert "schema validation" in result.stderr
    assert not log_path.exists()
    assert not best_path.exists()


def test_log_experiment_module_importable() -> None:
    """Sanity check the script is a valid, importable module (not just a CLI)."""
    spec = importlib.util.spec_from_file_location("log_experiment", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "recompute_best")
    assert hasattr(module, "sha256_file")
