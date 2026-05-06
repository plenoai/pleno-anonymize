"""Output formatters: human, json, sarif."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import TextIO

from pleno_pii_scanner import __version__
from pleno_pii_scanner.models import Finding, ScanStats


_VERIFICATION_BADGE = {
    "passed": "\033[32m✓ verified\033[0m",
    "failed": "\033[31m✗ checksum failed\033[0m",
    "unverified": "\033[33m? unverified\033[0m",
}


def _badge(v: str, color: bool) -> str:
    if not color:
        return {
            "passed": "verified",
            "failed": "checksum failed",
            "unverified": "unverified",
        }[v]
    return _VERIFICATION_BADGE[v]


def render_human(
    stats: ScanStats, out: TextIO = sys.stdout, *, color: bool = True
) -> None:
    findings = stats.findings
    if not findings:
        out.write(
            f"pleno-pii-scanner v{__version__}: scanned {stats.files_scanned} files "
            f"({stats.bytes_scanned:,} bytes) in {stats.duration_ms} ms — "
            f"\033[32mno findings\033[0m\n"
            if color
            else f"pleno-pii-scanner v{__version__}: scanned {stats.files_scanned} files in {stats.duration_ms} ms — no findings\n"
        )
        return

    out.write(f"pleno-pii-scanner v{__version__}\n")
    out.write(f"Scanned {stats.files_scanned} files ({stats.bytes_scanned:,} bytes)")
    if stats.commits_scanned:
        out.write(f", {stats.commits_scanned} commits")
    out.write(f" in {stats.duration_ms} ms\n\n")

    for f in findings:
        loc = f"{f.file}:{f.line}:{f.col}"
        if color:
            out.write(f"\033[1m\033[35m{f.entity}\033[0m  \033[36m{loc}\033[0m  ")
        else:
            out.write(f"{f.entity}  {loc}  ")
        out.write(f"score={f.score:.2f}  {_badge(f.verification, color)}\n")
        if f.commit:
            out.write(
                f"  commit {f.commit[:8]} by {f.author or '?'}"
                + (f" on {f.date}" if f.date else "")
                + "\n"
            )
        out.write(f"  {f.snippet}\n\n")

    n = len(findings)
    verified = sum(1 for f in findings if f.verification == "passed")
    out.write(
        f"{n} finding{'s' if n != 1 else ''} ({verified} verified) "
        f"in {stats.duration_ms} ms\n"
    )


def render_json(stats: ScanStats, out: TextIO = sys.stdout) -> None:
    payload = {
        "version": __version__,
        "stats": {
            "files_scanned": stats.files_scanned,
            "files_skipped_binary": stats.files_skipped_binary,
            "files_skipped_size": stats.files_skipped_size,
            "bytes_scanned": stats.bytes_scanned,
            "duration_ms": stats.duration_ms,
            "commits_scanned": stats.commits_scanned,
        },
        "findings": [
            {**asdict(f), "fingerprint": f.fingerprint()} for f in stats.findings
        ],
    }
    json.dump(payload, out, ensure_ascii=False, indent=2)
    out.write("\n")


def render_sarif(stats: ScanStats, out: TextIO = sys.stdout) -> None:
    """Emit SARIF 2.1.0 for GitHub Code Scanning."""
    rules: dict[str, dict] = {}
    results = []
    for f in stats.findings:
        rule_id = f.entity
        if rule_id not in rules:
            rules[rule_id] = {
                "id": rule_id,
                "name": rule_id,
                "shortDescription": {"text": f"PII: {rule_id}"},
                "fullDescription": {
                    "text": f"Detected {rule_id} via pattern matching."
                },
                "defaultConfiguration": {"level": "warning"},
            }
        results.append(
            {
                "ruleId": rule_id,
                "level": _sarif_level(f),
                "message": {
                    "text": f"{rule_id} (verification: {f.verification}, score: {f.score:.2f})"
                },
                "partialFingerprints": {"plenoFingerprint/v1": f.fingerprint()},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.file},
                            "region": {
                                "startLine": f.line,
                                "startColumn": f.col,
                                "snippet": {"text": f.matched},
                            },
                        }
                    }
                ],
            }
        )

    sarif = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "pleno-pii-scanner",
                        "informationUri": "https://github.com/plenoai/pleno-anonymize",
                        "version": __version__,
                        "rules": list(rules.values()),
                    }
                },
                "results": results,
            }
        ],
    }
    json.dump(sarif, out, ensure_ascii=False, indent=2)
    out.write("\n")


def _sarif_level(f: Finding) -> str:
    if f.verification == "passed":
        return "error"
    if f.verification == "failed":
        return "note"
    return "warning"
