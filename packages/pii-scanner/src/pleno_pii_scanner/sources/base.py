"""SourceConnector / Document protocol — type contract for every connector."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Protocol, runtime_checkable


# Opaque per-connector resume token. Persisted verbatim in CheckpointStore
# (#6) and round-tripped through `discover(..., cursor=...)`. Never parsed
# outside the owning connector — keeps the scheduler agnostic of GitHub
# `pushed:>ts` vs Slack ts strings vs Notion `next_cursor` vs SharePoint
# delta tokens.
Cursor = str


@dataclass(frozen=True, slots=True)
class Principal:
    """Identity that produced or owns a document.

    Populated when the source exposes authorship (git author, Slack user,
    SharePoint owner, Jira reporter). Used by FindingsStore (#9) to attach
    "who created this PII" to verified findings.
    """

    id: str
    display_name: str | None = None
    email: str | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Connector self-description consumed by the Scheduler (#7).

    `incremental` lets the scheduler skip a full re-walk when a checkpoint
    exists. `binary` declares whether `fetch()` yields binary payloads
    that the ContentExtractor (#8) needs to handle. `content_hash_delta`
    means the connector can short-circuit on unchanged ETag/digest before
    re-fetching the body. `max_concurrent_fetches` bounds the per-connector
    asyncio Semaphore.
    """

    incremental: bool = False
    binary: bool = False
    content_hash_delta: bool = False
    max_concurrent_fetches: int = 8
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class SourceFilter:
    """Discover-time include/exclude/since filter.

    Resolved by the connector when possible (e.g. JQL `updated >= since`,
    S3 `Prefix=`, Slack `oldest=`). When the source has no native
    server-side filter, the connector applies the same predicate
    client-side so behavior is uniform across connectors.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    since: datetime | None = None
    max_size: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """Cheap metadata-only handle yielded by `discover()`.

    Holds enough information for the scheduler to decide whether to fetch
    (incremental skip via `etag` or `last_modified`), to attribute work to
    a tenant for rate-limiting, and to render a partial finding location
    even before the body is available.
    """

    source_id: str
    source_kind: str
    path: str
    native_url: str | None = None
    parent_chain: tuple[str, ...] = ()
    content_type: str = "application/octet-stream"
    size: int | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Stable hash for FindingsStore dedup and CheckpointStore keys."""
        h = sha256()
        h.update(self.source_id.encode())
        h.update(b"\0")
        h.update(self.source_kind.encode())
        h.update(b"\0")
        h.update(self.path.encode())
        if self.etag:
            h.update(b"\0")
            h.update(self.etag.encode())
        return h.hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Document:
    """Full payload returned by `fetch()` for documents that fit in memory.

    Exactly one of `text` / `binary` is populated; the other is None. For
    streaming payloads (TB-scale S3 objects, SharePoint files larger than
    `--max-doc-bytes`), the connector yields `DocumentChunk` instead.
    """

    ref: DocumentRef
    text: str | None = None
    binary: bytes | None = None
    fetched_at: datetime | None = None
    content_hash: str | None = None
    created_by: Principal | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Enforce the (text XOR binary) invariant at construction so
        # downstream code can trust `if doc.text is not None` without
        # also defending against accidental dual-population.
        if (self.text is None) == (self.binary is None):
            raise ValueError(
                "Document must populate exactly one of `text` or `binary`; "
                f"got text={self.text is not None}, binary={self.binary is not None}"
            )


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    """Streamed slice of a document payload.

    Yielded in order by `fetch()` for documents that exceed the in-memory
    size budget. The pipeline carries a small overlap window between
    consecutive chunks (max-pattern-length + 256B) so that a regex match
    spanning a chunk boundary is not lost.
    """

    ref: DocumentRef
    byte_range: tuple[int, int]
    is_final: bool
    text: str | None = None
    binary: bytes | None = None
    fetched_at: datetime | None = None

    def __post_init__(self) -> None:
        if (self.text is None) == (self.binary is None):
            raise ValueError(
                "DocumentChunk must populate exactly one of `text` or `binary`"
            )
        start, end = self.byte_range
        if start < 0 or end < start:
            raise ValueError(
                f"DocumentChunk.byte_range must be (start>=0, end>=start); "
                f"got {self.byte_range}"
            )


@runtime_checkable
class SourceConnector(Protocol):
    """Type contract every connector implements.

    The Scheduler treats SourceConnector instances as opaque
    discover/fetch endpoints. Construction is the connector's
    responsibility (the registry passes a per-source config dict and a
    resolved Credential bundle); cleanup happens in `close()`.

    Connectors must be safe to call concurrently up to
    `capabilities().max_concurrent_fetches`. State that needs locking
    (HTTP session pools, paginator cursors) lives inside the connector
    instance.
    """

    id: str
    kind: str

    def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate document refs matching `filter`, resuming at `cursor`.

        Must be cheap — metadata only. Connectors may make multiple paged
        API calls but must not download payloads. Implementations should
        emit a fresh `Cursor` periodically (e.g. every page) by attaching
        it to a `DocumentRef.metadata['_cursor']` entry; the scheduler
        persists it for resume.
        """
        ...

    def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Retrieve the payload for `ref`.

        Yields a single `Document` when the body fits in
        `--max-doc-bytes`, or a sequence of `DocumentChunk` (in byte
        order, last with `is_final=True`) when streaming. Connectors that
        cannot determine the size in advance should start streaming.
        """
        ...

    def capabilities(self) -> Capabilities:
        """Return static connector capabilities."""
        ...

    async def close(self) -> None:
        """Release HTTP sessions, file handles, DB pools, etc."""
        ...
