"""File walker that respects .gitignore and skips noisy paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pathspec

# Always-skip directory names. Matched against any path component.
_NOISE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
        "target",
        "vendor",
        ".next",
        ".turbo",
        ".cache",
        ".idea",
        ".vscode",
        ".gradle",
    }
)

_BINARY_PROBE_BYTES = 4096


def _load_gitignore(root: Path) -> pathspec.PathSpec | None:
    gi = root / ".gitignore"
    if not gi.exists():
        return None
    try:
        with gi.open("r", encoding="utf-8", errors="replace") as f:
            return pathspec.PathSpec.from_lines("gitignore", f)
    except OSError:
        return None


def is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(_BINARY_PROBE_BYTES)
    except OSError:
        return True
    return b"\0" in chunk


def walk(
    root: Path,
    *,
    max_file_size: int = 1024 * 1024,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    respect_gitignore: bool = True,
) -> Iterator[Path]:
    """Yield candidate files under root for scanning."""
    root = root.resolve()
    spec = _load_gitignore(root) if respect_gitignore else None
    include_spec = (
        pathspec.PathSpec.from_lines("gitignore", include) if include else None
    )
    exclude_spec = (
        pathspec.PathSpec.from_lines("gitignore", exclude) if exclude else None
    )

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Filter directories in-place to prune subtrees.
        dirnames[:] = [d for d in dirnames if d not in _NOISE_DIRS]

        for fn in filenames:
            full = Path(dirpath) / fn
            try:
                rel = full.relative_to(root)
            except ValueError:
                continue
            rel_str = rel.as_posix()

            if spec is not None and spec.match_file(rel_str):
                continue
            if exclude_spec is not None and exclude_spec.match_file(rel_str):
                continue
            if include_spec is not None and not include_spec.match_file(rel_str):
                continue

            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size == 0 or size > max_file_size:
                continue
            if is_binary(full):
                continue

            yield full
