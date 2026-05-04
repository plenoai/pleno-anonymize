"""Tests for the Office extractors (skipped when [office] not installed)."""

from __future__ import annotations

import io

import pytest

docx_mod = pytest.importorskip("docx")
openpyxl = pytest.importorskip("openpyxl")
pptx_mod = pytest.importorskip("pptx")

from pleno_pii_scanner.extractors import collect
from pleno_pii_scanner.extractors.office import (
    DocxExtractor,
    PptxExtractor,
    XlsxExtractor,
)
from pleno_pii_scanner.sources.base import Document, DocumentRef


def _ref() -> DocumentRef:
    return DocumentRef(source_id="s", source_kind="t", path="p")


def _make_docx(paragraphs: list[str]) -> bytes:
    doc = docx_mod.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_xlsx(rows: list[list[object]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_pptx(slide_texts: list[str]) -> bytes:
    prs = pptx_mod.Presentation()
    for text in slide_texts:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        title = slide.shapes.title
        title.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


class TestDocxExtractor:
    @pytest.mark.asyncio
    async def test_paragraphs_extracted(self) -> None:
        ex = DocxExtractor()
        blob = _make_docx(["alpha", "beta", "gamma"])
        frags = await collect(ex, Document(ref=_ref(), binary=blob))
        texts = [f.text for f in frags]
        assert "alpha" in texts
        assert "beta" in texts
        assert "gamma" in texts

    @pytest.mark.asyncio
    async def test_empty_paragraphs_skipped(self) -> None:
        ex = DocxExtractor()
        blob = _make_docx(["a", "", "b"])
        frags = await collect(ex, Document(ref=_ref(), binary=blob))
        texts = [f.text for f in frags]
        assert "" not in texts


class TestXlsxExtractor:
    @pytest.mark.asyncio
    async def test_string_cells_only(self) -> None:
        ex = XlsxExtractor()
        blob = _make_xlsx(
            [
                ["name", "age"],
                ["alice", 30],
                ["bob", 25],
            ]
        )
        frags = await collect(ex, Document(ref=_ref(), binary=blob))
        texts = [f.text for f in frags]
        assert "alice" in texts
        assert "bob" in texts
        assert "30" not in texts
        assert "25" not in texts

    @pytest.mark.asyncio
    async def test_path_hint_carries_sheet_and_cell(self) -> None:
        ex = XlsxExtractor()
        blob = _make_xlsx([["hello"]])
        frags = await collect(ex, Document(ref=_ref(), binary=blob))
        assert frags[0].path_hint.startswith("sheet:")
        assert "cell:A1" in frags[0].path_hint


class TestPptxExtractor:
    @pytest.mark.asyncio
    async def test_slide_text_extracted(self) -> None:
        ex = PptxExtractor()
        blob = _make_pptx(["title-one", "title-two"])
        frags = await collect(ex, Document(ref=_ref(), binary=blob))
        texts = [f.text for f in frags]
        assert "title-one" in texts
        assert "title-two" in texts
