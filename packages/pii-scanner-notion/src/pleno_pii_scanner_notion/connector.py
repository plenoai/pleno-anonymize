"""NotionConnector — main SourceConnector for Notion workspaces.

Three independent discovery modes:

* **Search** — `POST /v1/search` with empty query yields every page +
  database the integration has been shared with. Default when no
  `pages` / `databases` is configured.
* **Explicit pages** — `pages=["<page-id>", ...]` scans the given pages
  and their descendant blocks.
* **Database query** — `databases=["<db-id>", ...]` enumerates rows of
  each database via `/v1/databases/{id}/query`.

The three modes are not mutually exclusive; the connector merges
results and yields one `DocumentRef` per page or database row. `fetch`
materializes the block tree (or the row's properties + child blocks)
into Markdown for the detector pipeline.

Concurrency defaults to 3 (`max_concurrent_fetches=3`) — Notion's
published cap is ~3 RPS averaged and 429 ramps up quickly past it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.scheduler.rate_limit import BucketKey
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,  # noqa: F401  re-exported in fetch annotation
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec

from .api import DEFAULT_BASE_URL, NOTION_VERSION, PAGE_SIZE, NotionApi
from .markdown import MAX_DEPTH, render_blocks, render_database_row


# Connector kind — entry-point key.
KIND = "notion"


@dataclass(frozen=True, slots=True)
class NotionConfig:
    """Construction config for `NotionConnector`.

    `token` is required and must be a Notion internal-integration token
    (Bearer). `pages` / `databases` switch on the explicit modes; if
    both are empty, the connector falls back to search-based discovery.
    """

    token: str
    id: str | None = None
    pages: tuple[str, ...] = ()
    databases: tuple[str, ...] = ()
    include_archived: bool = False
    base_url: str = DEFAULT_BASE_URL
    notion_version: str = NOTION_VERSION
    max_concurrent_fetches: int = 3
    request_timeout: float = 30.0
    workspace_id: str | None = None

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Token is never embedded in the id — derive a stable scope from
        # the optional workspace_id if the operator supplied one.
        scope = self.workspace_id or "default"
        return f"notion:{scope}"


class NotionConnector:
    """`SourceConnector` implementation backed by the Notion REST API.

    Owns one `NotionApi` for the connector's lifetime. The HTTP client is
    closed in `close()`; tests that pass `transport=...` get the same
    cleanup path so there are no leaked sessions on shutdown.
    """

    kind = KIND

    def __init__(
        self,
        config: NotionConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._api = NotionApi(
            token=config.token,
            base_url=config.base_url,
            notion_version=config.notion_version,
            transport=transport,
            timeout=config.request_timeout,
        )
        # `(object_type, object_id)` set used to deduplicate discoveries
        # when search + pages + databases are combined. Kept on the
        # instance because `discover` is an async generator and each
        # discovery invocation wants a fresh dedup horizon.
        self._discover_seen: set[tuple[str, str]] = set()

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=self._config.max_concurrent_fetches,
            streaming=False,
        )

    def bucket_key(self) -> BucketKey:
        """BucketKey for the global rate limiter.

        Notion's rate limit is per-integration-token — every endpoint
        shares the same ~3 RPS budget. We expose a single bucket per
        connector instance so the scheduler doesn't double-throttle.
        """
        return BucketKey(
            connector_kind=self.kind,
            tenant_id=self._config.workspace_id or self.id,
        )

    async def close(self) -> None:
        await self._api.aclose()

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(
        self,
        filter: SourceFilter,  # noqa: ARG002 — Notion has no native include/exclude
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield one DocumentRef per page or database row.

        Modes are evaluated in order — explicit pages first (so an
        operator pinning a small list gets fast results), then explicit
        databases, then the catch-all search. `cursor` is currently
        only honored by the search path; the explicit modes are bounded
        and always restart from scratch.
        """
        self._discover_seen = set()
        if self._config.pages:
            for page_id in self._config.pages:
                async for ref in self._discover_page(page_id):
                    yield ref
        if self._config.databases:
            for db_id in self._config.databases:
                async for ref in self._discover_database(db_id):
                    yield ref
        if not self._config.pages and not self._config.databases:
            async for ref in self._discover_search(cursor):
                yield ref

    async def _discover_page(self, page_id: str) -> AsyncIterator[DocumentRef]:
        page = await self._api.get(f"/pages/{page_id}")
        ref = self._page_to_ref(page)
        if ref is not None:
            yield ref

    async def _discover_database(self, database_id: str) -> AsyncIterator[DocumentRef]:
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                body["start_cursor"] = cursor
            payload = await self._api.post(f"/databases/{database_id}/query", json=body)
            for row in payload.get("results", []):
                ref = self._page_to_ref(row, parent_database_id=database_id)
                if ref is not None:
                    yield ref
            if not payload.get("has_more"):
                return
            cursor = payload.get("next_cursor")
            if not cursor:
                return

    async def _discover_search(self, cursor: str | None) -> AsyncIterator[DocumentRef]:
        next_cursor: str | None = cursor
        while True:
            body: dict[str, Any] = {"page_size": PAGE_SIZE}
            if next_cursor:
                body["start_cursor"] = next_cursor
            payload = await self._api.post("/search", json=body)
            for obj in payload.get("results", []):
                ref = self._page_to_ref(obj)
                if ref is not None:
                    # Round-trip the search cursor on every ref so the
                    # scheduler can checkpoint mid-search.
                    yield self._with_cursor(ref, payload.get("next_cursor"))
            if not payload.get("has_more"):
                return
            next_cursor = payload.get("next_cursor")
            if not next_cursor:
                return

    def _page_to_ref(
        self,
        obj: Mapping[str, Any] | None,
        *,
        parent_database_id: str | None = None,
    ) -> DocumentRef | None:
        if not isinstance(obj, Mapping) or not obj:
            return None
        object_type = obj.get("object")
        object_id = obj.get("id")
        if not isinstance(object_id, str) or object_type not in {"page", "database"}:
            return None
        if not self._config.include_archived and obj.get("archived"):
            return None
        key = (object_type, object_id)
        if key in self._discover_seen:
            return None
        self._discover_seen.add(key)
        last_modified = _parse_iso(obj.get("last_edited_time"))
        native_url = obj.get("url") if isinstance(obj.get("url"), str) else None
        # Path encodes the object kind so a finding's location renders as
        # `notion://page/<id>` or `notion://database-row/<id>` — the
        # parent_database_id qualifier disambiguates rows from standalone
        # pages.
        path_kind = "database-row" if parent_database_id else object_type
        path = f"notion://{path_kind}/{object_id}"
        parent_chain: tuple[str, ...] = ()
        if parent_database_id:
            parent_chain = (f"notion://database/{parent_database_id}",)
        elif isinstance(obj.get("parent"), Mapping):
            parent_id = _parent_id(obj["parent"])
            if parent_id is not None:
                parent_chain = (parent_id,)
        metadata: dict[str, str] = {
            "object_type": str(object_type),
            "object_id": object_id,
        }
        if parent_database_id:
            metadata["database_id"] = parent_database_id
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=path,
            native_url=native_url,
            parent_chain=parent_chain,
            content_type="text/markdown",
            last_modified=last_modified,
            metadata=metadata,
        )

    def _with_cursor(self, ref: DocumentRef, cursor: str | None) -> DocumentRef:
        if not cursor:
            return ref
        # DocumentRef is frozen; build a fresh metadata dict and rebind.
        new_meta = dict(ref.metadata)
        new_meta["_cursor"] = cursor
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
            metadata=new_meta,
        )

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Materialize a Markdown Document for `ref`.

        For pages and database rows: walk the block tree and convert
        every block to Markdown. For database rows the property map is
        emitted first (one `name: value` line per property) so detectors
        see the structured columns alongside the prose body.
        """
        meta = ref.metadata
        object_id = meta.get("object_id")
        object_type = meta.get("object_type")
        if not object_id or not object_type:
            return
        page_or_row = await self._fetch_object(object_type, object_id)
        if not page_or_row:
            return
        properties_md = ""
        if object_type == "page":
            # Database rows expose a `properties` map with rich-text
            # columns; standalone pages only have a `title` we don't
            # need to repeat (it appears in the block tree).
            if meta.get("database_id"):
                properties_md = render_database_row(page_or_row.get("properties"))
        body_md = await self._fetch_block_tree(object_id)
        text_parts = [p for p in (properties_md, body_md) if p]
        text = "\n\n".join(text_parts) if text_parts else ""
        if not text:
            # Nothing to scan — yield nothing rather than emit an empty
            # Document (which fails the text/binary XOR invariant).
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
        )

    async def _fetch_object(
        self, object_type: str, object_id: str
    ) -> Mapping[str, Any] | None:
        if object_type == "page":
            obj = await self._api.get(f"/pages/{object_id}")
        elif object_type == "database":
            obj = await self._api.get(f"/databases/{object_id}")
        else:
            return None
        return obj or None

    async def _fetch_block_tree(self, root_id: str) -> str:
        """Recursively pull a block's children and render them.

        Children are fetched eagerly per nesting level; for each block
        with `has_children=true` we issue another `/blocks/{id}/children`
        call. The recursion is bounded by `markdown.MAX_DEPTH` so a
        future API change that introduces a self-referential block can
        not infinite-loop.
        """
        # Cache: block_id -> list[block]. Populated as we descend so
        # `render_blocks` can resolve children synchronously via the
        # callback seam.
        children_cache: dict[str, list[Mapping[str, Any]]] = {}

        async def _walk(block_id: str, depth: int) -> None:
            if depth >= MAX_DEPTH:
                return
            children = await self._list_block_children(block_id)
            children_cache[block_id] = children
            for child in children:
                if child.get("has_children"):
                    child_id = child.get("id")
                    if isinstance(child_id, str):
                        await _walk(child_id, depth + 1)

        await _walk(root_id, depth=0)

        def lookup(block_id: str | None) -> list[Mapping[str, Any]]:
            if not isinstance(block_id, str):
                return []
            return children_cache.get(block_id, [])

        return render_blocks(
            children_cache.get(root_id, []),
            children_for=lookup,
            include_archived=self._config.include_archived,
        )

    async def _list_block_children(self, block_id: str) -> list[Mapping[str, Any]]:
        """Page through `/blocks/{id}/children` and return every child block."""
        out: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": PAGE_SIZE}
            if cursor:
                params["start_cursor"] = cursor
            payload = await self._api.get(f"/blocks/{block_id}/children", params=params)
            for block in payload.get("results", []):
                if not isinstance(block, Mapping):
                    continue
                if not self._config.include_archived and block.get("archived"):
                    continue
                out.append(block)
            if not payload.get("has_more"):
                return out
            cursor = payload.get("next_cursor")
            if not cursor:
                return out


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parent_id(parent: Mapping[str, Any]) -> str | None:
    """Render a Notion `parent` object as a `notion://...` URI."""
    p_type = parent.get("type")
    if p_type == "database_id":
        return f"notion://database/{parent.get('database_id')}"
    if p_type == "page_id":
        return f"notion://page/{parent.get('page_id')}"
    if p_type == "block_id":
        return f"notion://block/{parent.get('block_id')}"
    if p_type == "workspace":
        return "notion://workspace"
    return None


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    """Registry factory: dict → NotionConnector.

    Required key: `token`. Optional knobs match the `NotionConfig` fields.
    """
    if "token" not in config:
        raise ValueError("notion connector config requires 'token'")
    token = str(config["token"])
    if not token:
        raise ValueError("notion connector 'token' must be a non-empty string")
    pages = _string_tuple(config.get("pages"))
    databases = _string_tuple(config.get("databases"))
    return NotionConnector(
        NotionConfig(
            token=token,
            id=str(config["id"]) if config.get("id") is not None else None,
            pages=pages,
            databases=databases,
            include_archived=bool(config.get("include_archived", False)),
            base_url=str(config.get("base_url", DEFAULT_BASE_URL)),
            notion_version=str(config.get("notion_version", NOTION_VERSION)),
            max_concurrent_fetches=int(config.get("max_concurrent_fetches", 3)),
            request_timeout=float(config.get("request_timeout", 30.0)),
            workspace_id=(
                str(config["workspace_id"])
                if config.get("workspace_id") is not None
                else None
            ),
        )
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Accept list/tuple of strings; reject everything else loudly."""
    if value is None:
        return ()
    if isinstance(value, str):
        # A bare string is almost certainly an operator typo (one id
        # instead of a list of ids); reject so they catch it before the
        # scan launches.
        raise ValueError(
            "notion connector list-typed configs (pages, databases) "
            "must be a list, not a bare string"
        )
    if not isinstance(value, Iterable):
        raise ValueError("notion connector list-typed configs must be iterable")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(
                "notion connector list-typed configs must contain non-empty strings"
            )
        out.append(item)
    return tuple(out)


SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        max_concurrent_fetches=3,
    ),
    required_scopes=(
        # Notion doesn't expose granular OAuth scopes for internal
        # integrations; the integration's "Capabilities" UI gates
        # read access. We document the practical scope set here so
        # `connectors describe notion` shows what the operator must
        # toggle on in Notion's integration settings.
        "read_content",
        "read_user_information",
    ),
    description=(
        "Notion connector. Internal Integration Token (Bearer); search-based "
        "discovery + explicit page list + database query. Fetch materializes "
        "the block tree as Markdown so detectors see the same surface text a "
        "Notion reader would. Concurrency capped at 3 to stay under Notion's "
        "~3 RPS rate limit. ADR-0007 §13."
    ),
)


__all__ = [
    "KIND",
    "SPEC",
    "NotionConfig",
    "NotionConnector",
]
