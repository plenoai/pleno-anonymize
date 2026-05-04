"""Tests for the builtin `dir` SourceConnector."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner.sources import registry as _registry_mod
from pleno_pii_scanner.sources.builtin import DIR_KIND, DIR_SPEC, DirConfig, DirConnector


@pytest.fixture(autouse=True)
def _isolate_registry():
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


async def _drain_refs(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [r async for r in it]


async def _drain_fetch(c: SourceConnector, ref: DocumentRef) -> list[Document]:
    out: list[Document] = []
    async for d in c.fetch(ref):
        # builtin dir never streams — assert and narrow the type
        assert isinstance(d, Document), f"unexpected chunk: {d!r}"
        out.append(d)
    return out


class TestProtocolCompliance:
    def test_runtime_isinstance(self, tmp_path: Path) -> None:
        c = DirConnector(DirConfig(root=tmp_path))
        assert isinstance(c, SourceConnector)

    def test_capabilities_are_conservative(self, tmp_path: Path) -> None:
        c = DirConnector(DirConfig(root=tmp_path))
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=8,
            streaming=False,
        )

    def test_id_defaults_to_resolved_path(self, tmp_path: Path) -> None:
        c = DirConnector(DirConfig(root=tmp_path))
        assert c.id == f"dir:{tmp_path.resolve().as_posix()}"

    def test_id_can_be_overridden(self, tmp_path: Path) -> None:
        c = DirConnector(DirConfig(root=tmp_path, id="snapshot-A"))
        assert c.id == "snapshot-A"


class TestDiscover:
    async def test_yields_files_under_root(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        paths = sorted(r.path for r in refs)
        assert paths == ["a.txt", "b.txt"]

    async def test_paths_are_relative_posix(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.txt").write_text("x")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        assert refs[0].path == "sub/nested.txt"

    async def test_native_url_is_file_scheme(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        assert refs[0].native_url is not None
        assert refs[0].native_url.startswith("file://")

    async def test_size_and_mtime_are_populated(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        assert refs[0].size == 5
        assert refs[0].last_modified is not None

    async def test_respects_existing_skip_list_and_gitignore(
        self, tmp_path: Path
    ) -> None:
        # Inherits walker.walk's behavior; this is a smoke test that
        # the adapter does not bypass any of it. .gitignore itself is
        # a regular text file and walker yields it, but secret.txt
        # (matched by the rule) and node_modules/ (in the noise list)
        # must not appear.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "ignored.js").write_text("var y = 1")
        (tmp_path / ".gitignore").write_text("secret.txt\n")
        (tmp_path / "secret.txt").write_text("hidden")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        names = sorted(r.path for r in refs)
        assert "src/main.py" in names
        assert "secret.txt" not in names
        assert not any("node_modules" in n for n in names)

    async def test_filter_max_size_takes_min_with_config(
        self, tmp_path: Path
    ) -> None:
        # Discover-time `filter.max_size` is the operator's `--max-file-size`;
        # the connector's own config sets the safety ceiling. The lower of the
        # two wins so a per-scan filter cannot escalate above the configured
        # limit.
        (tmp_path / "big.txt").write_text("a" * 200)
        (tmp_path / "small.txt").write_text("ok")
        c = DirConnector(DirConfig(root=tmp_path, max_file_size=100))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        assert sorted(r.path for r in refs) == ["small.txt"]

        # Operator can tighten further but not loosen. small.txt (2 bytes)
        # passes max_size=10; big.txt (200 bytes) does not.
        refs2 = await _drain_refs(c.discover(SourceFilter(max_size=10), None))
        assert sorted(r.path for r in refs2) == ["small.txt"]

        # And the operator filter cannot exceed the connector ceiling.
        refs3 = await _drain_refs(c.discover(SourceFilter(max_size=5000), None))
        assert sorted(r.path for r in refs3) == ["small.txt"]

    async def test_filter_include_overrides_config(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "a.md").write_text("y")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(
            c.discover(SourceFilter(include=("*.md",)), None)
        )
        assert [r.path for r in refs] == ["a.md"]

    async def test_filter_exclude_overrides_config(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(
            c.discover(SourceFilter(exclude=("a.txt",)), None)
        )
        assert [r.path for r in refs] == ["b.txt"]

    async def test_cursor_is_ignored(self, tmp_path: Path) -> None:
        # Connector reports incremental=False; passing a cursor must not
        # cause an error.
        (tmp_path / "a.txt").write_text("x")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), "any-cursor"))
        assert len(refs) == 1


class TestFetch:
    async def test_returns_text_document(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        docs = await _drain_fetch(c, refs[0])
        assert len(docs) == 1
        assert docs[0].text == "hello"
        assert docs[0].fetched_at is not None

    async def test_decodes_invalid_utf8_with_replacement(
        self, tmp_path: Path
    ) -> None:
        # walker filters NUL-bearing files; a file with garbled but
        # NUL-free bytes still gets through and must not crash fetch().
        (tmp_path / "broken.txt").write_bytes(b"\xff\xfeABC")
        c = DirConnector(DirConfig(root=tmp_path))
        refs = await _drain_refs(c.discover(SourceFilter(), None))
        docs = await _drain_fetch(c, refs[0])
        assert "ABC" in docs[0].text

    async def test_rejects_path_outside_root(self, tmp_path: Path) -> None:
        (tmp_path / "inside.txt").write_text("x")
        c = DirConnector(DirConfig(root=tmp_path / "subdir"))
        (tmp_path / "subdir").mkdir()
        # Hand-crafted ref pointing outside root via .. traversal.
        bad_ref = DocumentRef(
            source_id=c.id,
            source_kind="dir",
            path="../inside.txt",
        )
        with pytest.raises(PermissionError, match="outside configured root"):
            async for _ in c.fetch(bad_ref):
                pass

    async def test_missing_file_yields_nothing(self, tmp_path: Path) -> None:
        c = DirConnector(DirConfig(root=tmp_path))
        ref = DocumentRef(source_id=c.id, source_kind="dir", path="ghost.txt")
        docs = [d async for d in c.fetch(ref)]
        assert docs == []


class TestRaceConditions:
    async def test_file_disappearing_between_walk_and_stat_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Real-world race: another process deletes the file after walker
        # enumerated it. _safe_size returns None and the connector must
        # skip rather than raising.
        from pleno_pii_scanner.sources.builtin import dir_source as ds

        (tmp_path / "a.txt").write_text("x")
        c = DirConnector(DirConfig(root=tmp_path))

        original_stat = Path.stat

        def fake_stat(self: Path, *args: object, **kwargs: object):  # noqa: ARG001
            if self.name == "a.txt":
                raise OSError("simulated race: file disappeared")
            return original_stat(self)

        monkeypatch.setattr(ds, "_safe_size", lambda p: None if p.name == "a.txt" else p.stat().st_size)

        refs = await _drain_refs(c.discover(SourceFilter(), None))
        assert refs == []

    async def test_safe_size_returns_none_on_oserror(
        self, tmp_path: Path
    ) -> None:
        # Direct unit test of the helper used by discover().
        from pleno_pii_scanner.sources.builtin.dir_source import _safe_size

        ghost = tmp_path / "does-not-exist"
        assert _safe_size(ghost) is None

    async def test_safe_mtime_returns_none_on_oserror(
        self, tmp_path: Path
    ) -> None:
        from pleno_pii_scanner.sources.builtin.dir_source import _safe_mtime

        ghost = tmp_path / "does-not-exist"
        assert _safe_mtime(ghost) is None


class TestClose:
    async def test_close_is_idempotent(self, tmp_path: Path) -> None:
        c = DirConnector(DirConfig(root=tmp_path))
        await c.close()
        await c.close()


class TestRegistration:
    def test_spec_kind_is_dir(self) -> None:
        assert DIR_KIND == "dir"
        assert DIR_SPEC.kind == "dir"

    def test_spec_carries_capabilities(self) -> None:
        assert DIR_SPEC.capabilities.incremental is False

    def test_factory_via_registry(self, tmp_path: Path) -> None:
        register(DIR_SPEC)
        c = create("dir", {"root": tmp_path})
        assert isinstance(c, DirConnector)
        assert c.id == f"dir:{tmp_path.resolve().as_posix()}"

    def test_factory_accepts_string_root(self, tmp_path: Path) -> None:
        register(DIR_SPEC)
        c = create("dir", {"root": str(tmp_path)})
        assert isinstance(c, DirConnector)

    def test_factory_propagates_config(self, tmp_path: Path) -> None:
        register(DIR_SPEC)
        c = create(
            "dir",
            {
                "root": tmp_path,
                "id": "snap",
                "max_file_size": 200,
                "include": ["*.py"],
                "exclude": ["foo.py"],
                "respect_gitignore": False,
            },
        )
        assert isinstance(c, DirConnector)
        assert c.id == "snap"

    def test_factory_requires_root(self) -> None:
        register(DIR_SPEC)
        with pytest.raises(ValueError, match="root"):
            create("dir", {})
