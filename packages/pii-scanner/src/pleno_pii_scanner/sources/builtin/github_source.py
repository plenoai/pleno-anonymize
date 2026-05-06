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
from pleno_pii_scanner.sources.base import (
    SUBSOURCE_METADATA_KEY,
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401 — kept in fetch return-type annotation
    DocumentRef,
    SourceConnector,
    SourceFilter,
    Subsource,
)
from pleno_pii_scanner.sources.builtin.dir_source import DirConfig, DirConnector
from pleno_pii_scanner.sources.registry import ConnectorSpec


# Test seams. Production defaults shell out (`git clone`, `gh repo list`,
# `git ls-remote`); tests inject in-memory doubles to keep the suite
# deterministic + offline + sub-second.
CloneFn = Callable[[str, "GithubConfig"], Path]
EnumerateFn = Callable[[str, bool], list[str]]
# `HeadShaFn` returns None when the SHA is unavailable (private repo,
# network failure) so the runner falls back to a full clone instead of
# risking a stale-cache hit.
HeadShaFn = Callable[[str], str | None]


# Bounds the number of concurrent `git ls-remote` subprocesses during
# `list_subsources`. 16 is a comfortable balance — high enough that an
# org with 1000 repos resolves in a few seconds, low enough that we do
# not exhaust file descriptors or trip GitHub's per-IP abuse heuristics.
_HEAD_SHA_CONCURRENCY = 16


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
        head_sha_fn: HeadShaFn | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._clone_fn: CloneFn = clone_fn or _clone_into_tempdir
        self._enumerate_fn: EnumerateFn = enumerate_fn or _default_enumerate
        self._head_sha_fn: HeadShaFn = head_sha_fn or _default_head_sha
        # slug → cloned path; populated lazily during fetch.
        self._clones: dict[str, Path] = {}
        self._tempdirs: list[Path] = []
        # IncrementalSourceConnector state. Populated by list_subsources()
        # so a subsequent set_subsource_skip() can land before discover()
        # runs; cleared on close().
        self._enumerated_slugs: list[str] | None = None
        self._skip_subsources: frozenset[str] = frozenset()
        self._lock = asyncio.Lock()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            # `incremental` advertises sub-source level skip via
            # IncrementalSourceConnector; the per-document iteration is
            # still a full re-walk inside an unchanged repo.
            incremental=True,
            binary=False,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def list_subsources(self) -> tuple[Subsource, ...]:
        """Return one Subsource per repo with its remote HEAD SHA.

        For an org config, enumerates all repos under the org (cached
        across this connector's lifetime so a follow-up `discover()`
        does not re-list). For a single-repo config, returns one entry.
        Slugs whose HEAD SHA cannot be resolved are returned with the
        sentinel fingerprint `unknown:<slug>` — the runner treats them
        as cache misses, never as silent hits.

        SHA resolution fans out across `_HEAD_SHA_CONCURRENCY` worker
        threads — each `git ls-remote` is a 100–500 ms blocking network
        round-trip, so an org with 1000 repos finishes in seconds, not
        minutes. The semaphore caps the fan-out so we do not flood the
        kernel with subprocess forks or trip GitHub's anti-abuse limits
        on a single client.
        """
        slugs = await self._resolve_slugs(use_cache=True)
        sem = asyncio.Semaphore(_HEAD_SHA_CONCURRENCY)

        async def _resolve(slug: str) -> Subsource:
            async with sem:
                sha = await asyncio.to_thread(self._head_sha_fn, slug)
            fingerprint = sha if sha is not None else f"unknown:{slug}"
            return Subsource(sub_id=slug, fingerprint=fingerprint)

        return tuple(await asyncio.gather(*(_resolve(s) for s in slugs)))

    def set_subsource_skip(self, skip: frozenset[str]) -> None:
        self._skip_subsources = skip

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        del cursor
        slugs = await self._resolve_slugs(use_cache=True)
        for slug in slugs:
            if slug in self._skip_subsources:
                # Cache hit — the IncrementalRunner already replayed this
                # repo's findings; we must not clone or yield refs for it.
                continue
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
            self._enumerated_slugs = None
            self._skip_subsources = frozenset()

    async def _resolve_slugs(self, *, use_cache: bool = False) -> list[str]:
        """Resolve the slug list. Single-repo configs short-circuit; for
        org configs the enumeration is cached after the first call so
        list_subsources() and discover() do not double-list. `use_cache`
        is the only entry point — passing False forces a re-enumeration
        (currently unused, kept for symmetry with the builtin connector
        idiom of an explicit refresh path)."""
        if self._config.repo is not None:
            return [self._config.repo]
        assert self._config.org is not None
        if use_cache and self._enumerated_slugs is not None:
            return list(self._enumerated_slugs)
        slugs = await asyncio.to_thread(
            self._enumerate_fn,
            self._config.org,
            self._config.include_archived,
        )
        self._enumerated_slugs = list(slugs)
        return list(slugs)

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
        # provenance for findings dashboards. SUBSOURCE_METADATA_KEY
        # lets the IncrementalRunner attribute findings back to this
        # repo when it stores the per-sub-source cache entry.
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
            metadata={
                **inner.metadata,
                "slug": slug,
                "inner_path": inner.path,
                SUBSOURCE_METADATA_KEY: slug,
            },
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


def _default_head_sha(slug: str) -> str | None:
    """Resolve a slug's remote HEAD commit SHA without cloning.

    Uses `git ls-remote --symref <url> HEAD`, parsing the SHA out of the
    second line ("<sha>\\tHEAD"). One outbound TCP connection per repo;
    no working tree on disk. Returns None on any failure so the runner
    can fall back to a full clone instead of risking a stale-cache hit.
    """
    url = (
        slug
        if "://" in slug or "@" in slug
        else f"https://github.com/{slug}.git"
    )
    try:
        proc = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    line = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
    if not line:
        return None
    sha = line[0].split()[0].strip()
    # WHY: a valid Git SHA-1 is 40 hex chars; SHA-256 repos emit 64.
    # Anything else (HTML error page from a captive portal, an empty
    # ref) is a sentinel-worthy failure. Treating it as None forces a
    # cache miss + fresh clone rather than caching garbage.
    if len(sha) not in (40, 64) or not all(c in "0123456789abcdef" for c in sha):
        return None
    return sha


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
