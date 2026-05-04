"""Builtin `github` connector — shallow-clone-and-walk wrapper.

This is the **zero-dependency** GitHub connector that ships with the
core wheel: it uses `git clone --depth=1` plus the `gh` CLI for org
enumeration, the same path the legacy `pleno-pii-scanner github`
subcommand used. Operators who want GitHub App auth, GHES, fine-grained
PAT scopes, or PyGithub-based pagination install the separate
`pleno-pii-scanner-github` wheel (Task #17 / ADR §13).

The connector accepts either:

  * a single `owner/repo` slug (or any URL git understands)
  * a whole org name (with `is_org=True`)

For an org, `discover()` enumerates `owner/repo` slugs via `gh repo
list` and clones each repo lazily as the scheduler asks for refs.
Cloned repos are reused across fetches in the same run and removed in
`close()` — so a tenant scanning 1000 repos pays disk for one repo at
a time, not all 1000 simultaneously.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pleno_pii_scanner.github import list_org_repos


# Test seam: factory that turns (slug, config) into a local cloned path.
# Production default is `_clone_into_tempdir` which shells out to git.
CloneFn = Callable[[str, "GithubConfig"], Path]
EnumerateFn = Callable[[str, bool], list[str]]
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401 — kept in fetch return-type annotation
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.builtin.dir_source import DirConfig, DirConnector
from pleno_pii_scanner.sources.registry import ConnectorSpec


@dataclass(frozen=True, slots=True)
class GithubConfig:
    """Construction config for `GithubConnector`.

    Exactly one of `repo` or `org` must be set. `depth` controls the
    shallow-clone history; `full=True` does an unshallowed clone (for
    operators who explicitly want history scanning, though the dedicated
    `git` connector is usually a better fit).
    """

    repo: str | None = None
    org: str | None = None
    depth: int = 1
    full: bool = False
    include_archived: bool = False
    id: str | None = None

    def __post_init__(self) -> None:
        if (self.repo is None) == (self.org is None):
            raise ValueError(
                "GithubConfig must set exactly one of `repo` or `org`"
            )
        if self.depth < 1:
            raise ValueError("depth must be >= 1")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        if self.org is not None:
            return f"github-org:{self.org}"
        return f"github:{self.repo}"


class GithubConnector:
    """Repo-level SourceConnector. Owns shallow clones; rmtrees on close()."""

    kind = "github"

    def __init__(
        self,
        config: GithubConfig,
        *,
        clone_fn: CloneFn | None = None,
        enumerate_fn: EnumerateFn | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._clone_fn: CloneFn = clone_fn or _clone_into_tempdir
        self._enumerate_fn: EnumerateFn = enumerate_fn or _default_enumerate
        # slug → cloned path; populated lazily during fetch.
        self._clones: dict[str, Path] = {}
        self._tempdirs: list[Path] = []
        self._lock = asyncio.Lock()

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
        del cursor
        slugs = await self._resolve_slugs()
        for slug in slugs:
            repo_path = await self._ensure_clone(slug)
            inner = DirConnector(
                DirConfig(root=repo_path, id=f"github:{slug}")
            )
            try:
                async for inner_ref in inner.discover(filter, None):
                    yield self._wrap_ref(inner_ref, slug)
            finally:
                await inner.close()

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        slug = ref.metadata.get("slug")
        if slug is None or slug not in self._clones:
            # Fetch with no live clone — caller mixed a stale ref or
            # discover never ran. Same idiom as GitConnector: empty
            # async-iterator yields nothing rather than crashing the
            # scheduler's gather().
            return
        repo_path = self._clones[slug]
        inner = DirConnector(DirConfig(root=repo_path, id=f"github:{slug}"))
        try:
            inner_ref = self._unwrap_ref(ref, slug)
            async for doc in inner.fetch(inner_ref):
                # DirConnector advertises streaming=False and only yields
                # `Document`. Re-emit with our outer ref so finding paths
                # are reported as `slug/file` rather than the local clone.
                assert isinstance(doc, Document)
                yield Document(
                    ref=ref,
                    text=doc.text,
                    binary=doc.binary,
                    fetched_at=doc.fetched_at,
                    content_hash=doc.content_hash,
                    created_by=doc.created_by,
                    extra=doc.extra,
                )
        finally:
            await inner.close()

    async def close(self) -> None:
        # Best-effort tempdir cleanup. We swallow rmtree errors because
        # the cleaner is purely housekeeping — a leftover dir under
        # /tmp/pleno-scan-* is annoying but not a correctness issue, and
        # raising here would mask whatever real error caused close() to
        # be invoked from a finally clause.
        async with self._lock:
            for path in self._tempdirs:
                await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            self._tempdirs.clear()
            self._clones.clear()

    async def _resolve_slugs(self) -> list[str]:
        if self._config.repo is not None:
            return [self._config.repo]
        assert self._config.org is not None
        return await asyncio.to_thread(
            self._enumerate_fn,
            self._config.org,
            self._config.include_archived,
        )

    async def _ensure_clone(self, slug: str) -> Path:
        async with self._lock:
            cached = self._clones.get(slug)
            if cached is not None:
                return cached
        # The default clone fn shells out to `git clone --depth=N` and
        # the contextmanager-style `shallow_clone` rmtree's on exit; we
        # want the clone to live for the connector's lifetime, so the
        # default helper does its own tempdir + cleanup-on-failure logic
        # and the connector's `close()` does the success-path cleanup.
        path = await asyncio.to_thread(self._clone_fn, slug, self._config)
        async with self._lock:
            self._clones[slug] = path
            self._tempdirs.append(path)
        return path

    def _wrap_ref(self, inner: DocumentRef, slug: str) -> DocumentRef:
        # Re-emit with our connector identity + the slug so fetch can
        # find the right clone. parent_chain captures the github://
        # provenance for findings dashboards.
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"{slug}/{inner.path}",
            native_url=f"https://github.com/{slug}/blob/HEAD/{inner.path}",
            parent_chain=(f"github://{slug}",),
            content_type=inner.content_type,
            size=inner.size,
            etag=inner.etag,
            last_modified=inner.last_modified,
            metadata={**inner.metadata, "slug": slug, "inner_path": inner.path},
        )

    def _unwrap_ref(self, ref: DocumentRef, slug: str) -> DocumentRef:
        inner_path = ref.metadata["inner_path"]
        return DocumentRef(
            source_id=f"github:{slug}",
            source_kind="dir",
            path=inner_path,
            content_type=ref.content_type,
            size=ref.size,
            etag=ref.etag,
            last_modified=ref.last_modified,
        )


def _default_enumerate(org: str, include_archived: bool) -> list[str]:
    """Positional-arg adapter so EnumerateFn doubles are easy to write."""
    return list_org_repos(org, include_archived=include_archived)


def _clone_into_tempdir(slug: str, config: GithubConfig) -> Path:
    """Synchronous helper: shallow-clone `slug` into a fresh tempdir.

    Returns the cloned root. The connector's `close()` is responsible
    for rmtree-ing the directory; we keep the lifecycle there so a
    failed scan doesn't leave the temp dir behind.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pleno-gh-"))
    try:
        # We mirror `pleno_pii_scanner.github.shallow_clone`'s subprocess
        # call instead of using its contextmanager — the contextmanager
        # auto-rmtree's on exit and we want the connector's `close()` to
        # own that lifecycle. Duplication is intentional and tiny.
        url = (
            slug
            if "://" in slug or "@" in slug
            else f"https://github.com/{slug}.git"
        )
        cmd: list[str] = ["git", "clone", "--quiet"]
        if not config.full:
            cmd += [f"--depth={config.depth}"]
        cmd += [url, str(tmp)]
        subprocess.run(
            cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        return tmp
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    repo = config.get("repo")
    org = config.get("org")
    if (repo is None) == (org is None):
        raise ValueError(
            "github connector config requires exactly one of 'repo' or 'org'"
        )
    return GithubConnector(
        GithubConfig(
            repo=str(repo) if repo is not None else None,
            org=str(org) if org is not None else None,
            depth=int(config.get("depth", 1)),
            full=bool(config.get("full", False)),
            include_archived=bool(config.get("include_archived", False)),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="github",
    version="1.0.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        max_concurrent_fetches=4,
    ),
    required_scopes=(),
    description=(
        "GitHub shallow-clone scanner. Accepts a single owner/repo slug "
        "or an org name (uses `gh` CLI for enumeration). For App auth, "
        "GHES, or fine-grained PAT scopes install pleno-pii-scanner-github."
    ),
)


__all__ = ["SPEC", "GithubConfig", "GithubConnector"]
