"""Archive extractor (zip / tar / gzip / zstd) with bomb guards.

Two attacker classes drive the design:

1. **Expansion bombs** — 42.zip-style payloads where 1KB on disk inflates
   to many gigabytes of NUL bytes. We track decompressed bytes against
   compressed bytes and reject when the ratio crosses the configured
   limit (default 100x). The check is done *during* extraction so we
   bail before allocating the full decompressed buffer.

2. **Depth bombs** — nested archives (zip-in-zip-in-zip) that defeat
   ratio guards because each layer expands modestly. We hard-cap the
   recursion depth at 8 and refuse to descend further.

Per-member size cap (``max_member_size``) is a separate axis: a single
huge file inside a clean archive is not a bomb but exceeds the per-doc
budget. We skip + warn rather than abort the whole archive so siblings
still get scanned.

zstd is gated on the optional ``zstandard`` package; without it we skip
zstd archives with a warning. gzip and tar are stdlib.
"""

from __future__ import annotations

import gzip
import io
import tarfile
import warnings
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass

from pleno_pii_scanner.extractors.base import (
    BombGuardError,
    ExtractedFragment,
    ExtractionWarning,
    doc_payload,
)
from pleno_pii_scanner.extractors.sniff import sniff
from pleno_pii_scanner.extractors.text import decode_bytes
from pleno_pii_scanner.sources.base import Document, DocumentChunk

try:
    import zstandard as _zstd
except ImportError:
    _zstd = None  # type: ignore[assignment]


DEFAULT_MAX_DEPTH = 8
DEFAULT_MAX_EXPANSION = 100.0
DEFAULT_MAX_MEMBER_SIZE = 50 * 1024 * 1024
# Per-archive decompressed ceiling defends against bombs whose ratio
# stays under DEFAULT_MAX_EXPANSION but whose absolute size would still
# exhaust memory on a small archive (e.g. 10MB compressed -> 900MB).
DEFAULT_MAX_DECOMPRESSED = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BombGuardConfig:
    """Tunable bomb-guard limits.

    Defaults are sized for SOC scanning workloads — operators with
    tighter memory budgets should lower ``max_member_size`` and
    ``max_decompressed`` rather than ``max_expansion`` (the ratio guard
    is the only protection against true zip-bombs).
    """

    max_depth: int = DEFAULT_MAX_DEPTH
    max_expansion: float = DEFAULT_MAX_EXPANSION
    max_member_size: int = DEFAULT_MAX_MEMBER_SIZE
    max_decompressed: int = DEFAULT_MAX_DECOMPRESSED


class ArchiveExtractor:
    """zip/tar/gz/zstd extractor with depth + expansion bomb guards."""

    name = "archive:multi"
    accepts = frozenset(
        {
            "application/zip",
            "application/x-tar",
            "application/gzip",
            "application/zstd",
            "application/vnd.openxmlformats-officedocument.*",
            "application/x-7z-compressed",
        }
    )

    def __init__(self, config: BombGuardConfig | None = None) -> None:
        self._config = config or BombGuardConfig()

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        payload = doc_payload(doc)
        if isinstance(payload, str):
            # An archive served as text/* is almost certainly a connector
            # bug; refuse rather than silently mojibake-decode.
            raise BombGuardError("ArchiveExtractor requires binary payload, got text")
        mime = sniff(payload)
        for fragment in _walk(
            payload,
            mime=mime,
            depth=0,
            base_offset=0,
            base_path="",
            cfg=self._config,
        ):
            yield fragment


