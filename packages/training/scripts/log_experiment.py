"""Append one experiment entry to experiments/log.jsonl and refresh best.json.

#293: agents following the /ner-improve skill used to hand-write JSON lines
directly into log.jsonl, which is how the log ended up with 4+ incompatible
shapes and no machine-readable "previous best" pointer. This script is now
the *only* supported way to add a row:

1. Build the entry from CLI args (one flag per experiments/log_schema.json
   field).
2. Hash the input data file with sha256 -> `data_hash`. This is mandatory:
   iter08b (log.jsonl id 20260402_iter08b_aug_only_no_new_llm) silently
   trained on 28k docs instead of 33k after an unstaged-file git revert,
   which invalidated its baseline comparison and was only caught by luck.
   A recorded content hash makes that class of bug detectable after the
   fact.
3. Validate the entry against experiments/log_schema.json *before* touching
   log.jsonl. On failure: print the error, exit 1, log.jsonl is untouched.
4. Append the entry as one line (existing lines are never rewritten).
5. Recompute experiments/best.json from the *entire* log (not just the new
   row): for every {language}::{baseline} group, the KEEP run with the
   highest metrics_after.overall_f1 wins. This is what lets the next
   iteration look up "the run to beat" in one file read instead of
   re-parsing 28+ lines of prose.

Usage:
    uv run python scripts/log_experiment.py \\
        --id 20260706_iter17_example \\
        --language ja \\
        --baseline ja_frozen_benchmark_v0.4.0 \\
        --hypothesis "Add X augmentation" \\
        --intervention-type data_augmentation \\
        --data-file data/raw/ja-v02/augmented.json \\
        --metrics-before '{"overall_f1": 0.85}' \\
        --metrics-after '{"overall_f1": 0.87}' \\
        --verdict KEEP \\
        --reason "overall F1 +2pt, no regressions"

--metrics-before/--metrics-after accept either an inline JSON object or
`@path/to/file.json` to read the object from a file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jsonschema

_SCRIPT_DIR = Path(__file__).resolve().parent
_TRAINING_ROOT = _SCRIPT_DIR.parent
DEFAULT_LOG_PATH = _TRAINING_ROOT / "experiments" / "log.jsonl"
DEFAULT_BEST_PATH = _TRAINING_ROOT / "experiments" / "best.json"
DEFAULT_SCHEMA_PATH = _TRAINING_ROOT / "experiments" / "log_schema.json"

JST = timezone(timedelta(hours=9))

VERDICTS = ("KEEP", "DISCARD", "NO_DECISION")


def sha256_file(path: Path) -> str:
    """content hash of the data file this run trained/evaluated on (#293)."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json_arg(value: str, flag_name: str) -> dict[str, Any]:
    """Parse a CLI value as inline JSON, or read it from `@path` if prefixed."""
    if value.startswith("@"):
        path = Path(value[1:])
        text = path.read_text(encoding="utf-8")
    else:
        text = value
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"{flag_name} must decode to a JSON object, got {type(parsed).__name__}")
    return parsed


def load_schema(schema_path: Path) -> dict[str, Any]:
    return json.loads(schema_path.read_text(encoding="utf-8"))


def build_entry(args: argparse.Namespace, data_hash: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": args.id,
        "timestamp": args.timestamp,
        "language": args.language,
        "baseline": args.baseline,
        "hypothesis": args.hypothesis,
        "intervention_type": args.intervention_type,
        "metrics_before": _load_json_arg(args.metrics_before, "--metrics-before"),
        "metrics_after": _load_json_arg(args.metrics_after, "--metrics-after"),
        "verdict": args.verdict,
        "data_hash": data_hash,
    }
    if args.reason is not None:
        entry["reason"] = args.reason
    if args.duration_minutes is not None:
        entry["duration_minutes"] = args.duration_minutes
    return entry


def validate_entry(entry: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Return a list of human-readable validation errors (empty = valid)."""
    validator = jsonschema.Draft7Validator(schema)
    return [str(e.message) for e in sorted(validator.iter_errors(entry), key=lambda e: e.path)]


def append_entry(entry: dict[str, Any], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def _iter_log_rows(log_path: Path):
    if not log_path.exists():
        return
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        yield json.loads(line)


def recompute_best(log_path: Path) -> dict[str, dict[str, Any]]:
    """{language}::{baseline} -> {"id": ..., "f1": ...} for the best KEEP run.

    "Best" = highest metrics_after.overall_f1 among rows with verdict=="KEEP"
    that actually recorded a numeric overall_f1. Rows without a comparable
    overall_f1 (e.g. per-entity-only baseline_comparison rows) are skipped
    rather than guessed at.
    """
    best: dict[str, dict[str, Any]] = {}
    for row in _iter_log_rows(log_path):
        if row.get("verdict") != "KEEP":
            continue
        f1 = row.get("metrics_after", {}).get("overall_f1")
        if not isinstance(f1, (int, float)):
            continue
        key = f"{row.get('language')}::{row.get('baseline')}"
        current = best.get(key)
        if current is None or f1 > current["f1"]:
            best[key] = {"id": row["id"], "f1": f1}
    return best


def write_best(best: dict[str, dict[str, Any]], best_path: Path) -> None:
    best_path.parent.mkdir(parents=True, exist_ok=True)
    best_path.write_text(
        json.dumps(best, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--id", required=True)
    parser.add_argument("--language", required=True)
    parser.add_argument("--baseline", required=True, help="{language, baseline} is the best.json grouping key")
    parser.add_argument("--hypothesis", required=True)
    parser.add_argument("--intervention-type", required=True, dest="intervention_type")
    parser.add_argument("--data-file", required=True, dest="data_file", type=Path, help="input data file to sha256-hash into data_hash")
    parser.add_argument("--metrics-before", required=True, dest="metrics_before", help="JSON object, or @path/to/file.json")
    parser.add_argument("--metrics-after", required=True, dest="metrics_after", help="JSON object, or @path/to/file.json")
    parser.add_argument("--verdict", required=True, choices=VERDICTS)
    parser.add_argument("--reason", default=None)
    parser.add_argument("--duration-minutes", dest="duration_minutes", type=float, default=None)
    parser.add_argument("--timestamp", default=None, help="ISO 8601; defaults to now in JST")
    parser.add_argument("--log-path", dest="log_path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--best-path", dest="best_path", type=Path, default=DEFAULT_BEST_PATH)
    parser.add_argument("--schema-path", dest="schema_path", type=Path, default=DEFAULT_SCHEMA_PATH)
    args = parser.parse_args(argv)
    if args.timestamp is None:
        args.timestamp = datetime.now(JST).isoformat()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.data_file.exists():
        print(f"error: --data-file not found: {args.data_file}", file=sys.stderr)
        return 1

    data_hash = sha256_file(args.data_file)

    try:
        entry = build_entry(args, data_hash)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"error: could not build entry: {exc}", file=sys.stderr)
        return 1

    schema = load_schema(args.schema_path)
    errors = validate_entry(entry, schema)
    if errors:
        print("error: entry failed schema validation; log.jsonl was NOT modified:", file=sys.stderr)
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        return 1

    append_entry(entry, args.log_path)
    best = recompute_best(args.log_path)
    write_best(best, args.best_path)

    key = f"{entry['language']}::{entry['baseline']}"
    print(f"appended {entry['id']} to {args.log_path}")
    if key in best:
        print(f"best.json[{key!r}] = {best[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
