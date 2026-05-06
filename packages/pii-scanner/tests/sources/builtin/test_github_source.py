"""Tests for the builtin `github` SourceConnector.

We never actually shell out to `git clone` or `gh repo list`. Instead
we inject a `clone_fn` that returns a pre-built local directory and an
`enumerate_fn` that returns a fixed slug list. This keeps the test
deterministic + offline + sub-second.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    GITHUB_KIND,
    GITHUB_SPEC,
    GithubConfig,
    GithubConnector,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    r = tmp_path / "fake-clone"
    r.mkdir()
    (r / "secret.txt").write_text("password=hunter2\n")
    (r / "readme.md").write_text("# hello\n")
    return r


def _stub_clone(path: Path):
    def _impl(slug: str, _config: GithubConfig) -> Path:
        # Per-slug subdir so two slugs don't collide.
        target = path / slug.replace("/", "_")
        if not target.exists():
            target.mkdir(parents=True)
            for f in path.iterdir():
                if f.is_file():
                    (target / f.name).write_text(f.read_text())
        return target

    return _impl


async def _drain_refs(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


# --- config validation ------------------------------------------------


class TestConfig:
    def test_requires_repo_or_org(self) -> None:
        with pytest.raises(ValueError, match="exactly one of"):
            GithubConfig()

    def test_rejects_both_repo_and_org(self) -> None:
        with pytest.raises(ValueError, match="exactly one of"):
            GithubConfig(repo="a/b", org="acme")

    def test_rejects_zero_depth(self) -> None:
        with pytest.raises(ValueError, match="depth must be >= 1"):
            GithubConfig(repo="a/b", depth=0)

    def test_id_default_for_repo(self) -> None:
        assert GithubConfig(repo="a/b").resolved_id() == "github:a/b"

    def test_id_default_for_org(self) -> None:
        assert GithubConfig(org="acme").resolved_id() == "github-org:acme"

    def test_id_explicit_overrides(self) -> None:
        assert GithubConfig(repo="a/b", id="custom").resolved_id() == "custom"


# --- protocol ---------------------------------------------------------


class TestProtocol:
    def test_runtime_isinstance(self, fake_repo: Path, tmp_path: Path) -> None:
        c = GithubConnector(
            GithubConfig(repo="a/b"),
            clone_fn=_stub_clone(fake_repo),
        )
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = GithubConnector(GithubConfig(repo="a/b"))
        assert c.capabilities() == Capabilities(
            # `incremental=True` advertises the IncrementalSourceConnector
            # protocol — sub-source level skip via list_subsources +
            # set_subsource_skip. Per-document iteration is still a full
            # walk inside an unchanged repo.
            incremental=True,
            binary=False,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )


# --- discover/fetch single repo --------------------------------------


class TestSingleRepo:
    async def test_discover_yields_files_via_dir_walk(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        c = GithubConnector(
            GithubConfig(repo="acme/widgets"),
            clone_fn=_stub_clone(tmp_path),
        )
        # Stage fake repo content under tmp_path so _stub_clone can copy.
        for f in fake_repo.iterdir():
            if f.is_file():
                (tmp_path / f.name).write_text(f.read_text())

        try:
            refs = await _drain_refs(c.discover(SourceFilter(), None))
            paths = {r.path for r in refs}
            assert paths == {
                "acme/widgets/secret.txt",
                "acme/widgets/readme.md",
            }
            for r in refs:
                assert r.parent_chain == ("github://acme/widgets",)
                assert r.metadata["slug"] == "acme/widgets"
                assert r.native_url is not None
                assert "github.com/acme/widgets" in r.native_url
        finally:
            await c.close()

    async def test_fetch_returns_file_contents(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        for f in fake_repo.iterdir():
            if f.is_file():
                (tmp_path / f.name).write_text(f.read_text())
        c = GithubConnector(
            GithubConfig(repo="acme/widgets"),
            clone_fn=_stub_clone(tmp_path),
        )
        try:
            refs = await _drain_refs(c.discover(SourceFilter(), None))
            secret_ref = next(r for r in refs if "secret.txt" in r.path)
            docs: list[Document] = []
            async for d in c.fetch(secret_ref):
                assert isinstance(d, Document)
                docs.append(d)
            assert len(docs) == 1
            assert docs[0].text is not None
            assert "hunter2" in docs[0].text
        finally:
            await c.close()

    async def test_fetch_unknown_slug_returns_empty(self) -> None:
        c = GithubConnector(GithubConfig(repo="acme/widgets"))
        ghost = DocumentRef(
            source_id=c.id,
            source_kind=c.kind,
            path="x/y/z",
            metadata={"slug": "never/cloned", "inner_path": "z"},
        )
        async for _ in c.fetch(ghost):
            pytest.fail("must yield nothing for slug not in clones")

    async def test_fetch_ref_without_slug_returns_empty(self) -> None:
        c = GithubConnector(GithubConfig(repo="acme/widgets"))
        ghost = DocumentRef(source_id=c.id, source_kind=c.kind, path="x/y/z")
        async for _ in c.fetch(ghost):
            pytest.fail("must yield nothing without slug metadata")

    async def test_clone_cached_across_fetches(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        # _ensure_clone must cache so multi-file fetch from the same slug
        # only clones once. We assert by counting `clone_fn` calls.
        for f in fake_repo.iterdir():
            if f.is_file():
                (tmp_path / f.name).write_text(f.read_text())
        calls = {"n": 0}
        base = _stub_clone(tmp_path)

        def counting_clone(slug: str, cfg: GithubConfig) -> Path:
            calls["n"] += 1
            return base(slug, cfg)

        c = GithubConnector(
            GithubConfig(repo="acme/widgets"),
            clone_fn=counting_clone,
        )
        try:
            await _drain_refs(c.discover(SourceFilter(), None))
            # Discover walked the clone once. Re-discovering must reuse.
            await _drain_refs(c.discover(SourceFilter(), None))
            assert calls["n"] == 1
        finally:
            await c.close()


# --- discover/fetch org enumeration ----------------------------------


class TestOrgEnumeration:
    async def test_enumerate_invoked_with_include_archived(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        for f in fake_repo.iterdir():
            if f.is_file():
                (tmp_path / f.name).write_text(f.read_text())
        seen: list[tuple[str, bool]] = []

        def enum(org: str, include_archived: bool) -> list[str]:
            seen.append((org, include_archived))
            return ["acme/one", "acme/two"]

        c = GithubConnector(
            GithubConfig(org="acme", include_archived=True),
            clone_fn=_stub_clone(tmp_path),
            enumerate_fn=enum,
        )
        try:
            refs = await _drain_refs(c.discover(SourceFilter(), None))
            slugs = {r.metadata["slug"] for r in refs}
            assert slugs == {"acme/one", "acme/two"}
            assert seen == [("acme", True)]
        finally:
            await c.close()


# --- close ------------------------------------------------------------


class TestClose:
    async def test_close_rmtree_tempdirs(self, fake_repo: Path, tmp_path: Path) -> None:
        for f in fake_repo.iterdir():
            if f.is_file():
                (tmp_path / f.name).write_text(f.read_text())
        c = GithubConnector(
            GithubConfig(repo="acme/widgets"),
            clone_fn=_stub_clone(tmp_path),
        )
        await _drain_refs(c.discover(SourceFilter(), None))
        cloned = c._tempdirs[0]
        assert cloned.exists()
        await c.close()
        assert not cloned.exists()
        # Idempotent.
        await c.close()


# --- spec / factory --------------------------------------------------


class TestSpecFactory:
    def test_spec_metadata(self) -> None:
        assert GITHUB_KIND == "github"
        assert GITHUB_SPEC.kind == "github"
        assert GITHUB_SPEC.version == "1.0.0"

    def test_factory_repo(self) -> None:
        register(GITHUB_SPEC)
        c = create("github", {"repo": "a/b"})
        assert isinstance(c, GithubConnector)
        assert c.id == "github:a/b"

    def test_factory_org(self) -> None:
        register(GITHUB_SPEC)
        c = create(
            "github",
            {"org": "acme", "depth": 2, "full": False, "include_archived": True},
        )
        assert isinstance(c, GithubConnector)
        assert c.id == "github-org:acme"

    def test_factory_id_explicit(self) -> None:
        register(GITHUB_SPEC)
        c = create("github", {"repo": "a/b", "id": "custom"})
        assert c.id == "custom"

    def test_factory_rejects_neither(self) -> None:
        with pytest.raises(ValueError, match="exactly one of"):
            GITHUB_SPEC.factory({})

    def test_factory_rejects_both(self) -> None:
        with pytest.raises(ValueError, match="exactly one of"):
            GITHUB_SPEC.factory({"repo": "a/b", "org": "acme"})


# --- _clone_into_tempdir + _default_enumerate ------------------------


class TestProductionHelpers:
    def test_clone_into_tempdir_url_for_slug(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Intercept subprocess.run so we can assert the constructed URL
        # without actually shelling out to git.
        from pleno_pii_scanner.sources.builtin import github_source as mod

        seen_cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            seen_cmds.append(list(cmd))

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        # Drive with a slug (no scheme) so the helper synthesizes a URL.
        path = mod._clone_into_tempdir(
            "acme/widgets", GithubConfig(repo="acme/widgets")
        )
        assert path.exists()
        assert any("https://github.com/acme/widgets.git" in c for c in seen_cmds[0])
        assert "--depth=1" in seen_cmds[0]
        # Cleanup the empty tempdir we created.
        path.rmdir()

    def test_clone_into_tempdir_passes_url_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pleno_pii_scanner.sources.builtin import github_source as mod

        seen_cmds: list[list[str]] = []
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda cmd, **kw: (
                seen_cmds.append(list(cmd)) or type("R", (), {"returncode": 0})()
            ),
        )
        url = "git@github.com:acme/widgets.git"
        path = mod._clone_into_tempdir(url, GithubConfig(repo=url))
        assert url in seen_cmds[0]
        path.rmdir()

    def test_clone_into_tempdir_full_omits_depth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pleno_pii_scanner.sources.builtin import github_source as mod

        seen_cmds: list[list[str]] = []
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda cmd, **kw: (
                seen_cmds.append(list(cmd)) or type("R", (), {"returncode": 0})()
            ),
        )
        path = mod._clone_into_tempdir(
            "acme/widgets", GithubConfig(repo="acme/widgets", full=True)
        )
        assert not any(c.startswith("--depth=") for c in seen_cmds[0])
        path.rmdir()

    def test_clone_into_tempdir_cleans_up_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pleno_pii_scanner.sources.builtin import github_source as mod

        captured: list[Path] = []

        def boom(cmd, **kw):
            # Capture the tempdir path before raising so we can assert
            # it was rmtree'd.
            captured.append(Path(cmd[-1]))
            raise RuntimeError("clone failed")

        monkeypatch.setattr(mod.subprocess, "run", boom)
        with pytest.raises(RuntimeError, match="clone failed"):
            mod._clone_into_tempdir("a/b", GithubConfig(repo="a/b"))
        assert captured
        assert not captured[0].exists()

    def test_default_enumerate_passes_include_archived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pleno_pii_scanner.sources.builtin import github_source as mod

        called: dict = {}

        def fake_list(org: str, *, include_archived: bool) -> list[str]:
            called["args"] = (org, include_archived)
            return ["a/x"]

        monkeypatch.setattr(mod, "list_org_repos", fake_list)
        result = mod._default_enumerate("acme", True)
        assert result == ["a/x"]
        assert called["args"] == ("acme", True)


# --- IncrementalSourceConnector ---------------------------------------


class TestIncrementalSubsources:
    def test_runtime_isinstance(self) -> None:
        c = GithubConnector(GithubConfig(repo="a/b"))
        assert isinstance(c, IncrementalSourceConnector)

    async def test_list_subsources_for_single_repo(self) -> None:
        # One slug → one Subsource, fingerprint = the stub HEAD SHA.
        c = GithubConnector(
            GithubConfig(repo="acme/widgets"),
            head_sha_fn=lambda slug: "deadbeefcafebabe1234567890abcdef12345678",
        )
        subs = await c.list_subsources()
        assert len(subs) == 1
        assert subs[0].sub_id == "acme/widgets"
        assert subs[0].fingerprint == ("deadbeefcafebabe1234567890abcdef12345678")

    async def test_list_subsources_for_org(self) -> None:
        c = GithubConnector(
            GithubConfig(org="acme"),
            enumerate_fn=lambda org, archived: ["acme/one", "acme/two"],
            head_sha_fn=lambda slug: (
                f"sha-{slug.split('/')[-1]}-pad-pad-pad-pad-pad-pad-pad"
            ),
        )
        subs = await c.list_subsources()
        slug_set = {s.sub_id for s in subs}
        assert slug_set == {"acme/one", "acme/two"}

    async def test_unknown_sha_yields_sentinel_fingerprint(self) -> None:
        c = GithubConnector(
            GithubConfig(repo="acme/widgets"),
            head_sha_fn=lambda slug: None,
        )
        subs = await c.list_subsources()
        # Sentinel makes the runner treat this as a guaranteed miss.
        assert subs[0].fingerprint == "unknown:acme/widgets"

    async def test_set_subsource_skip_omits_those_slugs_from_discover(
        self, fake_repo: Path, tmp_path: Path
    ) -> None:
        # Stage the fake repo content under tmp_path so _stub_clone copies it.
        for f in fake_repo.iterdir():
            if f.is_file():
                (tmp_path / f.name).write_text(f.read_text())
        c = GithubConnector(
            GithubConfig(org="acme"),
            clone_fn=_stub_clone(tmp_path),
            enumerate_fn=lambda org, archived: ["acme/one", "acme/two"],
            head_sha_fn=lambda slug: "0" * 40,
        )
        c.set_subsource_skip(frozenset({"acme/one"}))
        try:
            refs = await _drain_refs(c.discover(SourceFilter(), None))
            slugs = {r.metadata["slug"] for r in refs}
            # acme/one was told to skip; only acme/two surfaces.
            assert slugs == {"acme/two"}
            for r in refs:
                # Subsource attribution is wired through metadata.
                assert r.metadata[SUBSOURCE_METADATA_KEY] == "acme/two"
        finally:
            await c.close()

    def test_default_head_sha_parses_ls_remote_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pleno_pii_scanner.sources.builtin import github_source as mod

        sample = b"abcdef0123456789abcdef0123456789abcdef01\tHEAD\n"
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda cmd, **kw: type("R", (), {"stdout": sample})(),
        )
        assert mod._default_head_sha("acme/widgets") == (
            "abcdef0123456789abcdef0123456789abcdef01"
        )

    def test_default_head_sha_returns_none_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pleno_pii_scanner.sources.builtin import github_source as mod

        def boom(cmd, **kw):
            raise mod.subprocess.SubprocessError("network down")

        monkeypatch.setattr(mod.subprocess, "run", boom)
        assert mod._default_head_sha("acme/widgets") is None

    def test_default_head_sha_rejects_garbage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pleno_pii_scanner.sources.builtin import github_source as mod

        # HTML error page from a captive portal: not a SHA.
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda cmd, **kw: type("R", (), {"stdout": b"<html>nope</html>\n"})(),
        )
        assert mod._default_head_sha("acme/widgets") is None
