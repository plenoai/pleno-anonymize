"""Adapter unit tests using a stub saas-scraper Connector.

We never touch a real BrowserSession — the tests pre-populate the
adapter's internal scraper handle so ``_ensure_started`` short-circuits
and no Chromium process is spawned.
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
from saas_scraper.core import (
    Document as ScraperDocument,
    DocumentRef as ScraperDocumentRef,
    Principal as ScraperPrincipal,
    SourceFilter as ScraperFilter,
)

from pleno_pii_scanner_saas_scraper import (
    KIND,
    SPEC,
    SaasScraperAdapter,
    SaasScraperConfig,
    build_connector,
)
from pleno_pii_scanner_saas_scraper.adapter import (
    _to_pii_document,
    _to_pii_ref,
    _to_scraper_filter,
    _to_scraper_ref,
)


# --- stub scraper -------------------------------------------------------


class _StubScraper:
    """Minimal saas_scraper-shaped Connector for tests."""

    id = "stub-id"
    kind = "stub"

    def __init__(self, refs: list[ScraperDocumentRef], docs_by_path: dict[str, ScraperDocument]) -> None:
        self._refs = refs
        self._docs = docs_by_path
        self.received_filter: ScraperFilter | None = None
        self.received_fetch: list[ScraperDocumentRef] = []
        self.closed = False

    async def discover(self, filter: ScraperFilter) -> AsyncIterator[ScraperDocumentRef]:
        self.received_filter = filter
        for ref in self._refs:
            yield ref

    async def fetch(self, ref: ScraperDocumentRef) -> AsyncIterator[ScraperDocument]:
        self.received_fetch.append(ref)
        yield self._docs[ref.path]

    async def close(self) -> None:
        self.closed = True


def _adapter_with_stub(stub: _StubScraper, *, scraper_kind: str = "slack") -> SaasScraperAdapter:
    """Build an adapter that skips BrowserSession startup."""
    adapter = SaasScraperAdapter(
        SaasScraperConfig(
            scraper_kind=scraper_kind,
            scraper_kwargs={"workspace": "acme"},
        )
    )
    adapter._scraper = stub  # type: ignore[assignment]
    return adapter


# --- spec / entry-point sanity -----------------------------------------


def test_spec_uses_known_kind() -> None:
    assert SPEC.kind == KIND == "saas-scraper"
    assert SPEC.factory is build_connector
    assert isinstance(SPEC.capabilities, Capabilities)
    assert SPEC.capabilities.max_concurrent_fetches == 1


def test_capabilities_report_serial_chrome() -> None:
    adapter = _adapter_with_stub(_StubScraper([], {}))
    caps = adapter.capabilities()
    assert caps.max_concurrent_fetches == 1
    assert caps.incremental is False
    assert caps.streaming is False


# --- config validation --------------------------------------------------


def test_unknown_scraper_kind_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scraper_kind"):
        SaasScraperConfig(scraper_kind="not-a-thing", scraper_kwargs={})


def test_resolved_id_falls_back_to_distinctive_kwarg() -> None:
    cfg = SaasScraperConfig(scraper_kind="slack", scraper_kwargs={"workspace": "acme"})
    assert cfg.resolved_id() == "saas-scraper:slack:acme"

    cfg_repo = SaasScraperConfig(
        scraper_kind="github",
        scraper_kwargs={"owner": "plenoai", "repo": "saas-scraper"},
    )
    # repo wins over owner because the loop checks `repo` first.
    assert cfg_repo.resolved_id() == "saas-scraper:github:saas-scraper"

    cfg_explicit = SaasScraperConfig(
        scraper_kind="slack",
        scraper_kwargs={"workspace": "acme"},
        id="custom-id",
    )
    assert cfg_explicit.resolved_id() == "custom-id"


def test_build_connector_strips_owner_keys() -> None:
    """build_connector pops keys this wheel owns; rest goes to scraper kwargs."""
    adapter = build_connector({
        "scraper_kind": "slack",
        "workspace": "acme",
        "headless": False,
        "id": "my-id",
    })
    assert isinstance(adapter, SaasScraperAdapter)
    assert adapter.id == "my-id"
    assert adapter._config.headless is False
    assert dict(adapter._config.scraper_kwargs) == {"workspace": "acme"}


def test_build_connector_requires_scraper_kind() -> None:
    with pytest.raises(ValueError, match="scraper_kind"):
        build_connector({"workspace": "acme"})


# --- discover / fetch round-trips --------------------------------------


@pytest.mark.asyncio
async def test_discover_yields_translated_refs() -> None:
    refs = [
        ScraperDocumentRef(
            source_id="ws",
            source_kind="slack",
            path="/general",
            native_url="https://acme.slack.com/archives/C001",
            metadata={"channel_id": "C001"},
        )
    ]
    stub = _StubScraper(refs=refs, docs_by_path={})
    adapter = _adapter_with_stub(stub)

    out: list[DocumentRef] = []
    async for ref in adapter.discover(SourceFilter(include=("*.txt",), exclude=()), cursor=None):
        out.append(ref)

    assert len(out) == 1
    assert isinstance(out[0], DocumentRef)
    assert out[0].source_kind == "slack"
    assert out[0].path == "/general"
    assert out[0].native_url == "https://acme.slack.com/archives/C001"
    assert dict(out[0].metadata) == {"channel_id": "C001"}
    # Filter forwarding: include kwarg made it across the boundary.
    assert stub.received_filter is not None
    assert stub.received_filter.include == ("*.txt",)


@pytest.mark.asyncio
async def test_fetch_yields_translated_documents() -> None:
    ref = ScraperDocumentRef(source_id="ws", source_kind="github", path="issues/1")
    doc = ScraperDocument(
        ref=ref,
        text="leaked secret AKIAIOSFODNN7EXAMPLE",
        fetched_at=datetime(2026, 5, 6, tzinfo=UTC),
        content_hash="sha256:abc",
        created_by=ScraperPrincipal(id="u1", display_name="Alice", email="a@example.com"),
        extra={"thread_ts": "1"},
    )
    stub = _StubScraper(refs=[ref], docs_by_path={"issues/1": doc})
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
    assert out[0].created_by == Principal(id="u1", display_name="Alice", email="a@example.com")
    assert dict(out[0].extra) == {"thread_ts": "1"}
    # Stub saw a saas-scraper-shaped ref, not a pleno one.
    assert isinstance(stub.received_fetch[0], ScraperDocumentRef)


@pytest.mark.asyncio
async def test_close_drains_underlying_scraper() -> None:
    stub = _StubScraper(refs=[], docs_by_path={})
    adapter = _adapter_with_stub(stub)
    await adapter.close()
    assert stub.closed is True
    # Subsequent close is a no-op (idempotent).
    await adapter.close()


@pytest.mark.asyncio
async def test_close_swallows_scraper_close_errors() -> None:
    """close() must not raise — schedulers call it from finally blocks."""

    class _Boom(_StubScraper):
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
    dst = _to_scraper_filter(src)
    assert dst.include == src.include
    assert dst.exclude == src.exclude
    assert dst.since == src.since
    assert dst.max_size == src.max_size


def test_ref_helpers_are_inverses() -> None:
    src = ScraperDocumentRef(
        source_id="ws",
        source_kind="slack",
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
    back = _to_scraper_ref(pii)
    assert back == src


def test_document_helper_translates_binary() -> None:
    ref = ScraperDocumentRef(source_id="ws", source_kind="slack", path="/x.bin")
    doc = ScraperDocument(ref=ref, binary=b"\x00\x01")
    pii = _to_pii_document(doc)
    assert pii.binary == b"\x00\x01"
    assert pii.text is None
    assert pii.created_by is None
