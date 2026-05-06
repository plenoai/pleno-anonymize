"""Tests for the ExtractorRegistry, Protocol, and ExtractedFragment."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pleno_pii_scanner.extractors import (
    ExtractedFragment,
    Extractor,
    ExtractorRegistry,
    UnknownExtractorError,
    collect,
    doc_payload,
    for_mime,
    iter_extractors,
    patterns,
    register,
)
from pleno_pii_scanner.extractors import base as _base_mod
from pleno_pii_scanner.extractors.base import ExtractorError
from pleno_pii_scanner.sources.base import Document, DocumentRef


def _ref(**overrides: object) -> DocumentRef:
    base: dict[str, object] = dict(source_id="t:s", source_kind="t", path="p")
    base.update(overrides)
    return DocumentRef(**base)  # type: ignore[arg-type]


class _FakeExtractor:
    def __init__(
        self, name: str = "fake", patterns_: frozenset[str] = frozenset()
    ) -> None:
        self.name = name
        self.accepts = patterns_

    async def extract(self, doc: Document) -> AsyncIterator[ExtractedFragment]:
        yield ExtractedFragment(
            text="x", path_hint="hint", byte_offset=0, extractor=self.name
        )


class TestExtractedFragment:
    def test_is_immutable(self) -> None:
        f = ExtractedFragment(text="t", path_hint="h", byte_offset=0, extractor="x")
        with pytest.raises((AttributeError, TypeError)):
            f.text = "other"  # type: ignore[misc]

    def test_byte_offset_can_be_none(self) -> None:
        f = ExtractedFragment(text="t", path_hint="h", byte_offset=None, extractor="x")
        assert f.byte_offset is None


class TestExtractorRegistry:
    def test_register_and_for_mime_exact_match(self) -> None:
        r = ExtractorRegistry()
        e = _FakeExtractor()
        r.register("text/plain", e)
        assert r.for_mime("text/plain") is e

    def test_glob_match(self) -> None:
        r = ExtractorRegistry()
        e = _FakeExtractor("text-star")
        r.register("text/*", e)
        assert r.for_mime("text/plain") is e
        assert r.for_mime("text/markdown") is e
        assert r.for_mime("text/html") is e

    def test_specificity_wins(self) -> None:
        # text/html (specific) must win over text/* (glob) even if the
        # glob was registered second.
        r = ExtractorRegistry()
        generic = _FakeExtractor("generic")
        specific = _FakeExtractor("specific")
        r.register("text/html", specific)
        r.register("text/*", generic)
        assert r.for_mime("text/html") is specific
        assert r.for_mime("text/plain") is generic

    def test_specificity_tie_break_on_pattern_length(self) -> None:
        r = ExtractorRegistry()
        a = _FakeExtractor("a")
        b = _FakeExtractor("b")
        r.register("text/x-*", a)
        r.register("text/*", b)
        assert r.for_mime("text/x-yaml") is a
        assert r.for_mime("text/plain") is b

    def test_unknown_mime_raises(self) -> None:
        r = ExtractorRegistry()
        with pytest.raises(UnknownExtractorError):
            r.for_mime("application/x-mystery")

    def test_unknown_is_keyerror_compatible(self) -> None:
        r = ExtractorRegistry()
        with pytest.raises(KeyError):
            r.for_mime("application/x-mystery")

    def test_register_replaces_same_pattern(self) -> None:
        # Re-registering the same pattern updates rather than duplicates.
        r = ExtractorRegistry()
        first = _FakeExtractor("first")
        second = _FakeExtractor("second")
        r.register("text/plain", first)
        r.register("text/plain", second)
        assert r.for_mime("text/plain") is second
        assert r.patterns().count("text/plain") == 1

    def test_clear(self) -> None:
        r = ExtractorRegistry()
        r.register("text/*", _FakeExtractor())
        r.clear()
        assert r.patterns() == ()
        with pytest.raises(UnknownExtractorError):
            r.for_mime("text/plain")

    def test_patterns_returns_all(self) -> None:
        r = ExtractorRegistry()
        r.register("text/*", _FakeExtractor("a"))
        r.register("application/zip", _FakeExtractor("b"))
        assert set(r.patterns()) == {"text/*", "application/zip"}


class TestGlobalRegistry:
    @pytest.fixture(autouse=True)
    def _isolate(self) -> None:
        _base_mod._reset_for_tests()
        yield
        _base_mod._reset_for_tests()

    def test_register_and_for_mime(self) -> None:
        e = _FakeExtractor()
        register("text/plain", e)
        assert for_mime("text/plain") is e

    def test_patterns(self) -> None:
        register("text/*", _FakeExtractor())
        assert "text/*" in patterns()

    def test_iter_extractors(self) -> None:
        e = _FakeExtractor()
        register("text/*", e)
        items = list(iter_extractors())
        assert items == [("text/*", e)]


class TestProtocol:
    def test_runtime_isinstance_accepts_compliant(self) -> None:
        assert isinstance(_FakeExtractor(), Extractor)

    def test_runtime_isinstance_rejects_missing(self) -> None:
        class Bad:
            name = "bad"

        assert not isinstance(Bad(), Extractor)


class TestDocPayload:
    def test_text_returned(self) -> None:
        d = Document(ref=_ref(), text="hi")
        assert doc_payload(d) == "hi"

    def test_binary_returned(self) -> None:
        d = Document(ref=_ref(), binary=b"\x00\x01")
        assert doc_payload(d) == b"\x00\x01"

    def test_neither_raises(self) -> None:
        # Document.__post_init__ blocks this, so we synthesise via __new__
        # to verify defence in depth.
        d = object.__new__(Document)
        object.__setattr__(d, "ref", _ref())
        object.__setattr__(d, "text", None)
        object.__setattr__(d, "binary", None)
        object.__setattr__(d, "fetched_at", None)
        object.__setattr__(d, "content_hash", None)
        object.__setattr__(d, "created_by", None)
        object.__setattr__(d, "extra", {})
        with pytest.raises(ExtractorError):
            doc_payload(d)


@pytest.mark.asyncio
async def test_collect_materializes_stream() -> None:
    e = _FakeExtractor("c")
    out = await collect(e, Document(ref=_ref(), text="x"))
    assert len(out) == 1
    assert out[0].extractor == "c"
