"""Parallel regex scan over files.

`scan_files` runs the precompiled recognizer set against every file using
a process pool. Each worker compiles its own regex set once at init time
and reuses it across files in its shard.
"""

from __future__ import annotations

import bisect
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pleno_recognizers.types import PiiRecognizer

from pleno_pii_scanner.models import Finding


@dataclass(frozen=True, slots=True)
class CompiledPattern:
    entity: str
    pattern_name: str
    regex: re.Pattern
    score: float


def compile_patterns(recognizers: Iterable[PiiRecognizer]) -> list[CompiledPattern]:
    out: list[CompiledPattern] = []
    for r in recognizers:
        for p in r.patterns:
            out.append(
                CompiledPattern(
                    entity=r.entity,
                    pattern_name=p.name,
                    regex=re.compile(p.regex, re.MULTILINE),
                    score=p.score,
                )
            )
    return out


# Module-level state for worker processes (initialized in pool initializer).
_WORKER_PATTERNS: list[CompiledPattern] = []


def _worker_init(pattern_specs: list[tuple[str, str, str, float]]) -> None:
    global _WORKER_PATTERNS
    _WORKER_PATTERNS = [
        CompiledPattern(entity, name, re.compile(rgx, re.MULTILINE), score)
        for entity, name, rgx, score in pattern_specs
    ]


def _line_offsets(text: str) -> list[int]:
    """Return sorted list of newline offsets for fast line lookup."""
    offsets = [0]
    pos = 0
    while True:
        idx = text.find("\n", pos)
        if idx == -1:
            break
        offsets.append(idx + 1)
        pos = idx + 1
    return offsets


def _line_col(line_starts: list[int], offset: int) -> tuple[int, int]:
    """Return 1-indexed (line, col) for a byte offset."""
    line_idx = bisect.bisect_right(line_starts, offset) - 1
    return line_idx + 1, offset - line_starts[line_idx] + 1


def _scan_text(text: str, file: str, patterns: list[CompiledPattern]) -> list[Finding]:
    if not text:
        return []
    line_starts = _line_offsets(text)
    findings: list[Finding] = []
    for cp in patterns:
        for m in cp.regex.finditer(text):
            start = m.start()
            line, col = _line_col(line_starts, start)
            line_end_idx = bisect.bisect_right(line_starts, start)
            line_end = (
                line_starts[line_end_idx]
                if line_end_idx < len(line_starts)
                else len(text)
            )
            snippet = text[line_starts[line - 1] : line_end].rstrip("\n")
            if len(snippet) > 240:
                # Center the snippet on the match.
                rel = start - line_starts[line - 1]
                snippet = snippet[max(0, rel - 80) : rel + 160]
            findings.append(
                Finding(
                    entity=cp.entity,
                    file=file,
                    line=line,
                    col=col,
                    score=cp.score,
                    snippet=snippet,
                    matched=m.group(0),
                    pattern_name=cp.pattern_name,
                )
            )
    return findings


def _scan_file_worker(args: tuple[str, str]) -> list[Finding]:
    rel_path, abs_path = args
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return _scan_text(text, rel_path, _WORKER_PATTERNS)


def scan_files(
    files: list[tuple[Path, Path]],
    patterns: list[CompiledPattern],
    *,
    workers: int | None = None,
) -> list[Finding]:
    """Scan many files in parallel.

    `files` is a list of (relative_path, absolute_path) tuples. The relative
    path is what we report to users; the absolute path is what we read.
    """
    if not files:
        return []

    pattern_specs = [
        (cp.entity, cp.pattern_name, cp.regex.pattern, cp.score) for cp in patterns
    ]

    # Inline path for tiny scans avoids pool overhead.
    if workers == 1 or len(files) < 8:
        _worker_init(pattern_specs)
        out: list[Finding] = []
        for rel, absolute in files:
            out.extend(_scan_file_worker((rel.as_posix(), str(absolute))))
        return out

    args = [(rel.as_posix(), str(absolute)) for rel, absolute in files]
    findings: list[Finding] = []
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_worker_init,
        initargs=(pattern_specs,),
    ) as pool:
        futures = [pool.submit(_scan_file_worker, a) for a in args]
        for fut in as_completed(futures):
            findings.extend(fut.result())
    return findings


def scan_text(
    text: str,
    label: str,
    patterns: list[CompiledPattern],
) -> list[Finding]:
    """Scan a single in-memory string. Used for git-history hunks and pre-commit."""
    return _scan_text(text, label, patterns)