def _walk(
    data: bytes,
    *,
    mime: str,
    depth: int,
    base_offset: int,
    base_path: str,
    cfg: BombGuardConfig,
):
    """Generator over fragments for a single archive layer.

    Yields per-member fragments, recursing when a member is itself an
    archive. ``base_offset`` is propagated so a regex hit deep inside a
    nested zip can still report a byte offset relative to the original
    Document — important for line-number recovery in `regex_pass`.
    """
    if depth >= cfg.max_depth:
        raise BombGuardError(f"archive depth {depth} >= max_depth={cfg.max_depth}")

    if mime == "application/gzip":
        yield from _walk_gzip(
            data,
            depth=depth,
            base_offset=base_offset,
            base_path=base_path,
            cfg=cfg,
        )
        return
    if mime == "application/zstd":
        yield from _walk_zstd(
            data,
            depth=depth,
            base_offset=base_offset,
            base_path=base_path,
            cfg=cfg,
        )
        return
    if mime == "application/x-tar":
        yield from _walk_tar(
            data,
            depth=depth,
            base_offset=base_offset,
            base_path=base_path,
            cfg=cfg,
        )
        return
    if mime == "application/zip" or mime.startswith("application/vnd.openxmlformats"):
        yield from _walk_zip(
            data,
            depth=depth,
            base_offset=base_offset,
            base_path=base_path,
            cfg=cfg,
        )
        return
    if mime == "application/x-7z-compressed":
        warnings.warn(
            "7z archives are not supported by core ArchiveExtractor; "
            "install pleno-pii-scanner[archive7z] for py7zr support",
            ExtractionWarning,
            stacklevel=2,
        )
        return
    raise BombGuardError(f"unsupported archive MIME: {mime}")


def _check_expansion(compressed: int, decompressed: int, cfg: BombGuardConfig) -> None:
    """Reject when decompressed/compressed > max_expansion."""
    if compressed <= 0:
        return
    ratio = decompressed / compressed
    if ratio > cfg.max_expansion:
        raise BombGuardError(
            f"archive expansion ratio {ratio:.1f}x exceeds "
            f"max_expansion={cfg.max_expansion:.1f}x "
            f"(compressed={compressed}, decompressed={decompressed})"
        )
    if decompressed > cfg.max_decompressed:
        raise BombGuardError(
            f"archive decompressed size {decompressed} exceeds "
            f"max_decompressed={cfg.max_decompressed}"
        )


def _walk_zip(
    data: bytes,
    *,
    depth: int,
    base_offset: int,
    base_path: str,
    cfg: BombGuardConfig,
):
    compressed_total = len(data)
    decompressed_total = 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BombGuardError(f"corrupt zip: {exc}") from exc
    with zf:
        # Pre-flight: sum declared file_size and check ratio before we
        # start reading any member. Defends against bombs whose
        # individual members are small but whose total expansion is
        # catastrophic. We still re-check post-read because zip headers
        # can lie.
        declared = sum(info.file_size for info in zf.infolist())
        _check_expansion(compressed_total, declared, cfg)

        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.file_size > cfg.max_member_size:
                warnings.warn(
                    f"skipping zip member {info.filename!r} "
                    f"(size={info.file_size} > max_member_size="
                    f"{cfg.max_member_size})",
                    ExtractionWarning,
                    stacklevel=2,
                )
                continue
            try:
                member_bytes = zf.read(info.filename)
            except (zipfile.BadZipFile, RuntimeError) as exc:
                warnings.warn(
                    f"failed to read zip member {info.filename!r}: {exc}",
                    ExtractionWarning,
                    stacklevel=2,
                )
                continue
            decompressed_total += len(member_bytes)
            _check_expansion(compressed_total, decompressed_total, cfg)
            yield from _yield_member(
                name=info.filename,
                data=member_bytes,
                depth=depth,
                base_offset=base_offset,
                base_path=base_path,
                cfg=cfg,
            )


