"""Builtin `dir` connector — local filesystem walk wrapped as SourceConnector.

Adapts the existing `pleno_pii_scanner.walker.walk` helper (gitignore-aware,
binary-skip, size-cap) to the SourceConnector protocol so the scheduler
can drive it through the same interface as remote connectors.

The pre-multi-source CLI subcommand `pleno-pii-scanner dir <path>` keeps
its public behavior — the same files come out and the same Findings are
emitted. The CLI now constructs `DirConnector(DirConfig(root=...))` and
hands it to the scheduler instead of calling `walker.walk` directly.
ADR-0007 §7.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec
from pleno_pii_scanner.walker import walk


@dataclass(frozen=True, slots=True)
class DirConfig:
    """Construction config for `DirConnector`.

    `id` defaults to `dir:<resolved-path>` so the same root scanned twice
    in one session shares a checkpoint. Operators can override when they
    want to distinguish e.g. snapshot-A vs snapshot-B of the same path.
    """

    root: Path
    id: str | None = None
    max_file_size: int = 1024 * 1024
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    respect_gitignore: bool = True

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        return f"dir:{self.root.resolve().as_posix()}"


class DirConnector:
    """Local filesystem SourceConnector.

    `discover()` runs the synchronous walker in a worker thread so the
    asyncio event loop stays responsive when scanning large trees.
    `fetch()` reads file bytes the same way — `read_bytes` blocks long
    enough on multi-MB files to deserve a thread hop. The Document text
    decoding (UTF-8 with `errors='replace'`) matches the legacy
    `_scan_directory` behavior so existing snapshot tests stay byte-
    identical.
    """

    kind = "dir"

    def __init__(self, config: DirConfig) -> None:
        self._config = config
        self.id = config.resolved_id()
        # Resolve root once; downstream `fetch()` calls reuse it for the
        # `relative_to(root)` arithmetic that produces logical paths.
        self._root = config.root.resolve()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=8,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        # WHY: `walker.walk` is a generator over `os.walk` — synchronous
        # I/O. Materializing in a worker thread keeps the event loop free
        # for parallel `fetch()` from other connectors. Cursor is unused
        # because this connector reports `incremental=False`; resume
        # support would need stat()-based since-filtering, which we leave
        # to the GitHub/S3 connectors that have native `since` semantics.
        del cursor
        max_size = (
            min(filter.max_size, self._config.max_file_size)
            if filter.max_size is not None
            else self._config.max_file_size
        )
        include = (
            list(filter.include) if filter.include else list(self._config.include)
        )
        exclude = (
            list(filter.exclude) if filter.exclude else list(self._config.exclude)
        )

        files = await asyncio.to_thread(
            _collect_walk,
            self._root,
            max_size,
            include or None,
            exclude or None,
            self._config.respect_gitignore,
        )

        for full in files:
            # walker.walk yields paths under root; relative_to is safe.
            rel = full.relative_to(self._root)
            size = await asyncio.to_thread(_safe_size, full)
            # File can disappear between walk() enumeration and stat()
            # (race with another writer); skip and let the next scan
            # pick it up if it returns.
            if size is None:
                continue
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=rel.as_posix(),
                native_url=full.as_uri(),
                content_type="text/plain",
                size=size,
                last_modified=await asyncio.to_thread(_safe_mtime, full),
            )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        # WHY: re-resolving from `self._root + ref.path` rather than trusting
        # an absolute path stored on the ref defends against a malicious
        # registry handing us a ref pointing outside the configured root.
        # symlink-out-of-tree is rejected by the relative_to check below.
        full = (self._root / ref.path).resolve()
        try:
            full.relative_to(self._root)
        except ValueError as exc:
            raise PermissionError(
                f"refusing to fetch path outside configured root: {full}"
            ) from exc
        text = await asyncio.to_thread(_read_text, full)
        if text is None:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
        )

    async def close(self) -> None:
        # WHY: filesystem walker holds no persistent handles beyond the
        # short-lived per-fetch reads. close() is here for protocol
        # symmetry; the scheduler may call it multiple times.
        return None


def _collect_walk(
    root: Path,
    max_file_size: int,
    include: list[str] | None,
    exclude: list[str] | None,
    respect_gitignore: bool,
) -> list[Path]:
    return list(
        walk(
            root,
            max_file_size=max_file_size,
            include=include,
            exclude=exclude,
            respect_gitignore=respect_gitignore,
        )
    )


def _safe_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _safe_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    # WHY: the registry passes a plain dict from TOML/YAML config files
    # (CLI #15). Conversion to a typed DirConfig happens here so the
    # protocol stays Mapping-based but each connector enforces its own
    # config shape with clear errors.
    if "root" not in config:
        raise ValueError("dir connector config requires 'root'")
    root_raw = config["root"]
    root = root_raw if isinstance(root_raw, Path) else Path(str(root_raw))
    return DirConnector(
        DirConfig(
            root=root,
            id=str(config["id"]) if config.get("id") is not None else None,
            max_file_size=int(config.get("max_file_size", 1024 * 1024)),
            include=tuple(config.get("include", ())),
            exclude=tuple(config.get("exclude", ())),
            respect_gitignore=bool(config.get("respect_gitignore", True)),
        )
    )


SPEC = ConnectorSpec(
    kind="dir",
    version="1.0.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        max_concurrent_fetches=8,
    ),
    required_scopes=(),
    description=(
        "Local filesystem walker. Honors .gitignore plus a built-in skip "
        "list (.git, node_modules, .venv, dist, build, vendor, ...) and "
        "skips binary files via NUL-byte probe."
    ),
)


__all__ = ["SPEC", "DirConfig", "DirConnector"]
