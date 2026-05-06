"""Adapter unit tests using a stub saas-retriever Connector.

We never touch a real httpx client — the tests pre-populate the
adapter's internal retriever handle so ``_ensure_started`` short-circuits
and no real connector is constructed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Document,
    DocumentRef,
    Principal,
    SourceFilter,
)
from saas_retriever.core import (
    Document as RetrieverDocument,
    DocumentRef as RetrieverDocumentRef,
    Principal as RetrieverPrincipal,
    SourceFilter as RetrieverFilter,
)

from pleno_pii_scanner_saas_retriever import (
    KIND,
    SPEC,
    SaasRetrieverAdapter,
    SaasRetrieverConfig,
    build_connector,
)
from pleno_pii_scanner_saas_retriever.adapter import (
    _to_pii_document,
    _to_pii_ref,
    _to_retriever_filter,
    _to_retriever_ref,
)


# --- stub retriever -----------------------------------------------------


class _StubRetriever:
    """Minimal saas_retriever-shaped Connector for tests."""

    id = "stub-id"
    kind = "stub"

    def __init__(
        self,
        refs: list[RetrieverDocumentRef],
        docs_by_path: dict[str, RetrieverDocument],
    ) -> None:
        self._refs = refs
        self._docs = docs_by_path
        self.received_filter: RetrieverFilter | None = None
        self.received_fetch: list[RetrieverDocumentRef] = []
        self.closed = False

    async def discover(
        self, filter: RetrieverFilter
    ) -> AsyncIterator[RetrieverDocumentRef]:
        self.received_filter = filter
        for ref in self._refs:
            yield ref

    async def fetch(
        self, ref: RetrieverDocumentRef
    ) -> AsyncIterator[RetrieverDocument]:
        self.received_fetch.append(ref)
        yield self._docs[ref.path]

    async def close(self) -> None:
        self.closed = True


def _adapter_with_stub(
    stub: _StubRetriever, *, connector_kind: str = "github"
) -> SaasRetrieverAdapter:
    """Build an adapter that skips real connector startup."""
    adapter = SaasRetrieverAdapter(
        SaasRetrieverConfig(
            connector_kind=connector_kind,
            connector_kwargs={"owner": "acme"},
        )
    )
    adapter._retriever = stub  # type: ignore[assignment]
    return adapter


# --- spec / entry-point sanity -----------------------------------------


def test_spec_uses_known_kind() -> None:
    assert SPEC.kind == KIND == "saas-retriever"
    assert SPEC.factory is build_connector
    assert isinstance(SPEC.capabilities, Capabilities)
    assert SPEC.capabilities.max_concurrent_fetches == 8


def test_capabilities_report_api_concurrency() -> None:
    adapter = _adapter_with_stub(_StubRetriever([], {}))
    caps = adapter.capabilities()
    assert caps.max_concurrent_fetches == 8
    assert caps.incremental is False
    assert caps.streaming is False


# --- config validation --------------------------------------------------


def test_unknown_connector_kind_rejected() -> None:
    with pytest.raises(ValueError, match="unknown connector_kind"):
        SaasRetrieverConfig(connector_kind="not-a-thing", connector_kwargs={})


def test_resolved_id_falls_back_to_distinctive_kwarg() -> None:
    cfg = SaasRetrieverConfig(
        connector_kind="github", connector_kwargs={"owner": "plenoai"}
    )
    assert cfg.resolved_id() == "saas-retriever:github:plenoai"

    cfg_repo = SaasRetrieverConfig(
        connector_kind="github",
        connector_kwargs={"owner": "plenoai", "repo": "saas-retriever"},
    )
    # repo wins over owner because the loop checks `repo` first.
    assert cfg_repo.resolved_id() == "saas-retriever:github:saas-retriever"

    cfg_explicit = SaasRetrieverConfig(
        connector_kind="github",
        connector_kwargs={"owner": "plenoai"},
        id="custom-id",
    )
    assert cfg_explicit.resolved_id() == "custom-id"


def test_build_connector_strips_owner_keys() -> None:
    """build_connector pops keys this wheel owns; rest goes to retriever kwargs."""
    adapter = build_connector(
        {
            "connector_kind": "github",
            "owner": "plenoai",
            "id": "my-id",
        }
    )
    assert isinstance(adapter, SaasRetrieverAdapter)
    assert adapter.id == "my-id"
    assert dict(adapter._config.connector_kwargs) == {"owner": "plenoai"}


def test_build_connector_requires_connector_kind() -> None:
    with pytest.raises(ValueError, match="connector_kind"):
        build_connector({"owner": "plenoai"})


# --- discover / fetch round-trips --------------------------------------


@pytest.mark.asyncio
async def test_discover_yields_translated_refs() -> None:
    refs = [
        RetrieverDocumentRef(
            source_id="plenoai",
            source_kind="github",
            path="/repo/README.md",
            native_url="https://github.com/plenoai/repo/blob/main/README.md",
            metadata={"sha": "abcd1234"},
        )
    ]
    stub = _StubRetriever(refs=refs, docs_by_path={})
    adapter = _adapter_with_stub(stub)

    out: list[DocumentRef] = []
    async for ref in adapter.discover(
        SourceFilter(include=("*.md",), exclude=()), cursor=None
    ):
        out.append(ref)

    assert len(out) == 1
    assert isinstance(out[0], DocumentRef)
    assert out[0].source_kind == "github"
    assert out[0].path == "/repo/README.md"
    assert out[0].native_url == "https://github.com/plenoai/repo/blob/main/README.md"
    assert dict(out[0].metadata) == {"sha": "abcd1234"}
    # Filter forwarding: include kwarg made it across the boundary.
    assert stub.received_filter is not None
    assert stub.received_filter.include == ("*.md",)


@pytest.mark.asyncio
async def test_fetch_yields_translated_documents() -> None:
    ref = RetrieverDocumentRef(
        source_id="plenoai", source_kind="github", path="issues/1"
    )
    doc = RetrieverDocument(
        ref=ref,
        text="leaked secret AKIAIOSFODNN7EXAMPLE",
        fetched_at=datetime(2026, 5, 6, tzinfo=UTC),
        content_hash="sha256:abc",
        created_by=RetrieverPrincipal(
            id="u1", display_name="Alice", email="a@example.com"
        ),
        extra={"issue_number": 1},
    )
    stub = _StubRetriever(refs=[ref], docs_by_path={"issues/1": doc})
    adapter = _adapter_with_stub(stub)

    pii_ref = _to_pii_ref(ref)
    out: list[Document] = []
    async for d in adapter.fetch(pii_ref):
        assert isinstance(d, Document)
        out.append(d)
    assert len(out) == 1
    assert out[0].text == doc.text
    assert out[0].fetched_at == doc.fetched_at
    assert out[0].content_hash == doc.content_hash
    assert out[0].created_by == Principal(
        id="u1", display_name="Alice", email="a@example.com"
    )
    assert dict(out[0].extra) == {"issue_number": 1}
    # Stub saw a saas-retriever-shaped ref, not a pleno one.
    assert isinstance(stub.received_fetch[0], RetrieverDocumentRef)


@pytest.mark.asyncio
async def test_close_drains_underlying_retriever() -> None:
    stub = _StubRetriever(refs=[], docs_by_path={})
    adapter = _adapter_with_stub(stub)
    await adapter.close()
    assert stub.closed is True
    # Subsequent close is a no-op (idempotent).
    await adapter.close()


@pytest.mark.asyncio
async def test_close_swallows_retriever_close_errors() -> None:
    """close() must not raise — schedulers call it from finally blocks."""

    class _Boom(_StubRetriever):
        async def close(self) -> None:
            raise RuntimeError("boom")

    stub = _Boom(refs=[], docs_by_path={})
    adapter = _adapter_with_stub(stub)
    await adapter.close()  # would raise without the swallow.


# --- helpers ------------------------------------------------------------


def test_filter_helper_round_trips_every_field() -> None:
    src = SourceFilter(
        include=("a", "b"),
        exclude=("c",),
        since=datetime(2026, 1, 1, tzinfo=UTC),
        max_size=1024,
    )
    dst = _to_retriever_filter(src)
    assert dst.include == src.include
    assert dst.exclude == src.exclude
    assert dst.since == src.since
    assert dst.max_size == src.max_size


def test_ref_helpers_are_inverses() -> None:
    src = RetrieverDocumentRef(
        source_id="plenoai",
        source_kind="github",
        path="/x",
        native_url="https://example.com",
        parent_chain=("a", "b"),
        content_type="text/plain",
        size=42,
        etag="e1",
        last_modified=datetime(2026, 5, 6, tzinfo=UTC),
        metadata={"k": "v"},
    )
    pii = _to_pii_ref(src)
    back = _to_retriever_ref(pii)
    assert back == src


def test_document_helper_translates_binary() -> None:
    ref = RetrieverDocumentRef(source_id="plenoai", source_kind="github", path="/x.bin")
    doc = RetrieverDocument(ref=ref, binary=b"\x00\x01")
    pii = _to_pii_document(doc)
    assert pii.binary == b"\x00\x01"
    assert pii.text is None
    assert pii.created_by is None
