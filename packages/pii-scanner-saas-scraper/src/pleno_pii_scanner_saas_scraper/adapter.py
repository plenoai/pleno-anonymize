"""SourceConnector adapter wrapping any saas_scraper.Connector.

The shapes of ``saas_scraper.Document`` and
``pleno_pii_scanner.sources.base.Document`` were aligned at saas-scraper
0.1.0 so the translation is a per-field copy. If a future saas-scraper
release adds a field, mirror it here in ``_to_pii_document`` /
``_to_pii_ref`` — the adapter is the single integration seam between the
two type systems.

Concurrency: a saas-scraper Connector serialises on its parent
``BrowserSession`` (one Chromium instance). The adapter therefore reports
``max_concurrent_fetches=1`` so the scheduler never tries to drive the
same browser from two coroutines.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
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
from saas_scraper import BrowserSession
from saas_scraper import connectors as _saas_connectors  # noqa: F401  registry side-effect
from saas_scraper.core import Document as ScraperDocument
from saas_scraper.core import DocumentRef as ScraperDocumentRef
from saas_scraper.core import SourceFilter as ScraperFilter
from saas_scraper.registry import registry as _saas_registry

KIND = "saas-scraper"


@dataclass(frozen=True, slots=True)
class SaasScraperConfig:
    """Construction config for ``SaasScraperAdapter``.

    ``scraper_kind`` selects the underlying saas-scraper connector
    (slack, github, gitlab, ...). ``scraper_kwargs`` is forwarded
    verbatim — each saas-scraper connector takes a different set
    (``workspace=`` for slack, ``owner=`` + ``repo=`` for github, ...).

    ``headless`` and ``profile_dir`` shape the underlying ``BrowserSession``.
    The default profile dir resolves through saas-scraper's own XDG
    fallback so a user who has already logged in via the saas-scraper
    CLI inherits that session here.
    """

    scraper_kind: str
    scraper_kwargs: Mapping[str, Any]
    headless: bool = True
    profile_dir: Path | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        available = _saas_registry.names()
        if self.scraper_kind not in available:
            raise ValueError(
                f"unknown scraper_kind {self.scraper_kind!r}; "
                f"available: {sorted(available)}"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Fall back to a stable identifier derived from the underlying
        # scraper kind + the most-distinctive kwarg ("workspace" for
        # slack, "owner" / "repo" for github, "site" for jira). This
        # keeps FindingsStore keys readable when an operator hasn't
        # bothered with an explicit `id`.
        for key in ("workspace", "site", "repo", "owner", "project"):
            value = self.scraper_kwargs.get(key)
            if value:
                return f"saas-scraper:{self.scraper_kind}:{value}"
        return f"saas-scraper:{self.scraper_kind}"


class SaasScraperAdapter:
    """Bridge a ``saas_scraper.Connector`` into pleno-pii-scanner.

    Owns one ``BrowserSession`` for the connector's lifetime. The
    session is created lazily on the first ``discover()`` /
    ``fetch()`` call so a registry walk that never instantiates this
    adapter never spawns a Chromium process.
    """

    kind = KIND

    def __init__(self, config: SaasScraperConfig) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._session: BrowserSession | None = None
        self._scraper: Any | None = None
        self._init_lock = asyncio.Lock()

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,  # noqa: ARG002 — saas-scraper has no incremental cursor
    ) -> AsyncIterator[DocumentRef]:
        await self._ensure_started()
        assert self._scraper is not None
        scraper_filter = _to_scraper_filter(filter)
        async for ref in self._scraper.discover(scraper_filter):
            yield _to_pii_ref(ref)

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        await self._ensure_started()
        assert self._scraper is not None
        scraper_ref = _to_scraper_ref(ref)
        async for doc in self._scraper.fetch(scraper_ref):
            yield _to_pii_document(doc)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=1,
            streaming=False,
        )

    async def close(self) -> None:
        # Tear down in the order opposite to start: the saas-scraper
        # connector first (it may hold open Page handles tied to the
        # session), then the BrowserSession itself.
        if self._scraper is not None:
            try:
                await self._scraper.close()
            except Exception:
                # Closing must never raise — the scheduler may be in a
                # finally block. Swallow and continue tearing down.
                pass
            self._scraper = None
        if self._session is not None:
            await self._session.__aexit__(None, None, None)
            self._session = None

    async def _ensure_started(self) -> None:
        if self._scraper is not None:
            return
        async with self._init_lock:
            if self._scraper is not None:
                return
            session = BrowserSession(
                headless=self._config.headless,
                profile_dir=self._config.profile_dir,
            )
            await session.__aenter__()
            try:
                scraper = _saas_registry.create(
                    self._config.scraper_kind,
                    session=session,
                    **dict(self._config.scraper_kwargs),
                )
            except Exception:
                await session.__aexit__(None, None, None)
                raise
            self._session = session
            self._scraper = scraper


# --- factory used by the entry-point ConnectorSpec ----------------------


def build_connector(config: Mapping[str, Any]) -> SaasScraperAdapter:
    """Build a SaasScraperAdapter from a config dict.

    Strips the keys this wheel owns (``scraper_kind``, ``headless``,
    ``profile_dir``, ``id``); everything else is forwarded to the
    saas-scraper connector ``__init__`` as kwargs.
    """
    cfg = dict(config)
    scraper_kind = cfg.pop("scraper_kind", None)
    if not scraper_kind:
        raise ValueError(
            "saas-scraper connector requires `scraper_kind` (one of "
            f"{sorted(_saas_registry.names())})"
        )
    headless = bool(cfg.pop("headless", True))
    profile_dir = cfg.pop("profile_dir", None)
    if profile_dir is not None and not isinstance(profile_dir, Path):
        profile_dir = Path(os.path.expanduser(str(profile_dir)))
    explicit_id = cfg.pop("id", None)
    return SaasScraperAdapter(
        SaasScraperConfig(
            scraper_kind=str(scraper_kind),
            scraper_kwargs=cfg,
            headless=headless,
            profile_dir=profile_dir,
            id=str(explicit_id) if explicit_id else None,
        )
    )


# --- field translations -------------------------------------------------


def _to_scraper_filter(filter: SourceFilter) -> ScraperFilter:
    return ScraperFilter(
        include=filter.include,
        exclude=filter.exclude,
        since=filter.since,
        max_size=filter.max_size,
    )


def _to_scraper_ref(ref: DocumentRef) -> ScraperDocumentRef:
    return ScraperDocumentRef(
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


def _to_pii_ref(ref: ScraperDocumentRef) -> DocumentRef:
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


def _to_pii_document(doc: ScraperDocument) -> Document:
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