def _walk_tar(
    data: bytes,
    *,
    depth: int,
    base_offset: int,
    base_path: str,
    cfg: BombGuardConfig,
):
    compressed_total = len(data)
    decompressed_total = 0
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except tarfile.TarError as exc:
        raise BombGuardError(f"corrupt tar: {exc}") from exc
    with tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            if member.size > cfg.max_member_size:
                warnings.warn(
                    f"skipping tar member {member.name!r} "
                    f"(size={member.size} > max_member_size="
                    f"{cfg.max_member_size})",
                    ExtractionWarning,
                    stacklevel=2,
                )
                continue
            f = tf.extractfile(member)
            if f is None:
                continue
            member_bytes = f.read()
            decompressed_total += len(member_bytes)
            _check_expansion(compressed_total, decompressed_total, cfg)
            yield from _yield_member(
                name=member.name,
                data=member_bytes,
                depth=depth,
                base_offset=base_offset,
                base_path=base_path,
                cfg=cfg,
            )


def _walk_gzip(
    data: bytes,
    *,
    depth: int,
    base_offset: int,
    base_path: str,
    cfg: BombGuardConfig,
):
    """Stream-decompress with a hard ceiling so bombs cannot OOM us."""
    compressed_total = len(data)
    bio = io.BytesIO(data)
    out = bytearray()
    with gzip.GzipFile(fileobj=bio, mode="rb") as gz:
        while True:
            chunk = gz.read(64 * 1024)
            if not chunk:
                break
            out.extend(chunk)
            _check_expansion(compressed_total, len(out), cfg)
    yield from _yield_member(
        name="(gzip)",
        data=bytes(out),
        depth=depth,
        base_offset=base_offset,
        base_path=base_path,
        cfg=cfg,
    )


def _walk_zstd(
    data: bytes,
    *,
    depth: int,
    base_offset: int,
    base_path: str,
    cfg: BombGuardConfig,
):
    if _zstd is None:
        warnings.warn(
            "zstandard archives skipped: install zstandard package",
            ExtractionWarning,
            stacklevel=2,
        )
        return
    # Real-zstd path is exercised only when the optional `zstandard`
    # wheel is installed. Coverage is enforced by the dedicated extra
    # `pleno-pii-scanner-archive-zstd` test job, so we exclude the
    # decompress loop from the core 100% gate.
    compressed_total = len(data)  # pragma: no cover
    dctx = _zstd.ZstdDecompressor(  # pragma: no cover
        max_window_size=2**31,
    )
    out = bytearray()  # pragma: no cover
    with dctx.stream_reader(io.BytesIO(data)) as reader:  # pragma: no cover
        while True:  # pragma: no cover
            chunk = reader.read(64 * 1024)  # pragma: no cover
            if not chunk:  # pragma: no cover
                break  # pragma: no cover
            out.extend(chunk)  # pragma: no cover
            _check_expansion(compressed_total, len(out), cfg)  # pragma: no cover
    yield from _yield_member(  # pragma: no cover
        name="(zstd)",
        data=bytes(out),
        depth=depth,
        base_offset=base_offset,
        base_path=base_path,
        cfg=cfg,
    )


def _yield_member(
    *,
    name: str,
    data: bytes,
    depth: int,
    base_offset: int,
    base_path: str,
    cfg: BombGuardConfig,
):
    """Either recurse into a nested archive or emit a text fragment.

    We re-sniff each member rather than trusting the file extension —
    attackers commonly disguise nested zips as `.txt` to evade naive
    extension-based filters.
    """
    member_path = f"{base_path}/{name}" if base_path else name
    member_mime = sniff(data)
    if _is_archive(member_mime):
        yield from _walk(
            data,
            mime=member_mime,
            depth=depth + 1,
            base_offset=base_offset,
            base_path=member_path,
            cfg=cfg,
        )
        return
    yield ExtractedFragment(
        text=decode_bytes(data),
        path_hint=member_path,
        byte_offset=base_offset,
        extractor=f"archive:{member_mime}",
    )


def _is_archive(mime: str) -> bool:
    return mime in {
        "application/zip",
        "application/x-tar",
        "application/gzip",
        "application/zstd",
        "application/x-7z-compressed",
    } or mime.startswith("application/vnd.openxmlformats")
