"""Tests for the HTML -> text extractor."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.extractors import collect
from pleno_pii_scanner.extractors.html import HtmlExtractor
from pleno_pii_scanner.sources.base import Document, DocumentRef


def _ref() -> DocumentRef:
    return DocumentRef(source_id="s", source_kind="t", path="p")


class TestHtmlExtractor:
    @pytest.mark.asyncio
    async def test_plain_text_extracted(self) -> None:
        ex = HtmlExtractor()
        html = "<html><body><p>hello world</p></body></html>"
        frags = await collect(ex, Document(ref=_ref(), text=html))
        assert "hello world" in frags[0].text

    @pytest.mark.asyncio
    async def test_script_block_dropped(self) -> None:
        ex = HtmlExtractor()
        html = (
            '<html><body><script>secret_token="abc123"</script>'
            "<p>visible</p></body></html>"
        )
        frags = await collect(ex, Document(ref=_ref(), text=html))
        assert "visible" in frags[0].text
        assert "secret_token" not in frags[0].text

    @pytest.mark.asyncio
    async def test_style_block_dropped(self) -> None:
        ex = HtmlExtractor()
        html = "<html><head><style>body{color:red}</style></head><body>v</body></html>"
        frags = await collect(ex, Document(ref=_ref(), text=html))
        assert "color:red" not in frags[0].text
        assert "v" in frags[0].text

    @pytest.mark.asyncio
    async def test_block_tags_insert_newlines(self) -> None:
        ex = HtmlExtractor()
        html = "<p>one</p><p>two</p>"
        frags = await collect(ex, Document(ref=_ref(), text=html))
        assert "one" in frags[0].text
        assert "two" in frags[0].text
        assert "onetwo" not in frags[0].text

    @pytest.mark.asyncio
    async def test_br_inserts_newline(self) -> None:
        ex = HtmlExtractor()
        html = "a<br/>b"
        frags = await collect(ex, Document(ref=_ref(), text=html))
        assert "a" in frags[0].text
        assert "b" in frags[0].text

    @pytest.mark.asyncio
    async def test_charrefs_resolved(self) -> None:
        ex = HtmlExtractor()
        html = "<p>&amp; &lt;b&gt;</p>"
        frags = await collect(ex, Document(ref=_ref(), text=html))
        assert "&" in frags[0].text
        assert "<b>" in frags[0].text

    @pytest.mark.asyncio
    async def test_binary_utf8_decoded(self) -> None:
        ex = HtmlExtractor()
        html = "<p>こんにちは</p>".encode("utf-8")
        frags = await collect(ex, Document(ref=_ref(), binary=html))
        assert "こんにちは" in frags[0].text

    @pytest.mark.asyncio
    async def test_meta_charset_honored(self) -> None:
        ex = HtmlExtractor()
        body = (
            b'<html><head><meta charset="shift_jis"></head>'
            b"<body><p>" + "山田".encode("shift_jis") + b"</p></body></html>"
        )
        frags = await collect(ex, Document(ref=_ref(), binary=body))
        assert "山田" in frags[0].text

    @pytest.mark.asyncio
    async def test_unknown_meta_charset_falls_back(self) -> None:
        ex = HtmlExtractor()
        body = (
            b'<html><head><meta charset="definitely-not-real"></head>'
            b"<body>x</body></html>"
        )
        frags = await collect(ex, Document(ref=_ref(), binary=body))
        assert "x" in frags[0].text

    @pytest.mark.asyncio
    async def test_noscript_dropped(self) -> None:
        ex = HtmlExtractor()
        html = "<noscript>fallback</noscript><p>main</p>"
        frags = await collect(ex, Document(ref=_ref(), text=html))
        assert "main" in frags[0].text
        assert "fallback" not in frags[0].text

    @pytest.mark.asyncio
    async def test_accepts_xhtml(self) -> None:
        # Registry users wire xhtml the same way; verify the extractor's
        # accepts set declares it so registry registration is honest.
        ex = HtmlExtractor()
        assert "application/xhtml+xml" in ex.accepts
        assert "text/html" in ex.accepts
