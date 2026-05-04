"""Builtin `git` connector — local repo history wrapped as SourceConnector.

Adapts the existing `pleno_pii_scanner.git_history.iter_history` helper
(streaming `git log -p --unified=0`) to the SourceConnector protocol.

Design notes:

  * **One DocumentRef per (commit, file).** `iter_history` yields per
    line, but findings are reported with file + line attribution, so the
    natural unit of work for the scheduler is the changed-file slice of
    one commit. Rebatching to (commit, file) keeps fetch granular enough
    for the FindingsStore to attribute commits accurately while not
    flooding the scheduler with one ref per line.
  * **Added-only.** Only `+` lines from the diff are scanned. Working-
    tree contents are the dir scanner's job. The split was load-bearing
    for the legacy CLI and is preserved here for snapshot parity.
  * **Line numbers preserved.** Document.text is built so that
    `text.splitlines()[n - 1]` is the diff's added line at new-file line
    `n`. We pad with empty lines for gaps so existing regex_pass output
    stays byte-identical.
  * **Subprocess-backed → thread-hopped.** `iter_history` blocks reading
    from the `git log` pipe; we run discovery in `asyncio.to_thread` so
    the event loop stays free for parallel connectors.

The enterprise GitHub App / GHES / org-enum connector lives separately
in the `pleno-pii-scanner-github` wheel (Task #17). This builtin keeps
the pre-multi-source CLI behavior available with zero new dependencies.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pleno_pii_scanner.git_history import CommitMeta, iter_history
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    Principal,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


@dataclass(frozen=True, slots=True)
class GitConfig:
    """Construction config for `GitConnector`.

    `id` defaults to `git:<resolved-repo-path>` so two scans of the same
    repo share a checkpoint. `max_commits` mirrors the legacy CLI flag
    so operators can cap traversal on monorepos.
    """

    repo: Path
    id: str | None = None
    max_commits: int | None = None

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        return f"git:{self.repo.resolve().as_posix()}"


@dataclass(slots=True)
class _CommitFile:
    """Pre-aggregated (commit, file) slice of `iter_history` output.

    Line content is stored sparse (line_no → text) so we can rebuild a
    text body that places each added line at its original line number
    without materialising blank padding for huge gaps in memory.
    """

    commit: CommitMeta
    file: str
    lines: dict[int, str]


class GitConnector:
    """Local git-history SourceConnector.

    Streams `git log -p --unified=0` once per `discover()` call, groups
    by (commit, file), and yields one DocumentRef per slice. `fetch()`
    rehydrates the text body from the in-memory cache the discover pass
    already populated — re-running `git log` per fetch would dominate
    runtime on big histories.
    """

    kind = "git"

    def __init__(self, config: GitConfig) -> None:
        self._config = config
        self.id = config.resolved_id()
        # _slices maps DocumentRef.fingerprint() → _CommitFile so fetch
        # can find what discover yielded. Bounded by the size of the
        # repo's history; for monorepos the operator caps via max_commits.
        self._slices: dict[str, _CommitFile] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        # WHY: iter_history is sync (subprocess pipe). Materialising on a
        # worker thread keeps the event loop free for parallel connectors.
        # Cursor unused: we report incremental=False because resuming
        # mid-`git log` requires shelling out with --since=<sha>, which the
        # enterprise GitHub connector handles with the SaaS API instead.
        del cursor
        slices = await asyncio.to_thread(
            _aggregate, self._config.repo, self._config.max_commits
        )
        for slice_ in slices:
            ref = self._build_ref(slice_)
            self._slices[ref.fingerprint()] = slice_
            if not _ref_passes_filter(ref, filter):
                continue
            yield ref

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        slice_ = self._slices.get(ref.fingerprint())
        if slice_ is None:
            # WHY: a fetch for a ref we never produced means the caller
            # mixed scheduler state across processes. Returning an empty
            # async iterator (no yields) is the SourceConnector idiom for
            # "this ref produced nothing"; raising would crash the
            # scheduler's gather() and abort sibling plans.
            return
        text = _build_body(slice_)
        principal = Principal(
            id=slice_.commit.email,
            display_name=slice_.commit.author,
            email=slice_.commit.email,
        )
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            content_hash=slice_.commit.sha,
            created_by=principal,
            extra={
                "commit_sha": slice_.commit.sha,
                "commit_date": slice_.commit.date,
            },
        )

    async def close(self) -> None:
        # WHY: drop the per-(commit, file) cache so a long-lived
        # registry reusing one connector instance across scans does not
        # hold onto the previous repo's history.
        self._slices.clear()

    def _build_ref(self, slice_: _CommitFile) -> DocumentRef:
        # path is "<file>@<short-sha>" so two refs for the same file in
        # different commits do not collide on the FindingsStore dedup
        # key (which is sha256(source_id, source_kind, path) + etag).
        short = slice_.commit.sha[:12]
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"{slice_.file}@{short}",
            content_type="text/plain",
            etag=slice_.commit.sha,
            last_modified=_parse_iso(slice_.commit.date),
            metadata={
                "commit_sha": slice_.commit.sha,
                "commit_author": slice_.commit.author,
                "commit_email": slice_.commit.email,
                "file": slice_.file,
            },
        )


def _aggregate(repo: Path, max_commits: int | None) -> list[_CommitFile]:
    """Drain `iter_history` into one `_CommitFile` per (commit, file)."""
    slices: dict[tuple[str, str], _CommitFile] = {}
    for commit, file, line_no, added in iter_history(repo, max_commits=max_commits):
        key = (commit.sha, file)
        s = slices.get(key)
        if s is None:
            s = _CommitFile(commit=commit, file=file, lines={})
            slices[key] = s
        s.lines[line_no] = added
    # Stable order: commits in `git log` order, file path within commit.
    # `iter_history` already streams in that order, so insertion order
    # is correct. Python 3.7+ dict preserves insertion order.
    return list(slices.values())


def _build_body(slice_: _CommitFile) -> str:
    """Reconstruct an aligned text body from sparse (line_no → text) lines.

    The output is `slice_.lines[n - 1]` at each n, with empty strings
    filling gaps. The detector pass uses `splitlines()` indexing, so
    aligned padding preserves the line_no attribution that the legacy
    `scan_history` produced.
    """
    if not slice_.lines:
        return ""
    max_line = max(slice_.lines)
    out = [slice_.lines.get(n, "") for n in range(1, max_line + 1)]
    return "\n".join(out)


def _ref_passes_filter(ref: DocumentRef, filter: SourceFilter) -> bool:
    """Apply SourceFilter to a git slice ref.

    Only `since` and `max_size` are meaningful for git history;
    include/exclude work against the file path component (before `@sha`).
    """
    if filter.since is not None and ref.last_modified is not None:
        if ref.last_modified < filter.since:
            return False
    if filter.max_size is not None and ref.size is not None:
        if ref.size > filter.max_size:
            return False
    file = ref.metadata.get("file", ref.path)
    if filter.include and not _matches_any(file, filter.include):
        return False
    if filter.exclude and _matches_any(file, filter.exclude):
        return False
    return True


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(path, pat) for pat in patterns)


def _parse_iso(value: str) -> datetime | None:
    # iter_history emits %aI which is strict ISO-8601 with offset, but
    # operators occasionally feed us repos with corrupted commit dates
    # (e.g. import from svn). Returning None on parse failure means the
    # ref still scans; we just lose `last_modified` attribution.
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "repo" not in config:
        raise ValueError("git connector config requires 'repo'")
    repo_raw = config["repo"]
    repo = repo_raw if isinstance(repo_raw, Path) else Path(str(repo_raw))
    max_commits_raw = config.get("max_commits")
    max_commits = int(max_commits_raw) if max_commits_raw is not None else None
    return GitConnector(
        GitConfig(
            repo=repo,
            id=str(config["id"]) if config.get("id") is not None else None,
            max_commits=max_commits,
        )
    )


SPEC = ConnectorSpec(
    kind="git",
    version="1.0.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        max_concurrent_fetches=4,
    ),
    required_scopes=(),
    description=(
        "Local git repository history scanner. Walks `git log -p --all "
        "--no-merges` and emits one document per (commit, file) slice "
        "for added lines. Uses the system git binary; no library deps."
    ),
)


__all__ = ["SPEC", "GitConfig", "GitConnector"]
