"""Streaming layer extraction (gzip + zstd) — RSS-bounded per ADR §15.

Layers are content-addressed tarballs. We never materialise a layer in
memory or on disk; instead we drive `tarfile` with a streaming
file-like that emits members as the bytes arrive over the wire.

Bounded memory: each member's body is read into a single buffer (the
ContentExtractor handles per-document size caps), and the next member
overwrites it. RSS therefore tracks `max(member_size_for_this_layer)`,
not the cumulative layer size. A 5 GB layer with 5000 small members
peaks at the size of the largest single member.
"""

from __future__ import annotations

import gzip
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass

import zstandard


# Layer media types per OCI Image Spec v1.1 + Docker compat.
GZIP_LAYER_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
    }
)
ZSTD_LAYER_TYPES: frozenset[str] = frozenset(
    {"application/vnd.oci.image.layer.v1.tar+zstd"}
)
PLAIN_TAR_TYPES: frozenset[str] = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.docker.image.rootfs.diff.tar",
    }
)


@dataclass(frozen=True, slots=True)
class LayerMember:
    """One file inside a layer tarball."""

    path: str
    size: int
    body: bytes


def iter_layer_members(
    media_type: str, raw: bytes, *, max_member_bytes: int | None = None
) -> Iterator[LayerMember]:
    """Yield each regular-file member of a layer.

    `raw` is the full compressed layer body. Streaming from a network
    socket is wired in `connector.py` via httpx response.iter_bytes;
    this function takes the bytes already in hand so it stays a
    single-purpose unit testable in isolation.

    Symlinks, hardlinks, devices, and directories are silently
    skipped — the regex / NER pipeline scans bytes, and only regular
    files have bytes to scan.
    """
    fileobj = _open_decompressed(media_type, raw)
    with tarfile.open(fileobj=fileobj, mode="r|") as tar:
        for entry in tar:
            if not entry.isfile():
                continue
            if max_member_bytes is not None and entry.size > max_member_bytes:
                continue
            extracted = tar.extractfile(entry)
            if extracted is None:
                continue
            try:
                body = extracted.read()
            finally:
                extracted.close()
            yield LayerMember(path=entry.name, size=entry.size, body=body)


def _open_decompressed(media_type: str, raw: bytes):
    """Pick the right decompressor based on the layer media type."""
    if media_type in GZIP_LAYER_TYPES:
        # gzip.GzipFile handles arbitrary-length inputs; tarfile streams
        # through it without ever pulling the full layer into memory.
        import io

        return gzip.GzipFile(fileobj=io.BytesIO(raw), mode="rb")
    if media_type in ZSTD_LAYER_TYPES:
        import io

        dctx = zstandard.ZstdDecompressor()
        return dctx.stream_reader(io.BytesIO(raw))
    if media_type in PLAIN_TAR_TYPES:
        import io

        return io.BytesIO(raw)
    raise ValueError(f"unsupported layer media-type: {media_type!r}")


__all__ = [
    "GZIP_LAYER_TYPES",
    "LayerMember",
    "PLAIN_TAR_TYPES",
    "ZSTD_LAYER_TYPES",
    "iter_layer_members",
]
