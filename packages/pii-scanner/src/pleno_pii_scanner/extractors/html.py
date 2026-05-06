"""HTML -> text extraction via the stdlib HTMLParser.

We deliberately avoid ``markdownify`` / ``beautifulsoup4`` to keep core
install dep-free per ADR-0007 §6 (PDF/Office/columnar are extras, HTML is
not). Stdlib ``html.parser`` handles the HTML5 tokenizer well enough for
PII-scanning purposes — we just need readable text from <p>, <div>,
<a>, etc., and to drop <script>, <style>, <noscript>, and HTML comments
which are common XSS-payload / config-leak hideouts but contain no human
text we want to scan.

Charset is decoded the same way as text/plain, but we also honour an
explicit ``<meta charset>`` if present so we don't mis-decode pages that
declare Shift_JIS or EUC-JP.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from html.parser import HTMLParser

from pleno_pii_scanner.extractors.base import (
    ExtractedFragment,
    doc_payload,
)
from pleno_pii_scanner.extractors.text import decode_bytes
from pleno_pii_scanner.sources.base import Document, DocumentChunk

_SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

# Coarse meta-charset extractor — full HTML5 parsing of meta tags is not
# worth the complexity when 99% of real pages match this regex.
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE,
)


class _TextCollector(HTMLParser):
    """Stateful collector that drops script/style and inserts whitespace."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        # Block-level tags get a newline so adjacent <p>s don't run together.
        elif tag in {
            "p",
            "br",
            "div",
            "li",
            "tr",
            "td",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "pre",
        }:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/>, <img/>, etc. — treat <br/> as a line break, ignore others.
        if tag == "br":
            self._buf.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.append(data)

    def text(self) -> str:
        return "".join(self._buf)


class HtmlExtractor:
    """text/html -> plain text using stdlib HTMLParser."""

    name = "html:stdlib"
    accepts = frozenset({"text/html", "application/xhtml+xml"})

    async def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        payload = doc_payload(doc)
        if isinstance(payload, bytes):
            charset = _detect_meta_charset(payload)
            if charset:
                try:
                    raw = payload.decode(charset, errors="replace")
                except LookupError:
                    raw = decode_bytes(payload)
            else:
                raw = decode_bytes(payload)
        else:
            raw = payload
        collector = _TextCollector()
        collector.feed(raw)
        collector.close()
        yield ExtractedFragment(
            text=collector.text(),
            path_hint="",
            byte_offset=0,
            extractor=self.name,
        )


def _detect_meta_charset(data: bytes) -> str | None:
    """Scan the first 4KB for a <meta charset=…> declaration."""
    m = _META_CHARSET_RE.search(data[:4096])
    if not m:
        return None
    return m.group(1).decode("ascii", errors="ignore")
