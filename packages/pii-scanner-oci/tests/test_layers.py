"""Tests for streaming layer extraction (gzip + zstd + plain tar)."""

from __future__ import annotations

import gzip
import io
import tarfile

import pytest
import zstandard

from pleno_pii_scanner_oci.layers import (
    GZIP_LAYER_TYPES,
    PLAIN_TAR_TYPES,
    ZSTD_LAYER_TYPES,
    iter_layer_members,
)


def _build_tarball(*entries: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, body in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


def _build_dir_tarball() -> bytes:
    """Tarball containing one directory and one regular file.

    Used to confirm `iter_layer_members` skips directories.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        d = tarfile.TarInfo(name="etc")
        d.type = tarfile.DIRTYPE
        tar.addfile(d)
        f = tarfile.TarInfo(name="etc/passwd")
        f.size = 5
        tar.addfile(f, io.BytesIO(b"root\n"))
    return buf.getvalue()


class TestGzipLayer:
    def test_yields_regular_files(self) -> None:
        tar_bytes = _build_tarball(
            ("app/secret.txt", b"password=hunter2\n"),
            ("app/readme.md", b"# hello\n"),
        )
        gz = gzip.compress(tar_bytes)
        media_type = next(iter(GZIP_LAYER_TYPES))
        members = list(iter_layer_members(media_type, gz))
        paths = {m.path for m in members}
        assert paths == {"app/secret.txt", "app/readme.md"}

    def test_skips_directories(self) -> None:
        tar_bytes = _build_dir_tarball()
        gz = gzip.compress(tar_bytes)
        members = list(
            iter_layer_members("application/vnd.oci.image.layer.v1.tar+gzip", gz)
        )
        # Only the regular file, not the directory.
        assert [m.path for m in members] == ["etc/passwd"]
        assert members[0].body == b"root\n"


class TestZstdLayer:
    def test_yields_regular_files(self) -> None:
        tar_bytes = _build_tarball(
            ("app/config.json", b'{"k":"v"}\n'),
        )
        cctx = zstandard.ZstdCompressor()
        compressed = cctx.compress(tar_bytes)
        media_type = next(iter(ZSTD_LAYER_TYPES))
        members = list(iter_layer_members(media_type, compressed))
        assert len(members) == 1
        assert members[0].path == "app/config.json"


class TestPlainTar:
    def test_yields_regular_files(self) -> None:
        tar_bytes = _build_tarball(("note.txt", b"hello\n"))
        media_type = next(iter(PLAIN_TAR_TYPES))
        members = list(iter_layer_members(media_type, tar_bytes))
        assert members[0].path == "note.txt"


class TestSizeCap:
    def test_max_member_bytes_skips_oversize(self) -> None:
        big = b"x" * 1024
        small = b"y" * 64
        tar_bytes = _build_tarball(("big.bin", big), ("small.bin", small))
        gz = gzip.compress(tar_bytes)
        members = list(
            iter_layer_members(
                "application/vnd.oci.image.layer.v1.tar+gzip",
                gz,
                max_member_bytes=128,
            )
        )
        # big.bin (1024 bytes) skipped; small.bin (64 bytes) yielded.
        paths = {m.path for m in members}
        assert paths == {"small.bin"}


class TestUnsupportedMediaType:
    def test_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="unsupported layer media-type"):
            list(iter_layer_members("application/x-bogus", b""))
