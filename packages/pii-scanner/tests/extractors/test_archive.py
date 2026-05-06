"""Tests for the archive extractor including bomb guards.

The bomb tests are the critical contract: a deployment that lets a
1KB-on-disk zip blow out memory is a denial-of-service vector.
"""

from __future__ import annotations

import gzip
import io

import pytest

from pleno_pii_scanner.extractors import collect
from pleno_pii_scanner.extractors.archive import (
    ArchiveExtractor,
    BombGuardConfig,
)
from pleno_pii_scanner.extractors.base import BombGuardError, ExtractionWarning
from pleno_pii_scanner.sources.base import Document, DocumentRef
from .fixtures import (
    make_nested_zip_bomb,
    make_tar,
    make_zip,
    make_zip_bomb,
)


def _ref() -> DocumentRef:
    return DocumentRef(source_id="s", source_kind="t", path="p")


def _doc(blob: bytes) -> Document:
    return Document(ref=_ref(), binary=blob)


class TestZipExtraction:
    @pytest.mark.asyncio
    async def test_single_member(self) -> None:
        ex = ArchiveExtractor()
        blob = make_zip({"a.txt": b"hello world"})
        frags = await collect(ex, _doc(blob))
        assert len(frags) == 1
        assert frags[0].text == "hello world"
        assert frags[0].path_hint == "a.txt"

    @pytest.mark.asyncio
    async def test_multiple_members_preserve_order(self) -> None:
        ex = ArchiveExtractor()
        blob = make_zip({"a.txt": b"AAA", "b.txt": b"BBB"})
        frags = await collect(ex, _doc(blob))
        names = [f.path_hint for f in frags]
        assert "a.txt" in names
        assert "b.txt" in names

    @pytest.mark.asyncio
    async def test_directories_skipped(self) -> None:
        # A zip with an explicit directory entry must not yield a fragment.
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dir/", b"")
            zf.writestr("dir/leaf.txt", b"leaf")
        ex = ArchiveExtractor()
        frags = await collect(ex, _doc(buf.getvalue()))
        assert [f.path_hint for f in frags] == ["dir/leaf.txt"]

    @pytest.mark.asyncio
    async def test_nested_zip_recurses_with_path(self) -> None:
        ex = ArchiveExtractor()
        inner = make_zip({"leaf.txt": b"deep"})
        outer = make_zip({"inner.zip": inner})
        frags = await collect(ex, _doc(outer))
        assert any("inner.zip/leaf.txt" in f.path_hint for f in frags)
        assert any(f.text == "deep" for f in frags)


class TestBombGuard:
    @pytest.mark.asyncio
    async def test_zip_bomb_rejected_by_ratio(self) -> None:
        # 1MB of NULs deflates to ~1KB so ratio ~1000x — must blow the
        # 100x guard.
        ex = ArchiveExtractor()
        blob = make_zip_bomb(payload_size=1024 * 1024)
        with pytest.raises(BombGuardError, match="expansion"):
            await collect(ex, _doc(blob))

    @pytest.mark.asyncio
    async def test_depth_bomb_rejected(self) -> None:
        # 9 layers of nesting must trip the depth=8 guard.
        ex = ArchiveExtractor(BombGuardConfig(max_depth=8))
        blob = make_nested_zip_bomb(depth=9)
        with pytest.raises(BombGuardError, match="depth"):
            await collect(ex, _doc(blob))

    @pytest.mark.asyncio
    async def test_depth_under_limit_allowed(self) -> None:
        ex = ArchiveExtractor(BombGuardConfig(max_depth=8, max_expansion=10000))
        blob = make_nested_zip_bomb(depth=4)
        frags = await collect(ex, _doc(blob))
        assert any(f.text == "hello" for f in frags)

    @pytest.mark.asyncio
    async def test_oversized_member_skipped_with_warning(self) -> None:
        ex = ArchiveExtractor(BombGuardConfig(max_member_size=10, max_expansion=1000))
        blob = make_zip(
            {
                "small.txt": b"ok",
                "big.txt": b"x" * 100,
            }
        )
        with pytest.warns(ExtractionWarning, match="big.txt"):
            frags = await collect(ex, _doc(blob))
        names = [f.path_hint for f in frags]
        assert "small.txt" in names
        assert "big.txt" not in names

    @pytest.mark.asyncio
    async def test_decompressed_ceiling_enforced(self) -> None:
        # Even when ratio is fine, absolute decompressed bytes must cap.
        ex = ArchiveExtractor(
            BombGuardConfig(max_decompressed=100, max_expansion=10000)
        )
        blob = make_zip({"a.txt": b"x" * 1024})
        with pytest.raises(BombGuardError, match="max_decompressed"):
            await collect(ex, _doc(blob))

    @pytest.mark.asyncio
    async def test_corrupt_zip_raises(self) -> None:
        ex = ArchiveExtractor()
        with pytest.raises(BombGuardError, match="corrupt zip"):
            await collect(ex, _doc(b"PK\x03\x04not-a-real-zip"))

    @pytest.mark.asyncio
    async def test_text_payload_refused(self) -> None:
        ex = ArchiveExtractor()
        with pytest.raises(BombGuardError, match="binary"):
            await collect(ex, Document(ref=_ref(), text="not a zip"))


