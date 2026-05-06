"""SourceConnector adapter wrapping any saas_retriever.Connector.

The shapes of ``saas_retriever.Document`` and
``pleno_pii_scanner.sources.base.Document`` were aligned at
saas-retriever 0.1.0 so the translation is a per-field copy. If a future
saas-retriever release adds a field, mirror it here in
``_to_pii_document`` / ``_to_pii_ref`` — the adapter is the single
integration seam between the two type systems.

Concurrency: saas-retriever is API-only (httpx) so different connectors
can run in parallel. ``max_concurrent_fetches`` defaults to 8 — adjust
through ``Capabilities`` if a provider has tighter rate limits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    Principal,
    SourceFilter,
)
from saas_retriever import connectors as _saas_connectors  # noqa: F401  registry side-effect
from saas_retriever.core import Document as RetrieverDocument
from saas_retriever.core import DocumentRef as RetrieverDocumentRef
from saas_retriever.core import SourceFilter as RetrieverFilter
from saas_retriever.registry import registry as _saas_registry

KIND = "saas-retriever"


@dataclass(frozen=True, slots=True)
class SaasRetrieverConfig:
    """Construction config for ``SaasRetrieverAdapter``.

    ``connector_kind`` selects the underlying saas-retriever connector
    (currently ``github``; more land in subsequent releases).
    ``connector_kwargs`` is forwarded verbatim to that connector's
    constructor — each saas-retriever connector takes its own set
    (``owner=`` / ``repo=`` / ``token=`` / ``resources=`` for github).
    """

    connector_kind: str
    connector_kwargs: Mapping[str, Any]
    id: str | None = None

    def __post_init__(self) -> None:
        available = _saas_registry.names()
        if self.connector_kind not in available:
            raise ValueError(
                f"unknown connector_kind {self.connector_kind!r}; "
                f"available: {sorted(available)}"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Fall back to a stable identifier derived from the underlying
        # connector kind + the most-distinctive kwarg ("owner" / "repo"
        # for github, "site" for jira, "workspace" for slack). Keeps
        # FindingsStore keys readable when an operator hasn't bothered
        # with an explicit `id`.
        for key in ("workspace", "site", "repo", "owner", "project"):
            value = self.connector_kwargs.get(key)
            if value:
                return f"saas-retriever:{self.connector_kind}:{value}"
        return f"saas-retriever:{self.connector_kind}"


class SaasRetrieverAdapter:
    """Bridge a ``saas_retriever.Connector`` into pleno-pii-scanner.

    Construction is lazy — the underlying connector is instantiated on
    the first ``discover()`` / ``fetch()`` call so a registry walk that
    never exercises this adapter never opens an httpx client.
    """

    kind = KIND

    def __init__(self, config: SaasRetrieverConfig) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._retriever: Any | None = None
        self._init_lock = asyncio.Lock()

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,  # noqa: ARG002 — saas-retriever has no incremental cursor
    ) -> AsyncIterator[DocumentRef]:
        await self._ensure_started()
        assert self._retriever is not None
        retriever_filter = _to_retriever_filter(filter)
        async for ref in self._retriever.discover(retriever_filter):
            yield _to_pii_ref(ref)

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        await self._ensure_started()
        assert self._retriever is not None
        retriever_ref = _to_retriever_ref(ref)
        async for doc in self._retriever.fetch(retriever_ref):
            yield _to_pii_document(doc)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=8,
            streaming=False,
        )

    async def close(self) -> None:
        if self._retriever is not None:
            try:
                await self._retriever.close()
            except Exception:
                # Closing must never raise — the scheduler may be in a
                # finally block. Swallow and continue tearing down.
                pass
            self._retriever = None

    async def _ensure_started(self) -> None:
        if self._retriever is not None:
            return
        async with self._init_lock:
            if self._retriever is not None:
                return
            self._retriever = _saas_registry.create(
                self._config.connector_kind,
                **dict(self._config.connector_kwargs),
            )


# --- factory used by the entry-point ConnectorSpec ----------------------


def build_connector(config: Mapping[str, Any]) -> SaasRetrieverAdapter:
    """Build a SaasRetrieverAdapter from a config dict.

    Strips the keys this wheel owns (``connector_kind``, ``id``);
    everything else is forwarded to the saas-retriever connector
    ``__init__`` as kwargs.
    """
    cfg = dict(config)
    connector_kind = cfg.pop("connector_kind", None)
    if not connector_kind:
        raise ValueError(
            "saas-retriever connector requires `connector_kind` (one of "
            f"{sorted(_saas_registry.names())})"
        )
    explicit_id = cfg.pop("id", None)
    return SaasRetrieverAdapter(
        SaasRetrieverConfig(
            connector_kind=str(connector_kind),
            connector_kwargs=cfg,
            id=str(explicit_id) if explicit_id else None,
        )
    )


# --- field translations -------------------------------------------------


def _to_retriever_filter(filter: SourceFilter) -> RetrieverFilter:
    return RetrieverFilter(
        include=filter.include,
        exclude=filter.exclude,
        since=filter.since,
        max_size=filter.max_size,
    )


def _to_retriever_ref(ref: DocumentRef) -> RetrieverDocumentRef:
    return RetrieverDocumentRef(
        source_id=ref.source_id,
        source_kind=ref.source_kind,
        path=ref.path,
        native_url=ref.native_url,
        parent_chain=ref.parent_chain,
        content_type=ref.content_type,
        size=ref.size,
        etag=ref.etag,
        last_modified=ref.last_modified,
        metadata=dict(ref.metadata),
    )


def _to_pii_ref(ref: RetrieverDocumentRef) -> DocumentRef:
    return DocumentRef(
        source_id=ref.source_id,
        source_kind=ref.source_kind,
        path=ref.path,
        native_url=ref.native_url,
        parent_chain=ref.parent_chain,
        content_type=ref.content_type,
        size=ref.size,
        etag=ref.etag,
        last_modified=ref.last_modified,
        metadata=dict(ref.metadata),
    )


def _to_pii_document(doc: RetrieverDocument) -> Document:
    return Document(
        ref=_to_pii_ref(doc.ref),
        text=doc.text,
        binary=doc.binary,
        fetched_at=doc.fetched_at,
        content_hash=doc.content_hash,
        created_by=(
            Principal(
                id=doc.created_by.id,
                display_name=doc.created_by.display_name,
                email=doc.created_by.email,
            )
            if doc.created_by is not None
            else None
        ),
        extra=dict(doc.extra),
    )
