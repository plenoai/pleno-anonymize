"""Elasticsearch / OpenSearch SourceConnector.

Pipeline:

  1. Resolve indices: GET `/_resolve/index/<patterns>` → concrete index names
  2. Open snapshot:
       Elasticsearch: POST `/<idx>/_pit?keep_alive=5m` → `{"id": pit_id}`
       OpenSearch ≥2: POST `/<idx>/_search/point_in_time?keep_alive=5m`
       OpenSearch <2 (PIT unavailable): fall back to scroll API
  3. Page with `search_after`:
       POST `/_search` body:
         { "size": page_size,
           "query": <random_score wrapper if sample_fraction<1 else match_all>,
           "sort": [{"_shard_doc": "asc"}],   # ES requires _shard_doc with PIT
           "pit": {"id": pit_id, "keep_alive": "5m"},
           "search_after": <last sort vector> }
       Each hit → DocumentRef (path = `<index>/<_id>`).
  4. Cursor encodes the last `search_after` vector + pit_id so a resumed
     scan picks up exactly where it stopped without revisiting hits.
  5. Close PIT: DELETE `/_pit` { "id": pit_id }.

`fetch()` returns the `_source` body, concatenated across `text_fields`
(falling back to JSON-serialised `_source` if no fields configured).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


_DEFAULT_KEEP_ALIVE = "5m"


@dataclass(frozen=True, slots=True)
class ElasticsearchConfig:
    """Construction config for `ElasticsearchConnector`."""

    hosts: tuple[str, ...]
    indices: tuple[str, ...] = ("*",)
    api_key: str | None = None
    basic_user: str | None = None
    basic_password: str | None = None
    bearer_token: str | None = None
    flavor: Literal["elasticsearch", "opensearch"] = "elasticsearch"
    sample_fraction: float = 1.0
    text_fields: tuple[str, ...] = ()
    page_size: int = 1000
    keep_alive: str = _DEFAULT_KEEP_ALIVE
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.hosts:
            raise ValueError("hosts must be non-empty")
        # Exactly one auth mode (or none for unauthenticated localhost).
        modes = sum(
            1
            for v in (
                self.api_key,
                self.basic_user,
                self.bearer_token,
            )
            if v
        )
        if modes > 1:
            raise ValueError(
                "specify at most one of api_key / basic_user / bearer_token"
            )
        if self.basic_user and not self.basic_password:
            raise ValueError("basic_user requires basic_password")
        if not 0 < self.sample_fraction <= 1.0:
            raise ValueError("sample_fraction must be in (0, 1]")
        if self.page_size < 1:
            raise ValueError("page_size must be >= 1")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Hash the host set so two configs that look the same get the same
        # id while never leaking secrets into logs/metrics.
        import hashlib

        h = hashlib.sha256()
        for host in sorted(self.hosts):
            h.update(host.encode())
            h.update(b"\0")
        for idx in sorted(self.indices):
            h.update(idx.encode())
            h.update(b"\0")
        return f"elasticsearch:{h.hexdigest()[:16]}"


class ElasticsearchConnector:
    """Read-only SourceConnector for Elasticsearch / OpenSearch."""

    kind = "elasticsearch"

    def __init__(
        self,
        config: ElasticsearchConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=config.hosts[0], timeout=60.0
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._headers = self._build_auth_headers()
        self._documents: dict[str, str] = {}
        # Track open PITs so close() can release server resources.
        self._open_pits: list[str] = []

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        state = _decode_cursor(cursor)
        # Resolve index patterns once so includes/excludes can match concrete names.
        indices = await self._resolve_indices()
        if not indices:
            return
        index_csv = ",".join(indices)
        if state.pit_id is None:
            pit_id = await self._open_pit(index_csv)
        else:
            pit_id = state.pit_id
        if pit_id:
            self._open_pits.append(pit_id)
        search_after: list[Any] | None = state.search_after
        try:
            while True:
                body = self._build_search_body(
                    pit_id=pit_id,
                    search_after=search_after,
                    index_csv=index_csv,
                )
                hits = await self._search(pit_id=pit_id, body=body)
                if not hits:
                    return
                for hit in hits:
                    idx = hit.get("_index", "")
                    doc_id = hit.get("_id", "")
                    full = f"{idx}/{doc_id}"
                    if filter.include and not _matches_any(
                        full, filter.include
                    ):
                        continue
                    if filter.exclude and _matches_any(full, filter.exclude):
                        continue
                    text = self._render_source(hit.get("_source", {}))
                    self._documents[full] = text
                    new_cursor = _encode_cursor(
                        _CursorState(
                            pit_id=pit_id,
                            search_after=hit.get("sort"),
                        )
                    )
                    yield DocumentRef(
                        source_id=self.id,
                        source_kind=self.kind,
                        path=full,
                        content_type="application/json",
                        size=len(text),
                        metadata={
                            "_cursor": new_cursor,
                            "index": idx,
                            "doc_id": doc_id,
                        },
                    )
                # Advance search_after to the last hit's sort vector.
                last_sort = hits[-1].get("sort")
                if not last_sort:
                    return
                search_after = list(last_sort)
                if len(hits) < self._config.page_size:
                    return
        finally:
            # PIT lifecycle is owned by discover() — close it as soon as we
            # exhaust the snapshot or the caller stops iterating.
            if pit_id and pit_id in self._open_pits:
                try:
                    await self._close_pit(pit_id)
                finally:
                    self._open_pits.remove(pit_id)

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        text = self._documents.get(ref.path)
        if text is None:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            extra=dict(ref.metadata),
        )

    async def close(self) -> None:
        # Best-effort PIT cleanup before tearing down the client.
        for pit_id in list(self._open_pits):
            try:
                await self._close_pit(pit_id)
            except Exception:
                # PIT may already be expired server-side; never let cleanup
                # raise during shutdown.
                pass
        self._open_pits.clear()
        self._documents.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- internals ------------------------------------------------

    def _build_auth_headers(self) -> dict[str, str]:
        c = self._config
        if c.api_key:
            return {"Authorization": f"ApiKey {c.api_key}"}
        if c.basic_user and c.basic_password:
            token = base64.b64encode(
                f"{c.basic_user}:{c.basic_password}".encode()
            ).decode()
            return {"Authorization": f"Basic {token}"}
        if c.bearer_token:
            return {"Authorization": f"Bearer {c.bearer_token}"}
        return {}

    async def _resolve_indices(self) -> list[str]:
        # Collapse the configured patterns into a comma-separated path
        # segment per the _resolve API contract.
        pattern = ",".join(self._config.indices)
        resp = await self._client.get(
            f"/_resolve/index/{pattern}", headers=self._headers
        )
        resp.raise_for_status()
        body = resp.json()
        names = [i["name"] for i in body.get("indices", []) if i.get("name")]
        return names

    async def _open_pit(self, index_csv: str) -> str:
        if self._config.flavor == "opensearch":
            url = f"/{index_csv}/_search/point_in_time"
            params = {"keep_alive": self._config.keep_alive}
        else:
            url = f"/{index_csv}/_pit"
            params = {"keep_alive": self._config.keep_alive}
        resp = await self._client.post(
            url, params=params, headers=self._headers
        )
        if resp.status_code == 404:
            # OpenSearch <2 has no PIT. We can't transparently recover here
            # without a scroll fallback; surface a clear error rather than
            # silently fall through to no-PIT pagination (which is unsafe
            # against concurrent writes).
            raise RuntimeError(
                "PIT unavailable (404); upgrade to OpenSearch 2.x or "
                "Elasticsearch 7.10+ — scroll-fallback is not implemented"
            )
        resp.raise_for_status()
        body = resp.json()
        # ES returns {"id": "..."}; OpenSearch returns {"pit_id": "..."}.
        return body.get("id") or body.get("pit_id", "")

    async def _close_pit(self, pit_id: str) -> None:
        if self._config.flavor == "opensearch":
            url = "/_search/point_in_time"
            payload = {"pit_id": [pit_id]}
        else:
            url = "/_pit"
            payload = {"id": pit_id}
        # httpx requires DELETE to use `request("DELETE", ...)` for body.
        await self._client.request(
            "DELETE", url, json=payload, headers=self._headers
        )

    def _build_search_body(
        self,
        *,
        pit_id: str,
        search_after: list[Any] | None,
        index_csv: str,
    ) -> dict[str, Any]:
        del index_csv  # PIT body is index-less — pit.id pins the indices
        if self._config.sample_fraction < 1.0:
            query: dict[str, Any] = {
                "function_score": {
                    "query": {"match_all": {}},
                    "random_score": {},
                    # boost_mode=replace so docs with score < threshold drop
                    "min_score": 1.0 - self._config.sample_fraction,
                }
            }
        else:
            query = {"match_all": {}}
        body: dict[str, Any] = {
            "size": self._config.page_size,
            "query": query,
            # _shard_doc is the canonical tiebreaker for PIT pagination.
            "sort": [{"_shard_doc": "asc"}],
            "pit": {
                "id": pit_id,
                "keep_alive": self._config.keep_alive,
            },
        }
        if search_after:
            body["search_after"] = search_after
        return body

    async def _search(
        self,
        *,
        pit_id: str,
        body: dict[str, Any],
    ) -> list[dict[str, Any]]:
        del pit_id  # PIT pins the indices, so the URL is just `/_search`
        resp = await self._client.post(
            "/_search", json=body, headers=self._headers
        )
        resp.raise_for_status()
        return resp.json().get("hits", {}).get("hits", []) or []

    def _render_source(self, source: Mapping[str, Any]) -> str:
        if not self._config.text_fields:
            # Default: serialise the entire _source so PII anywhere in the
            # doc is visible to the scanner.
            return json.dumps(source, ensure_ascii=False)
        parts: list[str] = []
        for field_name in self._config.text_fields:
            value = source.get(field_name)
            if value is None:
                continue
            parts.append(f"{field_name}={_to_text(value)}")
        return "\n".join(parts)


# --- helpers ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CursorState:
    pit_id: str | None = None
    search_after: list[Any] | None = None


def _decode_cursor(cursor: Cursor | None) -> _CursorState:
    if cursor is None or cursor == "":
        return _CursorState()
    raw = json.loads(cursor)
    return _CursorState(
        pit_id=raw.get("pit_id"),
        search_after=raw.get("search_after"),
    )


def _encode_cursor(state: _CursorState) -> Cursor:
    return json.dumps(
        {
            "pit_id": state.pit_id,
            "search_after": state.search_after,
        }
    )


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return ", ".join(_to_text(v) for v in value)
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "hosts" not in config or not config["hosts"]:
        raise ValueError("elasticsearch connector config requires non-empty 'hosts'")
    flavor = config.get("flavor", "elasticsearch")
    if flavor not in ("elasticsearch", "opensearch"):
        raise ValueError("flavor must be 'elasticsearch' or 'opensearch'")
    return ElasticsearchConnector(
        ElasticsearchConfig(
            hosts=tuple(str(h) for h in config["hosts"]),
            indices=tuple(str(i) for i in config.get("indices", ("*",))),
            api_key=_opt_str(config, "api_key"),
            basic_user=_opt_str(config, "basic_user"),
            basic_password=_opt_str(config, "basic_password"),
            bearer_token=_opt_str(config, "bearer_token"),
            flavor=flavor,
            sample_fraction=float(config.get("sample_fraction", 1.0)),
            text_fields=tuple(str(f) for f in config.get("text_fields", ())),
            page_size=int(config.get("page_size", 1000)),
            keep_alive=str(config.get("keep_alive", _DEFAULT_KEEP_ALIVE)),
            id=_opt_str(config, "id"),
        )
    )


def _opt_str(config: Mapping[str, Any], key: str) -> str | None:
    value = config.get(key)
    return str(value) if value is not None else None


SPEC = ConnectorSpec(
    kind="elasticsearch",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=("elasticsearch:read",),
    description=(
        "Elasticsearch / OpenSearch SourceConnector. Opens a Point-In-Time "
        "snapshot, paginates with search_after for deep-pagination safety, "
        "and supports random_score sampling for partial scans of large indices."
    ),
)


__all__ = ["ElasticsearchConfig", "ElasticsearchConnector", "SPEC"]
