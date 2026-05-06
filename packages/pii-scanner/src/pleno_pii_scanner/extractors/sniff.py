"""Magic-byte MIME sniffer.

`python-magic` is rejected upstream: it requires a libmagic shared library
that is absent from distroless images, alpine, and Windows by default. We
implement a minimum sniffer that recognises every container format the
ContentExtractor registry actually dispatches on, plus a heuristic
text/binary classifier for the long tail.

The sniffer is conservative: when in doubt it returns
`application/octet-stream` so the registry falls back to whatever
`Document.ref.content_type` declared. This keeps connector-supplied MIME
types (S3 ``Content-Type``, GitHub ``X-Content-Type-Options``) as the
primary signal and uses sniff only as a tie-breaker.
"""

from __future__ import annotations

# Surface-area size for sniffing. Aligned with Document.binary[:4096] in
# the registry call sites — most magic numbers live in the first 8 bytes,
# but office/zip central-directory probes need more.
SNIFF_BYTES = 4096

OCTET_STREAM = "application/octet-stream"


def sniff(data: bytes) -> str:
    """Classify ``data`` to a MIME type using leading-byte signatures.

    Order matters: more specific containers (docx = zip + manifest) are
    checked before their parent format (zip).
    """
    if not data:
        return OCTET_STREAM

    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        return "application/zstd"
    if data.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "application/x-7z-compressed"
    if data.startswith(b"BZh"):
        return "application/x-bzip2"
    if data.startswith(b"\xfd7zXZ\x00"):
        return "application/x-xz"
    if data.startswith(b"PAR1") or data[-4:] == b"PAR1":
        # Parquet uses the magic at file head AND tail; we check head and
        # the registry never receives < 4 bytes here.
        return "application/vnd.apache.parquet"
    if data.startswith(b"ORC"):
        return "application/vnd.apache.orc"
    if data.startswith(b"Obj\x01"):
        return "application/vnd.apache.avro"

    if data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06"):
        return _sniff_zip_family(data)

    if _is_tar(data):
        return "application/x-tar"

    if _is_html(data):
        return "text/html"

    if _is_text_likely(data):
        return "text/plain"

    return OCTET_STREAM


def _sniff_zip_family(data: bytes) -> str:
    """Distinguish OOXML / OpenDocument / generic zip.

    OOXML containers all carry ``[Content_Types].xml`` near the start of
    the central directory. We scan the first SNIFF_BYTES for the marker
    rather than parsing the central directory because the latter requires
    seeking from the file tail and we may only hold a prefix.
    """
    if b"word/" in data and b"[Content_Types].xml" in data:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if b"xl/" in data and b"[Content_Types].xml" in data:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if b"ppt/" in data and b"[Content_Types].xml" in data:
        return (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        )
    if data[30:38] == b"mimetype" and b"opendocument" in data[:512]:
        return "application/vnd.oasis.opendocument"
    return "application/zip"


def _is_tar(data: bytes) -> bool:
    """Heuristic: classic ustar magic at offset 257."""
    if len(data) < 265:
        return False
    return data[257:262] == b"ustar"


def _is_html(data: bytes) -> bool:
    head = data[:1024].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _is_text_likely(data: bytes) -> bool:
    """Treat as text when no NUL bytes and control-char ratio is low.

    The 30% threshold matches GNU diff's ``--text`` heuristic and
    correctly classifies UTF-8 / UTF-16-LE / Shift_JIS samples used in
    Japanese codebases without a BOM.
    """
    sample = data[:SNIFF_BYTES]
    if b"\x00" in sample:
        # UTF-16-LE looks NUL-heavy but is rare and the connector usually
        # provides the real Content-Type.
        return False
    if not sample:
        return False
    control = sum(1 for b in sample if b < 0x09 or (0x0E <= b <= 0x1F) or b == 0x7F)
    return control / len(sample) < 0.30
