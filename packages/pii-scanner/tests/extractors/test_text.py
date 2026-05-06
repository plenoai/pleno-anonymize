"""Tests for the text/* passthrough extractor + charset decode."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.extractors import collect
from pleno_pii_scanner.extractors.text import TextExtractor, decode_bytes
from pleno_pii_scanner.sources.base import Document, DocumentRef


def _ref() -> DocumentRef:
    return DocumentRef(source_id="s", source_kind="t", path="p")


class TestTextExtractor:
    @pytest.mark.asyncio
    async def test_text_passthrough(self) -> None:
        ex = TextExtractor()
        frags = await collect(ex, Document(ref=_ref(), text="hello"))
        assert len(frags) == 1
        assert frags[0].text == "hello"
        assert frags[0].extractor == "text:passthrough"

    @pytest.mark.asyncio
    async def test_binary_utf8_decoded(self) -> None:
        ex = TextExtractor()
        frags = await collect(
            ex, Document(ref=_ref(), binary="こんにちは".encode("utf-8"))
        )
        assert frags[0].text == "こんにちは"

    @pytest.mark.asyncio
    async def test_binary_shift_jis_decoded(self) -> None:
        ex = TextExtractor()
        # charset-normalizer must pick a Japanese-capable codec; we only
        # assert the content round-trips, not the specific codec name.
        sjis = "山田太郎の電話番号".encode("shift_jis")
        frags = await collect(ex, Document(ref=_ref(), binary=sjis))
        assert "山田太郎" in frags[0].text or "電話" in frags[0].text

    @pytest.mark.asyncio
    async def test_empty_binary_returns_empty(self) -> None:
        ex = TextExtractor()
        # Document XOR rules out empty Document with both None, but text=""
        # is a legitimate "empty file" case.
        frags = await collect(ex, Document(ref=_ref(), text=""))
        assert frags[0].text == ""

    @pytest.mark.asyncio
    async def test_byte_offset_is_zero(self) -> None:
        ex = TextExtractor()
        frags = await collect(ex, Document(ref=_ref(), text="abc"))
        assert frags[0].byte_offset == 0
        assert frags[0].path_hint == ""


class TestDecodeBytes:
    def test_empty_returns_empty(self) -> None:
        assert decode_bytes(b"") == ""

    def test_utf8_round_trip(self) -> None:
        assert decode_bytes("hello".encode()) == "hello"

    def test_garbage_falls_back_to_replace(
        self, recwarn: pytest.WarningsRecorder
    ) -> None:
        # A short fully-random byte string defeats charset detection;
        # the decoder must still return a usable str rather than raise.
        # Repeated 0xFF is not valid UTF-8 nor any common encoding's
        # high-bit-only sequence — charset-normalizer typically still
        # picks a fallback, but if it gives up we exercise the warning.
        result = decode_bytes(b"\xff\xfe\xfd\xfc\xfb")
        assert isinstance(result, str)

    def test_no_match_path_warns_and_replaces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # charset-normalizer almost always finds *something*; we force
        # the no-match path by stubbing the function. Verifies the
        # ExtractionWarning emission and the utf-8/replace fallback.
        from pleno_pii_scanner.extractors import text as text_mod

        class _NoneMatch:
            def best(self):  # noqa: ANN201
                return None

        def fake_from_bytes(_data, **_kwargs):  # noqa: ANN001, ANN201
            return _NoneMatch()

        monkeypatch.setattr(text_mod, "from_bytes", fake_from_bytes)
        with pytest.warns():
            out = text_mod.decode_bytes(b"\xff\xfe")
        assert isinstance(out, str)
