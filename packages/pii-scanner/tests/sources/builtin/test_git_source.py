"""Tests for the builtin `git` SourceConnector."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pleno_pii_scanner.sources import (
    SUBSOURCE_METADATA_KEY,
    Capabilities,
    Document,
    DocumentRef,
    IncrementalSourceConnector,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner.sources.builtin import (
    GIT_KIND,
    GIT_SPEC,
    GitConfig,
    GitConnector,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(
        list(args),
        cwd=str(cwd),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Build a tiny throwaway git repo with two commits across two files."""
    r = tmp_path / "repo"
    r.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=r)
    _run("git", "config", "user.email", "alice@example.com", cwd=r)
    _run("git", "config", "user.name", "Alice", cwd=r)
    _run("git", "config", "commit.gpgsign", "false", cwd=r)
    (r / "a.txt").write_text("hello world\n")
    _run("git", "add", "a.txt", cwd=r)
    _run("git", "commit", "-q", "-m", "first", cwd=r)
    (r / "b.txt").write_text("password=hunter2\n")
    _run("git", "add", "b.txt", cwd=r)
    _run("git", "commit", "-q", "-m", "second", cwd=r)
    return r


async def _drain(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


class TestProtocol:
    def test_runtime_isinstance(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        assert isinstance(c, SourceConnector)

    def test_capabilities_advertise_subsource_skip(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        assert c.capabilities() == Capabilities(
            # IncrementalSourceConnector treats the whole repo as one
            # sub-source whose fingerprint is HEAD's SHA.
            incremental=True,
            binary=False,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )

    def test_id_defaults_to_repo_path(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        assert c.id == f"git:{repo.resolve().as_posix()}"

    def test_id_explicit_overrides(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo, id="custom"))
        assert c.id == "custom"


class TestDiscover:
    async def test_yields_one_ref_per_commit_file(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        refs = await _drain(c.discover(SourceFilter(), None))
        # Two commits, one file each.
        files = {r.metadata["file"] for r in refs}
        assert files == {"a.txt", "b.txt"}
        # path = "<file>@<short-sha>" and short-sha is 12 hex chars.
        for r in refs:
            assert "@" in r.path
            file, short = r.path.split("@", 1)
            assert len(short) == 12

    async def test_metadata_has_commit_attribution(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        refs = await _drain(c.discover(SourceFilter(), None))
        assert all(r.metadata["commit_email"] == "alice@example.com" for r in refs)
        assert all(r.metadata["commit_author"] == "Alice" for r in refs)

    async def test_max_commits_caps_traversal(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo, max_commits=1))
        refs = await _drain(c.discover(SourceFilter(), None))
        # Only the most recent commit (b.txt).
        assert {r.metadata["file"] for r in refs} == {"b.txt"}

    async def test_filter_include_excludes_other_files(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        refs = await _drain(c.discover(SourceFilter(include=("a.*",)), None))
        assert {r.metadata["file"] for r in refs} == {"a.txt"}

    async def test_filter_exclude_drops_matches(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        refs = await _drain(c.discover(SourceFilter(exclude=("b.*",)), None))
        assert {r.metadata["file"] for r in refs} == {"a.txt"}

    async def test_filter_since_drops_old_commits(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        future = datetime.now(UTC).replace(year=2099)
        refs = await _drain(c.discover(SourceFilter(since=future), None))
        assert refs == []

    async def test_filter_max_size_passes_when_size_unknown(
        self, repo: Path
    ) -> None:
        # GitConnector never sets ref.size, so max_size never filters
        # anything out — we still want the branch executed for coverage.
        c = GitConnector(GitConfig(repo=repo))
        refs = await _drain(c.discover(SourceFilter(max_size=1), None))
        assert len(refs) > 0

    async def test_cursor_argument_ignored(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        refs1 = await _drain(c.discover(SourceFilter(), None))
        refs2 = await _drain(c.discover(SourceFilter(), "anything"))
        assert {r.path for r in refs1} == {r.path for r in refs2}


class TestFetch:
    async def test_fetch_returns_aligned_text(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        refs = await _drain(c.discover(SourceFilter(), None))
        ref = next(r for r in refs if r.metadata["file"] == "b.txt")
        docs: list[Document] = []
        async for d in c.fetch(ref):
            assert isinstance(d, Document)
            docs.append(d)
        assert len(docs) == 1
        assert docs[0].text is not None
        assert "password=hunter2" in docs[0].text

    async def test_fetch_attaches_principal(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        refs = await _drain(c.discover(SourceFilter(), None))
        ref = refs[0]
        async for d in c.fetch(ref):
            assert isinstance(d, Document)
            assert d.created_by is not None
            assert d.created_by.email == "alice@example.com"
            assert d.created_by.display_name == "Alice"

    async def test_fetch_for_unknown_ref_yields_nothing(
        self, repo: Path
    ) -> None:
        c = GitConnector(GitConfig(repo=repo))
        unknown = DocumentRef(
            source_id=c.id,
            source_kind=c.kind,
            path="ghost.txt@deadbeefdeadbe",
        )
        # discover() never ran, so the slice cache is empty.
        async for _ in c.fetch(unknown):
            pytest.fail("fetch must not yield for unknown ref")


class TestClose:
    async def test_close_clears_cache(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        await _drain(c.discover(SourceFilter(), None))
        assert c._slices  # populated
        await c.close()
        assert not c._slices


class TestSpecAndFactory:
    def test_spec_metadata(self) -> None:
        assert GIT_KIND == "git"
        assert GIT_SPEC.kind == "git"
        assert GIT_SPEC.version == "1.0.0"

    def test_factory_via_registry(self, repo: Path) -> None:
        register(GIT_SPEC)
        c = create("git", {"repo": str(repo)})
        assert isinstance(c, GitConnector)
        assert c.id == f"git:{repo.resolve().as_posix()}"

    def test_factory_accepts_path_object(self, repo: Path) -> None:
        register(GIT_SPEC)
        c = create("git", {"repo": repo, "id": "x", "max_commits": 2})
        assert isinstance(c, GitConnector)
        assert c.id == "x"

    def test_factory_rejects_missing_repo(self) -> None:
        with pytest.raises(ValueError, match="requires 'repo'"):
            GIT_SPEC.factory({})


class TestParseHelpers:
    def test_parse_iso_returns_none_for_garbage(self) -> None:
        from pleno_pii_scanner.sources.builtin.git_source import _parse_iso

        assert _parse_iso("not-a-date") is None

    def test_build_body_handles_empty_lines(self) -> None:
        from pleno_pii_scanner.sources.builtin.git_source import (
            _CommitFile,
            _build_body,
        )
        from pleno_pii_scanner.git_history import CommitMeta

        empty = _CommitFile(
            commit=CommitMeta("sha", "a", "a@x", "2026-01-01"),
            file="x",
            lines={},
        )
        assert _build_body(empty) == ""

    def test_ref_passes_filter_max_size_drops_oversize(self) -> None:
        # Construct a ref with an explicit size to exercise the max_size
        # branch — GitConnector itself never populates ref.size, so the
        # only way to cover this is direct call.
        from pleno_pii_scanner.sources.builtin.git_source import (
            _ref_passes_filter,
        )

        ref = DocumentRef(
            source_id="git:x",
            source_kind="git",
            path="big.bin",
            size=10 * 1024,
            metadata={"file": "big.bin"},
        )
        assert not _ref_passes_filter(ref, SourceFilter(max_size=1024))
        assert _ref_passes_filter(ref, SourceFilter(max_size=20 * 1024))

    def test_build_body_pads_gaps(self) -> None:
        from pleno_pii_scanner.sources.builtin.git_source import (
            _CommitFile,
            _build_body,
        )
        from pleno_pii_scanner.git_history import CommitMeta

        sparse = _CommitFile(
            commit=CommitMeta("sha", "a", "a@x", "2026-01-01"),
            file="x",
            lines={1: "first", 5: "fifth"},
        )
        body = _build_body(sparse)
        lines = body.split("\n")
        assert lines[0] == "first"
        assert lines[1] == ""
        assert lines[4] == "fifth"


class TestIncrementalSubsources:
    def test_runtime_isinstance(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        assert isinstance(c, IncrementalSourceConnector)

    async def test_list_subsources_uses_head_sha(self, repo: Path) -> None:
        c = GitConnector(GitConfig(repo=repo))
        subs = await c.list_subsources()
        # One sub-source = the whole repo. Fingerprint is HEAD's SHA.
        assert len(subs) == 1
        assert subs[0].sub_id == c.id
        assert len(subs[0].fingerprint) in (40, 64)
        assert all(ch in "0123456789abcdef" for ch in subs[0].fingerprint)

    async def test_list_subsources_unknown_sha_for_broken_repo(
        self, tmp_path: Path
    ) -> None:
        broken = tmp_path / "not-a-repo"
        broken.mkdir()
        c = GitConnector(GitConfig(repo=broken))
        subs = await c.list_subsources()
        assert subs[0].fingerprint.startswith("unknown:")

    async def test_set_subsource_skip_yields_zero_refs(
        self, repo: Path
    ) -> None:
        c = GitConnector(GitConfig(repo=repo))
        c.set_subsource_skip(frozenset({c.id}))
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert refs == []
        finally:
            await c.close()

    async def test_subsource_metadata_attached_to_refs(
        self, repo: Path
    ) -> None:
        c = GitConnector(GitConfig(repo=repo))
        try:
            refs = await _drain(c.discover(SourceFilter(), None))
            assert refs
            for r in refs:
                assert r.metadata[SUBSOURCE_METADATA_KEY] == c.id
        finally:
            await c.close()
