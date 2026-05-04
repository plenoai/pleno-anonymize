"""text/* passthrough Extractor with charset-normalizer decode.

When ``Document.text`` is already populated the extractor yields a single
fragment unchanged — the connector did the decode work. When the body is
binary (S3 object with declared ``text/plain`` Content-Type but unknown
charset) we run ``charset-normalizer`` 3.x which is pure-Python and ships
no native dependency, unlike ``chardet`` which is unmaintained as of 2024.
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator

from charset_normalizer import from_bytes

from pleno_pii_scanner.extractors.base import (
    ExtractedFragment,
    ExtractionWarning,
    doc_payload,
)
from pleno_pii_scanner.sources.base import Document, DocumentChunk


class TextExtractor:
    """Passthrough for text/*; decodes binary bodies via charset-normalizer."""

    name = "text:passthrough"
    accepts = frozenset({"text/*"})

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        payload = doc_payload(doc)
        if isinstance(payload, str):
            text = payload
        else:
            text = decode_bytes(payload)
        yield ExtractedFragment(
            text=text,
            path_hint="",
            byte_offset=0,
            extractor=self.name,
        )


def decode_bytes(data: bytes) -> str:
    """Decode ``data`` to str via charset-normalizer with replace fallback.

    The library returns a ranked match list; the best match's ``str()``
    is the decoded text. When detection fails entirely (e.g. random
    binary masquerading as text/*) we fall back to ``utf-8`` with
    ``errors="replace"`` so the scanner still runs regex on whatever
    printable bytes are present, instead of crashing the whole document.
    """
    if not data:
        return ""
    matches = from_bytes(
        data,
        cp_isolation=None,
        explain=False,
    )
    best = matches.best()
    if best is None:
        warnings.warn(
            "charset-normalizer could not detect encoding; "
            "falling back to utf-8 errors=replace",
            ExtractionWarning,
            stacklevel=2,
        )
        return data.decode("utf-8", errors="replace")
    return str(best)