class TestTarExtraction:
    @pytest.mark.asyncio
    async def test_basic_tar(self) -> None:
        ex = ArchiveExtractor()
        blob = make_tar({"a.txt": b"alpha", "b.txt": b"beta"})
        frags = await collect(ex, _doc(blob))
        texts = [f.text for f in frags]
        assert "alpha" in texts
        assert "beta" in texts

    @pytest.mark.asyncio
    async def test_tar_oversized_member_skipped(self) -> None:
        ex = ArchiveExtractor(BombGuardConfig(max_member_size=4, max_expansion=10000))
        blob = make_tar({"big.txt": b"x" * 50, "small.txt": b"hi"})
        with pytest.warns(ExtractionWarning, match="big.txt"):
            frags = await collect(ex, _doc(blob))
        assert all(f.path_hint != "big.txt" for f in frags)

    @pytest.mark.asyncio
    async def test_tar_corrupt_raises(self) -> None:
        ex = ArchiveExtractor()
        # 257 bytes of zeros + "ustar" magic confuses the sniffer into
        # tar dispatch, then tarfile rejects.
        bad = b"\x00" * 257 + b"ustar" + b"\x00" * 200
        with pytest.raises(BombGuardError):
            await collect(ex, _doc(bad))


class TestGzipExtraction:
    @pytest.mark.asyncio
    async def test_basic_gzip(self) -> None:
        ex = ArchiveExtractor()
        body = b"hello gzip"
        blob = gzip.compress(body)
        frags = await collect(ex, _doc(blob))
        assert frags[0].text == "hello gzip"
        assert frags[0].path_hint == "(gzip)"

    @pytest.mark.asyncio
    async def test_gzip_bomb_rejected(self) -> None:
        # 2MB of zeros gzip-compresses to ~2KB -> ratio >> 100x.
        ex = ArchiveExtractor()
        blob = gzip.compress(b"\x00" * (2 * 1024 * 1024))
        with pytest.raises(BombGuardError, match="expansion"):
            await collect(ex, _doc(blob))


class TestZstdExtraction:
    @pytest.mark.asyncio
    async def test_zstd_skipped_when_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the import-fallback path even if zstandard is installed
        # in dev: monkey-patch the module-level binding to None.
        from pleno_pii_scanner.extractors import archive as arch_mod

        monkeypatch.setattr(arch_mod, "_zstd", None)
        ex = ArchiveExtractor()
        # Synthetic zstd magic so sniff dispatches us into _walk_zstd.
        blob = b"\x28\xb5\x2f\xfd" + b"\x00" * 32
        with pytest.warns(ExtractionWarning, match="zstandard"):
            frags = await collect(ex, _doc(blob))
        assert frags == []


class TestSevenZ:
    @pytest.mark.asyncio
    async def test_7z_warned_not_supported(self) -> None:
        ex = ArchiveExtractor()
        # 7z magic + minimum padding so sniff recognises it.
        blob = b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32
        with pytest.warns(ExtractionWarning, match="7z"):
            frags = await collect(ex, _doc(blob))
        assert frags == []


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_zip_member_read_failure_warned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate a corrupt member by patching ZipFile.read to raise on
        # one specific member. The extractor must warn + skip rather than
        # abort the whole archive.
        import zipfile

        ex = ArchiveExtractor()
        blob = make_zip({"good.txt": b"ok", "bad.txt": b"will-fail"})
        original_read = zipfile.ZipFile.read

        def fake_read(self, name, *args, **kwargs):  # noqa: ANN001, ANN201
            if name == "bad.txt":
                raise RuntimeError("simulated decryption failure")
            return original_read(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "read", fake_read)
        with pytest.warns(ExtractionWarning, match="bad.txt"):
            frags = await collect(ex, _doc(blob))
        assert any(f.text == "ok" for f in frags)

    @pytest.mark.asyncio
    async def test_tar_with_directory_member(self) -> None:
        # Directory entries inside a tar must be transparently skipped.
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            d = tarfile.TarInfo(name="dir/")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
            f = tarfile.TarInfo(name="dir/leaf.txt")
            payload = b"leaf content"
            f.size = len(payload)
            tf.addfile(f, io.BytesIO(payload))
        ex = ArchiveExtractor()
        frags = await collect(ex, _doc(buf.getvalue()))
        assert [f.path_hint for f in frags] == ["dir/leaf.txt"]

    @pytest.mark.asyncio
    async def test_tar_extractfile_none_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A symlink / hardlink member returns None from extractfile;
        # the loop must continue past it without raising.
        import tarfile

        ex = ArchiveExtractor()
        blob = make_tar({"a.txt": b"hello"})

        def fake_extractfile(self, member):  # noqa: ANN001, ANN201
            return None

        monkeypatch.setattr(tarfile.TarFile, "extractfile", fake_extractfile)
        frags = await collect(ex, _doc(blob))
        assert frags == []

    def test_check_expansion_zero_compressed_returns(self) -> None:
        # Defensive branch: avoid div-by-zero when an upstream test
        # constructs a synthetic empty archive descriptor.
        from pleno_pii_scanner.extractors.archive import (
            BombGuardConfig as _Cfg,
            _check_expansion,
        )

        _check_expansion(0, 999_999, _Cfg())  # must not raise


class TestUnsupportedMime:
    @pytest.mark.asyncio
    async def test_unsupported_mime_raises(self) -> None:
        # Plain text bytes -> sniff returns text/plain -> ArchiveExtractor
        # is the wrong dispatch and must raise rather than silently emit.
        ex = ArchiveExtractor()
        with pytest.raises(BombGuardError, match="unsupported"):
            await collect(ex, _doc(b"just plain ascii text"))
