"""PDF text extraction via pypdfium2.

Optional extra (``pleno-pii-scanner[pdf]``). pypdfium2 ships PDFium as a
native wheel for every supported platform, which means no system-level
poppler/mupdf dependency — important for distroless deployment.

Each PDF page is yielded as a separate fragment with ``path_hint =
"page:N"`` so a finding can be reported against a specific page rather
than just "somewhere in the PDF".
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pleno_pii_scanner.extractors.base import (
    ExtractedFragment,
    ExtractorError,
    doc_payload,
)
from pleno_pii_scanner.sources.base import Document, DocumentChunk


class PdfExtractor:
    """PDF -> per-page text fragments."""

    name = "pdf:pypdfium2"
    accepts = frozenset({"application/pdf"})

    def __init__(self) -> None:
        try:
            import pypdfium2 as pdfium
        except ImportError as exc:
            raise ExtractorError(
                "PdfExtractor requires the [pdf] extra: "
                "pip install pleno-pii-scanner[pdf]"
            ) from exc
        self._pdfium = pdfium

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        payload = doc_payload(doc)
        if isinstance(payload, str):
            raise ExtractorError("PdfExtractor requires binary payload, got text")
        pdf = self._pdfium.PdfDocument(payload)
        try:
            for page_idx in range(len(pdf)):
                page = pdf[page_idx]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                yield ExtractedFragment(
                    text=text,
                    path_hint=f"page:{page_idx + 1}",
                    byte_offset=None,
                    extractor=self.name,
                )
        finally:
            pdf.close()
