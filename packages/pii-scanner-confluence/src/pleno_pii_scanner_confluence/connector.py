"""Confluence SourceConnector — Cloud v2 + DC, storage XHTML → text.

Pipeline:

  1. Resolve allowlist (`spaces=` config) to space IDs (Cloud) or
     pass-through space keys (DC).
  2. Cloud: GET /wiki/api/v2/pages?cursor=...&body-format=storage&limit=100
     (+ space-id=... when filtering).
     DC:    GET /rest/api/content?type=page&start=N&limit=100
            &expand=body.storage,version (+ spaceKey= when filtering).
  3. For each page emit a DocumentRef with path `<space-key>/<page-id>`,
     metadata `version_id`, body cached for fetch().
  4. If `include_attachments_meta=True`, also enumerate attachments
     (`/wiki/api/v2/pages/{id}/attachments` cloud, or
     `/rest/api/content/{id}/child/attachment` DC) and emit one
     metadata-only DocumentRef per attachment with `content_type` set.
  5. fetch(): yield one Document with the page body decoded from
     storage XHTML to plain text via a small recursive walker.

Incremental cursor: JSON `{"last_modified": "...", "spaces": {key: cursor}}`.
On resume, pages older than `last_modified` are skipped client-side
(the page list comes back newest-first from both APIs when sorted).

The XHTML walker handles `<p>`, `<h1..6>`, `<li>` block boundaries and
emits one line per block; `<ac:structured-macro ac:name="X">` is rendered
as `[macro X] <inner text>` so secrets stashed inside `{code}` /
`{noformat}` macros stay detectable. No BeautifulSoup dependency — the
core API is `xml.etree.ElementTree` wrapped in a small namespace shim
because storage XHTML uses the `ac:` and `ri:` Confluence namespaces
without declaring them on the body fragment.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from xml.etree import ElementTree as ET

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


# Block elements that should produce a newline boundary when flattening
# storage XHTML to plain text. Confluence uses XHTML so the canonical
# block tags from HTML5 are the right set; the connector deliberately
# stays narrow rather than copying the full block-level enum.
_BLOCK_TAGS: frozenset[str] = frozenset(
    {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "td", "th", "div", "br"}
)

# Confluence storage XHTML uses the `ac:` and `ri:` namespaces without
# declaring them on the body fragment the API returns. We wrap the
# fragment in a root element that declares both so ElementTree can parse
# without a NamespaceError.
_AC_NS = "http://atlassian.com/content"
_RI_NS = "http://atlassian.com/resource/identifier"
_AC_NAME_ATTR = f"{{{_AC_NS}}}name"


@dataclass(frozen=True, slots=True)
class ConfluenceConfig:
    """Construction config for `ConfluenceConnector`."""

    base_url: str
    email: str
    api_token: str
    spaces: tuple[str, ...] = ()
    include_attachments_meta: bool = True
    deployment: Literal["cloud", "dc"] = "cloud"
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("base_url must be non-empty")
        if not self.api_token:
            raise ValueError("api_token must be non-empty")
        if self.deployment not in ("cloud", "dc"):
            raise ValueError(
                f"deployment must be 'cloud' or 'dc'; got {self.deployment!r}"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Token is sensitive; identify by hashed (base_url, token, spaces).
        import hashlib

        h = hashlib.sha256()
        h.update(self.base_url.encode())
        h.update(b"\0")
        h.update(self.api_token.encode())
        for s in sorted(self.spaces):
            h.update(b"\0")
            h.update(s.encode())
        return f"confluence:{h.hexdigest()[:16]}"


class ConfluenceConnector:
    """Read-only SourceConnector for Atlassian Confluence (Cloud + DC)."""

    kind = "confluence"

    def __init__(
        self,
        config: ConfluenceConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=config.base_url, timeout=30.0
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        if config.deployment == "cloud":
            # HTTP basic email:api_token. httpx handles base64 encoding
            # via the `auth` kwarg per request to keep the credential
            # off any structured logging path.
            self._auth: httpx.BasicAuth | None = httpx.BasicAuth(
                config.email, config.api_token
            )
            self._headers: dict[str, str] = {"Accept": "application/json"}
        else:
            # DC PATs are bearer tokens.
            self._auth = None
            self._headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {config.api_token}",
            }
        # Cache page-body XHTML keyed by ref.path so fetch() doesn't
        # re-issue the page GET.
        self._bodies: dict[str, str] = {}
        # High-water last_modified ISO timestamp, written across discover.
        self._high_water: str | None = None

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
        prior = _decode_cursor(cursor)
        prior_lm = prior.get("last_modified")
        # Threshold for client-side incremental skip — pages with
        # last_modified <= prior_lm have already been emitted.
        threshold = _parse_iso(prior_lm) if isinstance(prior_lm, str) else None
        if self._config.deployment == "cloud":
            async for ref in self._discover_cloud(filter, threshold):
                yield ref
        else:
            async for ref in self._discover_dc(filter, threshold):
                yield ref

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        # Attachments are metadata-only: no body cached, fetch() is a no-op.
        if ref.metadata.get("kind") == "attachment":
            return
        body_xhtml = self._bodies.get(ref.path)
        if body_xhtml is None:
            return
        text = _xhtml_to_text(body_xhtml)
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            extra=dict(ref.metadata),
        )

    def cursor_after_run(self) -> Cursor | None:
        if self._high_water is None:
            return None
        return _encode_cursor({"last_modified": self._high_water})

    async def close(self) -> None:
        self._bodies.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- cloud discovery -----------------------------------------

    async def _discover_cloud(
        self,
        filter: SourceFilter,
        threshold: datetime | None,
    ) -> AsyncIterator[DocumentRef]:
        # Resolve space-key allowlist → space-id (Cloud v2 filters by id).
        space_id_to_key = await self._resolve_cloud_spaces()
        # When operator gave no allowlist, hit the global pages endpoint
        # once; otherwise issue one paged walk per space-id.
        if space_id_to_key:
            walks: list[tuple[str | None, str]] = [
                (sid, key) for sid, key in space_id_to_key.items()
            ]
        else:
            walks = [(None, "")]
        for space_id, space_key in walks:
            params: dict[str, str | int] = {
                "limit": 100,
                "body-format": "storage",
            }
            if space_id is not None:
                params["space-id"] = space_id
            cursor: str | None = None
            while True:
                if cursor is not None:
                    params["cursor"] = cursor
                body = await self._get_json("/wiki/api/v2/pages", params=params)
                for page in body.get("results", []) or []:
                    ref = self._cloud_page_to_ref(
                        page, space_key=space_key, filter=filter, threshold=threshold
                    )
                    if ref is None:
                        continue
                    yield ref
                    if self._config.include_attachments_meta:
                        async for att in self._cloud_attachments(
                            page_id=str(page["id"]),
                            page_path=ref.path,
                            filter=filter,
                        ):
                            yield att
                cursor = _next_cursor_from_links(body.get("_links", {}))
                if not cursor:
                    break

    async def _resolve_cloud_spaces(self) -> dict[str, str]:
        """Map configured space keys → space ids via /wiki/api/v2/spaces.

        Returns an empty dict when no allowlist was configured (caller
        then walks every space the credential can see).
        """
        if not self._config.spaces:
            return {}
        out: dict[str, str] = {}
        params: dict[str, str | int] = {"limit": 100}
        cursor: str | None = None
        wanted = set(self._config.spaces)
        while wanted:
            if cursor is not None:
                params["cursor"] = cursor
            body = await self._get_json("/wiki/api/v2/spaces", params=params)
            for space in body.get("results", []) or []:
                key = str(space.get("key", ""))
                if key in wanted:
                    out[str(space["id"])] = key
                    wanted.discard(key)
            cursor = _next_cursor_from_links(body.get("_links", {}))
            if not cursor:
                break
        return out

    async def _cloud_attachments(
        self,
        *,
        page_id: str,
        page_path: str,
        filter: SourceFilter,
    ) -> AsyncIterator[DocumentRef]:
        params: dict[str, str | int] = {"limit": 100}
        cursor: str | None = None
        while True:
            if cursor is not None:
                params["cursor"] = cursor
            body = await self._get_json(
                f"/wiki/api/v2/pages/{page_id}/attachments", params=params
            )
            for att in body.get("results", []) or []:
                title = str(att.get("title") or att.get("id"))
                full = f"{page_path}/attachments/{title}"
                if filter.include and not _matches_any(full, filter.include):
                    continue
                if filter.exclude and _matches_any(full, filter.exclude):
                    continue
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=full,
                    content_type=str(att.get("mediaType") or "application/octet-stream"),
                    size=int(att["fileSize"]) if isinstance(att.get("fileSize"), int) else None,
                    metadata={
                        "kind": "attachment",
                        "page_id": page_id,
                        "attachment_id": str(att.get("id", "")),
                        "title": title,
                    },
                )
            cursor = _next_cursor_from_links(body.get("_links", {}))
            if not cursor:
                break

    def _cloud_page_to_ref(
        self,
        page: Mapping[str, Any],
        *,
        space_key: str,
        filter: SourceFilter,
        threshold: datetime | None,
    ) -> DocumentRef | None:
        page_id = str(page["id"])
        # Cloud v2 returns spaceId, not spaceKey, on the page payload —
        # we already know the key from the outer walk loop, but fall back
        # to the page's own spaceId when the operator gave no allowlist.
        eff_key = space_key or str(page.get("spaceId", ""))
        full = f"{eff_key}/{page_id}"
        if filter.include and not _matches_any(full, filter.include):
            return None
        if filter.exclude and _matches_any(full, filter.exclude):
            return None
        version = page.get("version") or {}
        # Cloud v2 nests last-modified under version.createdAt.
        lm_str = str(version.get("createdAt") or "")
        lm = _parse_iso(lm_str) if lm_str else None
        if threshold is not None and lm is not None and lm <= threshold:
            return None
        if lm_str and (self._high_water is None or lm_str > self._high_water):
            self._high_water = lm_str
        body_obj = page.get("body") or {}
        storage_obj = body_obj.get("storage") or {}
        body_value = str(storage_obj.get("value", "") or "")
        self._bodies[full] = body_value
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=full,
            content_type="text/html",
            size=len(body_value),
            etag=str(version.get("number", "")),
            last_modified=lm,
            metadata={
                "kind": "page",
                "page_id": page_id,
                "space_key": eff_key,
                "version_id": str(version.get("number", "")),
                "title": str(page.get("title", "")),
            },
        )

    # --- DC discovery --------------------------------------------

    async def _discover_dc(
        self,
        filter: SourceFilter,
        threshold: datetime | None,
    ) -> AsyncIterator[DocumentRef]:
        space_keys: tuple[str | None, ...] = (
            self._config.spaces if self._config.spaces else (None,)
        )
        for space_key in space_keys:
            start = 0
            while True:
                params: dict[str, str | int] = {
                    "type": "page",
                    "start": start,
                    "limit": 100,
                    "expand": "body.storage,version",
                }
                if space_key is not None:
                    params["spaceKey"] = space_key
                body = await self._get_json("/rest/api/content", params=params)
                results = body.get("results", []) or []
                if not results:
                    break
                for page in results:
                    ref = self._dc_page_to_ref(
                        page, filter=filter, threshold=threshold
                    )
                    if ref is None:
                        continue
                    yield ref
                    if self._config.include_attachments_meta:
                        async for att in self._dc_attachments(
                            page_id=str(page["id"]),
                            page_path=ref.path,
                            filter=filter,
                        ):
                            yield att
                size = int(body.get("size", len(results)) or len(results))
                limit = int(body.get("limit", 100) or 100)
                start += size
                if size < limit:
                    break

    def _dc_page_to_ref(
        self,
        page: Mapping[str, Any],
        *,
        filter: SourceFilter,
        threshold: datetime | None,
    ) -> DocumentRef | None:
        page_id = str(page["id"])
        space_obj = page.get("space") or {}
        space_key = str(space_obj.get("key", ""))
        full = f"{space_key}/{page_id}"
        if filter.include and not _matches_any(full, filter.include):
            return None
        if filter.exclude and _matches_any(full, filter.exclude):
            return None
        version = page.get("version") or {}
        lm_str = str(version.get("when") or "")
        lm = _parse_iso(lm_str) if lm_str else None
        if threshold is not None and lm is not None and lm <= threshold:
            return None
        if lm_str and (self._high_water is None or lm_str > self._high_water):
            self._high_water = lm_str
        body_obj = page.get("body") or {}
        storage_obj = body_obj.get("storage") or {}
        body_value = str(storage_obj.get("value", "") or "")
        self._bodies[full] = body_value
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=full,
            content_type="text/html",
            size=len(body_value),
            etag=str(version.get("number", "")),
            last_modified=lm,
            metadata={
                "kind": "page",
                "page_id": page_id,
                "space_key": space_key,
                "version_id": str(version.get("number", "")),
                "title": str(page.get("title", "")),
            },
        )

    async def _dc_attachments(
        self,
        *,
        page_id: str,
        page_path: str,
        filter: SourceFilter,
    ) -> AsyncIterator[DocumentRef]:
        start = 0
        while True:
            params: dict[str, str | int] = {"start": start, "limit": 100}
            body = await self._get_json(
                f"/rest/api/content/{page_id}/child/attachment", params=params
            )
            results = body.get("results", []) or []
            if not results:
                break
            for att in results:
                title = str(att.get("title") or att.get("id"))
                full = f"{page_path}/attachments/{title}"
                if filter.include and not _matches_any(full, filter.include):
                    continue
                if filter.exclude and _matches_any(full, filter.exclude):
                    continue
                metadata_obj = att.get("metadata") or {}
                ext = att.get("extensions") or {}
                media_type = str(
                    metadata_obj.get("mediaType")
                    or (ext.get("mediaType") if isinstance(ext, Mapping) else "")
                    or "application/octet-stream"
                )
                size_val = ext.get("fileSize") if isinstance(ext, Mapping) else None
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=full,
                    content_type=media_type,
                    size=int(size_val) if isinstance(size_val, int) else None,
                    metadata={
                        "kind": "attachment",
                        "page_id": page_id,
                        "attachment_id": str(att.get("id", "")),
                        "title": title,
                    },
                )
            size = int(body.get("size", len(results)) or len(results))
            limit = int(body.get("limit", 100) or 100)
            start += size
            if size < limit:
                break

    # --- HTTP ----------------------------------------------------

    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.get(
            path,
            params=params,
            headers=self._headers,
            auth=self._auth or httpx.USE_CLIENT_DEFAULT,
        )
        resp.raise_for_status()
        return resp.json()


# --- helpers ------------------------------------------------------


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp; tolerate the `Z` suffix."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _decode_cursor(cursor: Cursor | None) -> dict[str, Any]:
    """Decode the JSON cursor; empty/garbage → fresh-scan {}."""
    if not cursor:
        return {}
    try:
        decoded = json.loads(cursor)
    except (ValueError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return decoded


def _encode_cursor(state: Mapping[str, Any]) -> str:
    return json.dumps(dict(state), sort_keys=True)


def _next_cursor_from_links(links: Mapping[str, Any]) -> str | None:
    """Pull the `cursor=` query param out of `_links.next`.

    Cloud v2 returns the next page as an opaque relative URL with the
    cursor in the query string; we surface only the cursor value so the
    caller can pass it back as `?cursor=`.
    """
    nxt = links.get("next") if isinstance(links, Mapping) else None
    if not isinstance(nxt, str) or not nxt:
        return None
    # Locate the cursor query parameter without pulling urllib.parse for
    # such a narrow use case.
    marker = "cursor="
    idx = nxt.find(marker)
    if idx < 0:
        return None
    rest = nxt[idx + len(marker) :]
    end = rest.find("&")
    return rest if end < 0 else rest[:end]


def _xhtml_to_text(xhtml: str) -> str:
    """Render Confluence storage XHTML as plain text.

    Block elements (`p`, `h1..h6`, `li`, `tr`, `td`, `th`, `div`, `br`)
    introduce newlines so paragraph and table boundaries survive the
    flatten. `ac:structured-macro` blocks emit `[macro <name>]` followed
    by their inner text on a new line.

    Returns an empty string when the body is empty or when the parser
    cannot recover anything — never raises on malformed input.
    """
    if not xhtml.strip():
        return ""
    # Wrap fragment in a root element with the Confluence namespaces
    # declared so ElementTree accepts `ac:` and `ri:` prefixed elements.
    wrapped = (
        f'<root xmlns:ac="{_AC_NS}" xmlns:ri="{_RI_NS}">'
        f"{xhtml}"
        "</root>"
    )
    try:
        root = ET.fromstring(wrapped)
    except ET.ParseError:
        # Fallback: strip everything that looks like a tag and return
        # whatever text remained. Confluence XHTML is well-formed in
        # practice, but the connector must not be the link in the chain
        # that raises on a single malformed page.
        import re

        return re.sub(r"<[^>]+>", "", xhtml).strip()
    parts: list[str] = []
    _walk(root, parts)
    # Collapse runs of blank lines → single blank line so block
    # boundaries are visible without runaway whitespace.
    out_lines: list[str] = []
    last_blank = False
    for line in "".join(parts).splitlines():
        stripped = line.strip()
        if not stripped:
            if last_blank:
                continue
            last_blank = True
            out_lines.append("")
        else:
            last_blank = False
            out_lines.append(stripped)
    return "\n".join(out_lines).strip()


def _walk(elem: ET.Element, parts: list[str]) -> None:
    """Recursive XHTML walker; appends text fragments to `parts`."""
    tag = _local(elem.tag)
    is_macro = elem.tag == f"{{{_AC_NS}}}structured-macro"
    if is_macro:
        macro_name = elem.get(_AC_NAME_ATTR) or elem.get("name") or ""
        parts.append(f"\n[macro {macro_name}]\n")
        if elem.text:
            parts.append(elem.text)
        for child in elem:
            _walk(child, parts)
        # Tail belongs to the parent's flow, not the macro.
        if elem.tail:
            parts.append(elem.tail)
        parts.append("\n")
        return
    if tag in _BLOCK_TAGS:
        parts.append("\n")
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        _walk(child, parts)
    if tag in _BLOCK_TAGS:
        parts.append("\n")
    if elem.tail:
        parts.append(elem.tail)


def _local(tag: str) -> str:
    """Strip XML namespace from `{uri}local` → `local`."""
    if tag.startswith("{"):
        return tag.partition("}")[2]
    return tag


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    for required in ("base_url", "email", "api_token"):
        if required not in config:
            raise ValueError(
                f"confluence connector config requires {required!r}"
            )
    return ConfluenceConnector(
        ConfluenceConfig(
            base_url=str(config["base_url"]),
            email=str(config["email"]),
            api_token=str(config["api_token"]),
            spaces=tuple(str(s) for s in config.get("spaces", ())),
            include_attachments_meta=bool(
                config.get("include_attachments_meta", True)
            ),
            deployment=str(config.get("deployment", "cloud")),  # type: ignore[arg-type]
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="confluence",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=("confluence:read",),
    description=(
        "Atlassian Confluence SourceConnector. Cloud v2 (cursor-paginated, "
        "HTTP basic email:api_token) + Data Center (start/limit, bearer "
        "token). Storage XHTML is flattened to plain text via "
        "xml.etree.ElementTree; structured-macro blocks surface their "
        "name and inner text. Attachments are emitted as metadata-only "
        "DocumentRefs (binary download is opt-out)."
    ),
)


__all__ = ["SPEC", "ConfluenceConfig", "ConfluenceConnector"]
