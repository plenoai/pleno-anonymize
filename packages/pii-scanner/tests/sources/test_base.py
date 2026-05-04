"""Tests for the SourceConnector / Document type contract."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    DocumentChunk,
    DocumentRef,
    Principal,
    SourceConnector,
    SourceFilter,
)


def _ref(**overrides: object) -> DocumentRef:
    base: dict[str, object] = dict(
        source_id="test:src",
        source_kind="test",
        path="path/to/doc",
    )
    base.update(overrides)
    return DocumentRef(**base)  # type: ignore[arg-type]


class TestDocumentRef:
    def test_fingerprint_is_stable_across_construction(self) -> None:
        a = _ref()
        b = _ref()
        assert a.fingerprint() == b.fingerprint()

    def test_fingerprint_changes_with_path(self) -> None:
        a = _ref(path="a")
        b = _ref(path="b")
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_changes_with_source_id(self) -> None:
        a = _ref(source_id="src1")
        b = _ref(source_id="src2")
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_changes_with_source_kind(self) -> None:
        a = _ref(source_kind="github")
        b = _ref(source_kind="gitlab")
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_includes_etag_when_present(self) -> None:
        a = _ref(etag=None)
        b = _ref(etag="abc123")
        assert a.fingerprint() != b.fingerprint()

    def test_fingerprint_length_is_stable(self) -> None:
        # 32 hex chars = 128 bits — enough for FindingsStore dedup keys
        # without bloating Postgres index size.
        assert len(_ref().fingerprint()) == 32

    def test_is_immutable(self) -> None:
        ref = _ref()
        with pytest.raises((AttributeError, TypeError)):
            ref.path = "other"  # type: ignore[misc]


class TestDocument:
    def test_text_only_is_valid(self) -> None:
        d = Document(ref=_ref(), text="hello")
        assert d.text == "hello"
        assert d.binary is None

    def test_binary_only_is_valid(self) -> None:
        d = Document(ref=_ref(), binary=b"\x00\x01")
        assert d.binary == b"\x00\x01"
        assert d.text is None

    def test_neither_text_nor_binary_raises(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            Document(ref=_ref())

    def test_both_text_and_binary_raises(self) -> None:
        # Defends downstream code that does `if doc.text is not None`
        # against silent bugs where a connector populates both.
        with pytest.raises(ValueError, match="exactly one"):
            Document(ref=_ref(), text="x", binary=b"x")

    def test_carries_principal(self) -> None:
        p = Principal(id="u1", display_name="Alice", email="a@example.com")
        d = Document(ref=_ref(), text="x", created_by=p)
        assert d.created_by == p


class TestDocumentChunk:
    def test_byte_range_must_be_ordered(self) -> None:
        with pytest.raises(ValueError, match="byte_range"):
            DocumentChunk(
                ref=_ref(),
                byte_range=(100, 50),
                is_final=False,
                text="x",
            )

    def test_byte_range_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError, match="byte_range"):
            DocumentChunk(
                ref=_ref(),
                byte_range=(-1, 10),
                is_final=False,
                text="x",
            )

    def test_text_xor_binary_invariant(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            DocumentChunk(
                ref=_ref(),
                byte_range=(0, 10),
                is_final=False,
                text="x",
                binary=b"x",
            )

    def test_streaming_sequence_orders_by_byte_range(self) -> None:
        # Connectors must yield chunks in byte order; the pipeline relies
        # on this for the overlap window. We don't enforce ordering here
        # (that's the connector's contract) but we verify the data shape
        # is suitable for it.
        chunks = [
            DocumentChunk(ref=_ref(), byte_range=(0, 1023), is_final=False, binary=b"a" * 1024),
            DocumentChunk(ref=_ref(), byte_range=(1024, 2047), is_final=True, binary=b"b" * 1024),
        ]
        assert sorted(chunks, key=lambda c: c.byte_range[0]) == chunks
        assert chunks[-1].is_final


class TestSourceFilter:
    def test_defaults_match_no_constraints(self) -> None:
        f = SourceFilter()
        assert f.include == ()
        assert f.exclude == ()
        assert f.since is None
        assert f.max_size is None

    def test_carries_since(self) -> None:
        ts = datetime(2026, 5, 4, tzinfo=UTC)
        assert SourceFilter(since=ts).since == ts


class TestCapabilities:
    def test_defaults_are_conservative(self) -> None:
        # A connector that doesn't override these should not be assumed
        # to support incremental scan or binary handling — defaults
        # produce correct (if slow) behavior for any source.
        c = Capabilities()
        assert c.incremental is False
        assert c.binary is False
        assert c.content_hash_delta is False
        assert c.streaming is False
        assert c.max_concurrent_fetches == 8


class _FakeConnector:
    """Minimal SourceConnector implementation for protocol-compliance tests."""

    id = "fake:1"
    kind = "fake"

    def __init__(self) -> None:
        self.closed = False

    async def discover(
        self,
        filter: SourceFilter,
        cursor: str | None,
    ) -> AsyncIterator[DocumentRef]:
        yield _ref(path="a")
        yield _ref(path="b")

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        yield Document(ref=ref, text=f"body of {ref.path}")

    def capabilities(self) -> Capabilities:
        return Capabilities(incremental=True)

    async def close(self) -> None:
        self.closed = True


class TestSourceConnectorProtocol:
    def test_runtime_isinstance_accepts_compliant_class(self) -> None:
        # runtime_checkable Protocol lets the registry validate plugins
        # without importing them at type-check time.
        c = _FakeConnector()
        assert isinstance(c, SourceConnector)

    def test_runtime_isinstance_rejects_missing_methods(self) -> None:
        class Incomplete:
            id = "x"
            kind = "x"

        assert not isinstance(Incomplete(), SourceConnector)

    @pytest.mark.asyncio
    async def test_discover_yields_refs(self) -> None:
        c = _FakeConnector()
        refs = [r async for r in c.discover(SourceFilter(), None)]
        assert [r.path for r in refs] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_fetch_yields_document(self) -> None:
        c = _FakeConnector()
        ref = _ref(path="a")
        chunks = [d async for d in c.fetch(ref)]
        assert len(chunks) == 1
        d = chunks[0]
        assert isinstance(d, Document)
        assert d.text == "body of a"

    @pytest.mark.asyncio
    async def test_close_releases_resources(self) -> None:
        c = _FakeConnector()
        await c.close()
        assert c.closed is True


class TestPrincipal:
    def test_minimal_construction(self) -> None:
        p = Principal(id="u1")
        assert p.id == "u1"
        assert p.display_name is None
        assert p.email is None
