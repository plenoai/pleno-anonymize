"""Stream `git log -p` and scan added lines for PII.

We only scan **added** lines (`+` prefix in unified diff), since secrets/PII
that were present in the working tree are already caught by the dir scan.
The history pass exists to find PII that was ever committed and then removed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.regex_pass import CompiledPattern, scan_text

_COMMIT_RE = re.compile(
    r"^PLENOCOMMIT\x1f([0-9a-f]+)\x1f([^\x1f]*)\x1f([^\x1f]*)\x1f([^\x1f]*)$"
)
_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(slots=True)
class CommitMeta:
    sha: str
    author: str
    email: str
    date: str


def iter_history(
    repo: Path,
    *,
    max_commits: int | None = None,
) -> Iterator[tuple[CommitMeta, str, int, str]]:
    """Yield (commit, file, line_no, added_line) for every added line in history."""
    fmt = "PLENOCOMMIT\x1f%H\x1f%an\x1f%ae\x1f%aI"
    cmd = [
        "git",
        "-C",
        str(repo),
        "log",
        "--all",
        "-p",
        "--no-merges",
        "--no-color",
        f"--format={fmt}",
        "--unified=0",
    ]
    if max_commits is not None:
        cmd.insert(4, f"-n{max_commits}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )
    assert proc.stdout is not None

    commit: CommitMeta | None = None
    current_file: str | None = None
    new_line_no = 0

    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            cm = _COMMIT_RE.match(line)
            if cm:
                commit = CommitMeta(cm.group(1), cm.group(2), cm.group(3), cm.group(4))
                current_file = None
                continue
            if line.startswith("+++ "):
                fm = _FILE_RE.match(line)
                current_file = fm.group(1) if fm else None
                continue
            if line.startswith("@@"):
                hm = _HUNK_RE.match(line)
                new_line_no = int(hm.group(1)) if hm else 0
                continue
            if (
                commit is not None
                and current_file is not None
                and line.startswith("+")
                and not line.startswith("+++")
            ):
                yield commit, current_file, new_line_no, line[1:]
                new_line_no += 1
    finally:
        proc.stdout.close()
        proc.wait(timeout=5)


def scan_history(
    repo: Path,
    patterns: list[CompiledPattern],
    *,
    max_commits: int | None = None,
) -> tuple[list[Finding], int]:
    """Scan all added lines in git history. Returns (findings, commits_scanned)."""
    seen_commits: set[str] = set()
    findings: list[Finding] = []
    for commit, file, line_no, added in iter_history(repo, max_commits=max_commits):
        seen_commits.add(commit.sha)
        results = scan_text(added, file, patterns)
        for r in results:
            findings.append(
                Finding(
                    entity=r.entity,
                    file=file,
                    line=line_no,
                    col=r.col,
                    score=r.score,
                    snippet=added.rstrip(),
                    matched=r.matched,
                    pattern_name=r.pattern_name,
                    commit=commit.sha,
                    author=f"{commit.author} <{commit.email}>",
                    date=commit.date.split("T")[0],
                )
            )
    return findings, len(seen_commits)
