"""Fixture builders for extractor tests.

We construct bomb / archive / HTML payloads in-memory rather than
committing binary blobs because (a) git diffs are unreadable for binary
files and (b) the bomb size we want (1KB compressed -> 1MB+ inflated)
is trivial to generate from constants.
"""

from __future__ import annotations

import io
import tarfile
import zipfile


def make_zip(members: dict[str, bytes]) -> bytes:
    """Build an in-memory zip archive."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def make_tar(members: dict[str, bytes]) -> bytes:
    """Build an in-memory tar archive (uncompressed)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def make_zip_bomb(payload_size: int = 1024 * 1024) -> bytes:
    """Build a high-ratio zip bomb.

    A single member of ``payload_size`` NUL bytes compresses to roughly
    ``payload_size/1000`` so the expansion ratio comfortably exceeds the
    100x guard while the on-disk archive stays tiny.
    """
    return make_zip({"bomb.bin": b"\x00" * payload_size})


def make_nested_zip_bomb(depth: int) -> bytes:
    """Build a depth-bomb: ``depth`` levels of zip-in-zip.

    The innermost member is a small text payload. Each outer layer wraps
    the previous bytes as a single file, so total compressed size grows
    only modestly while archive depth grows linearly.
    """
    inner = b"hello"
    blob = make_zip({"leaf.txt": inner})
    for level in range(depth - 1):
        blob = make_zip({f"layer{level}.zip": blob})
    return blob
