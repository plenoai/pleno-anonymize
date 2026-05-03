"""Filter findings via .plenoignore, baseline file, and inline comments.

.plenoignore syntax:
    # comment
    docs/samples/**            # path glob (gitignore-style)
    PHONE_NUMBER               # entity-wide ignore
    finding:7a3b8c9d           # specific finding fingerprint

Inline:
    contact = "090-1234-5678"  # pleno:ignore PHONE_NUMBER
    secret = "..."             # pleno:ignore
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pathspec

from pleno_scan.models import Finding

_INLINE_RE = re.compile(r"pleno:ignore(?:\s+([A-Z_,]+))?", re.IGNORECASE)


@dataclass
class IgnoreSet:
    path_spec: pathspec.PathSpec | None = None
    entities: set[str] = field(default_factory=set)
    fingerprints: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "IgnoreSet":
        if not path.exists():
            return cls()
        path_lines: list[str] = []
        entities: set[str] = set()
        fingerprints: set[str] = set()
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("finding:"):
                fingerprints.add(line.split(":", 1)[1].strip())
            elif line.isupper() and "/" not in line and "*" not in line:
                entities.add(line)
            else:
                path_lines.append(line)
        spec = (
            pathspec.PathSpec.from_lines("gitignore", path_lines)
            if path_lines
            else None
        )
        return cls(path_spec=spec, entities=entities, fingerprints=fingerprints)

    def matches(self, f: Finding) -> bool:
        if f.entity in self.entities:
            return True
        if f.fingerprint() in self.fingerprints:
            return True
        if self.path_spec is not None and self.path_spec.match_file(f.file):
            return True
        return False


def load_baseline(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {item.get("fingerprint", "") for item in data.get("findings", [])} - {""}


def write_baseline(path: Path, findings: list[Finding]) -> None:
    payload = {
        "version": 1,
        "findings": [
            {
                "fingerprint": f.fingerprint(),
                "entity": f.entity,
                "file": f.file,
                "line": f.line,
            }
            for f in findings
        ],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _inline_ignored_entities(line: str) -> set[str] | None:
    """Return set of entities suppressed on this line, or None if no directive.
    Empty set means "all entities" (`# pleno:ignore` with no args)."""
    m = _INLINE_RE.search(line)
    if not m:
        return None
    args = m.group(1)
    if not args:
        return set()  # suppress all
    return {e.strip() for e in args.split(",") if e.strip()}


def filter_findings(
    findings: list[Finding],
    *,
    ignore_set: IgnoreSet,
    baseline: set[str],
    file_lines: dict[str, list[str]] | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Split findings into (kept, suppressed)."""
    kept: list[Finding] = []
    suppressed: list[Finding] = []
    for f in findings:
        if ignore_set.matches(f):
            suppressed.append(f)
            continue
        if f.fingerprint() in baseline:
            suppressed.append(f)
            continue
        if file_lines is not None:
            lines = file_lines.get(f.file)
            if lines is not None and 0 <= f.line - 1 < len(lines):
                inline = _inline_ignored_entities(lines[f.line - 1])
                if inline is not None and (not inline or f.entity in inline):
                    suppressed.append(f)
                    continue
        kept.append(f)
    return kept, suppressed
