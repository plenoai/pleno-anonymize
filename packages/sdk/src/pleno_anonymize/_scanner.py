"""Filesystem scanner — walk paths, run :class:`Engine.analyze` per file."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ._engine import Engine, Finding

DEFAULT_MAX_BYTES = 256 * 1024

DEFAULT_IGNORE = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        ".next",
        ".turbo",
        ".cache",
    }
)

SCAN_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".json",
        ".jsonl",
        ".ndjson",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".env",
        ".cfg",
        ".conf",
        ".csv",
        ".tsv",
        ".log",
        ".html",
        ".htm",
        ".xml",
        ".svg",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".jsx",
        ".py",
        ".rb",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".swift",
        ".php",
        ".cs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".sql",
    }
)


@dataclass(slots=True)
class FileScanResult:
    path: str
    bytes: int
    language: str
    findings: list[Finding] = field(default_factory=list)
    truncated: bool = False
    skipped: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "language": self.language,
            "findings": [f.to_dict() for f in self.findings],
            "truncated": self.truncated,
            "skipped": self.skipped,
            "error": self.error,
        }


@dataclass(slots=True)
class ScanSummary:
    files: list[FileScanResult]
    total_findings: int
    by_entity: dict[str, int]
    scanned_files: int
    skipped_files: int

    def to_dict(self) -> dict[str, object]:
        return {
            "files": [f.to_dict() for f in self.files],
            "totalFindings": self.total_findings,
            "byEntity": self.by_entity,
            "scannedFiles": self.scanned_files,
            "skippedFiles": self.skipped_files,
        }


def scan_paths(
    engine: Engine,
    paths: Iterable[str | os.PathLike[str]],
    *,
    language: str = "ja",
    entities: Iterable[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    workers: int = 4,
    ignore: Iterable[str] | None = None,
    include_extensions: Iterable[str] | None = None,
    follow_symlinks: bool = False,
    on_file: Callable[[FileScanResult], None] | None = None,
) -> ScanSummary:
    ignore_set = set(DEFAULT_IGNORE)
    if ignore:
        ignore_set.update(ignore)
    if include_extensions:
        allow = frozenset(
            (e if e.startswith(".") else f".{e}").lower() for e in include_extensions
        )
    else:
        allow = SCAN_EXTENSIONS

    targets: list[Path] = []
    for raw in paths:
        targets.extend(_collect(Path(raw), ignore_set, allow, follow_symlinks))

    cwd = Path.cwd()
    ent_list = list(entities) if entities is not None else None

    def _worker(file: Path) -> FileScanResult:
        return _scan_one(engine, file, cwd, language, ent_list, max_bytes)

    results: list[FileScanResult] = []
    if not targets:
        return ScanSummary(
            files=[], total_findings=0, by_entity={}, scanned_files=0, skipped_files=0
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(_worker, targets):
            results.append(result)
            if on_file is not None:
                on_file(result)

    results.sort(key=lambda r: r.path)
    by_entity: dict[str, int] = {}
    total_findings = 0
    scanned = 0
    skipped = 0
    for r in results:
        if r.skipped:
            skipped += 1
            continue
        scanned += 1
        for f in r.findings:
            total_findings += 1
            by_entity[f.entity_type] = by_entity.get(f.entity_type, 0) + 1
    return ScanSummary(
        files=results,
        total_findings=total_findings,
        by_entity=by_entity,
        scanned_files=scanned,
        skipped_files=skipped,
    )


def scan_file(
    engine: Engine,
    path: str | os.PathLike[str],
    *,
    language: str = "ja",
    entities: Iterable[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FileScanResult:
    return _scan_one(
        engine,
        Path(path).resolve(),
        Path.cwd(),
        language,
        list(entities) if entities is not None else None,
        max_bytes,
    )


# ----- internal --------------------------------------------------------------


def _collect(
    root: Path,
    ignore: set[str],
    allow: frozenset[str],
    follow_symlinks: bool,
) -> list[Path]:
    out: list[Path] = []
    try:
        root = root.resolve()
    except OSError:
        return out
    if not root.exists():
        return out
    if root.is_file():
        if _ext_match(root, allow):
            out.append(root)
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_symlinks):
        # in-place prune
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            full = Path(dirpath) / name
            if _ext_match(full, allow):
                out.append(full)
    return out


def _ext_match(path: Path, allow: frozenset[str]) -> bool:
    # suffix is "" for dotfiles (e.g. .env); fall back to name for those
    ext = path.suffix.lower() or path.name.lower()
    return ext in allow


def _scan_one(
    engine: Engine,
    abs_path: Path,
    cwd: Path,
    language: str,
    entities: list[str] | None,
    max_bytes: int,
) -> FileScanResult:
    try:
        display = str(abs_path.relative_to(cwd))
    except ValueError:
        display = str(abs_path)
    try:
        size = abs_path.stat().st_size
    except OSError as e:
        return FileScanResult(
            path=display,
            bytes=0,
            language=language,
            skipped="read-error",
            error=str(e),
        )
    try:
        with abs_path.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError as e:
        return FileScanResult(
            path=display,
            bytes=size,
            language=language,
            skipped="read-error",
            error=str(e),
        )
    truncated = len(raw) > max_bytes
    chunk = raw[:max_bytes] if truncated else raw
    if b"\x00" in chunk[:8000]:
        return FileScanResult(
            path=display,
            bytes=size,
            language=language,
            truncated=truncated,
            skipped="binary",
        )
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        if truncated:
            # Truncation may have split a multibyte sequence (UTF-8 chars are ≤4 bytes).
            # Strip up to 3 trailing bytes and retry before declaring binary.
            for _trim in range(1, 4):
                try:
                    text = chunk[:-_trim].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    continue
            else:
                return FileScanResult(
                    path=display,
                    bytes=size,
                    language=language,
                    truncated=truncated,
                    skipped="binary",
                )
        else:
            return FileScanResult(
                path=display,
                bytes=size,
                language=language,
                truncated=truncated,
                skipped="binary",
            )
    if not text.strip():
        return FileScanResult(
            path=display,
            bytes=size,
            language=language,
            truncated=truncated,
        )
    try:
        findings = engine.analyze(text, language=language, entities=entities)
    except Exception as e:  # noqa: BLE001 - report any engine failure per-file
        return FileScanResult(
            path=display,
            bytes=size,
            language=language,
            truncated=truncated,
            skipped="read-error",
            error=str(e),
        )
    return FileScanResult(
        path=display,
        bytes=size,
        language=language,
        findings=list(findings),
        truncated=truncated,
    )
