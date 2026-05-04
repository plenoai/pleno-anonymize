"""SharePoint SourceConnector — Microsoft Graph drive delta + Sites.Selected.

Pipeline:

  1. Acquire an Entra v2 application access token. Two modes:
       - client_secret (classic confidential client)
       - federated_token (jwt-bearer client_assertion; workload identity)
     The result is cached until 5 minutes before `expires_in`.
  2. Enumerate sites:
       - if `sites=()`, GET /v1.0/sites?search=*  (every site the
         app has been granted)
       - else use the explicit allowlist (site IDs or
         hostname:server-relative-url paths resolved through
         /v1.0/sites/{path}).
  3. Per site, GET /v1.0/sites/{site-id}/drives → document libraries.
  4. Per drive, run /root/delta. Fresh runs start at the bare endpoint;
     resumes use the previously stored `@odata.deltaLink` verbatim.
     Folders are filtered out client-side; only `file` items become refs.
  5. If `include_lists=True`, additionally enumerate
     /v1.0/sites/{site-id}/lists and yield one ref per list item.
  6. Each emitted ref carries `etag` so the scheduler can short-circuit
     on unchanged content via `content_hash_delta=True`.
  7. The new `@odata.deltaLink` per drive is collected and serialised
     into the run cursor (JSON object keyed by `<site-id>/<drive-id>`).

Sites.Selected is the *narrowest* SharePoint app permission. The app
gets nothing until an admin runs `POST /sites/{id}/permissions` granting
this client_id read access to that specific site. We therefore expect
operators to pin `sites=` to the granted set; falling back to
`?search=*` is supported but will be empty for most tenants.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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


# Microsoft Graph v1 base. The connector relies on absolute deltaLink
# URLs returned by the service for resume, so the base only matters for
# initial requests.
_GRAPH_BASE = "https://graph.microsoft.com"

# Entra v2 client-credentials token endpoint template. Tenant-scoped
# because Sites.Selected only resolves against the granting tenant.
_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# JWT-bearer client_assertion grant per RFC 7521/7523. Required when
# we substitute a federated OIDC JWT for a client_secret.
_CLIENT_ASSERTION_TYPE = (
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
)

# Default Graph scope for application credentials. `/.default` requests
# the union of pre-consented application permissions registered on the
# app — Sites.Selected + Files.Read.All in our case.
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Re-mint the bearer 5 minutes before its declared `expires_in` so an
# in-flight request never carries a token that expires mid-flight.
_TOKEN_SKEW_SECONDS = 5 * 60


@dataclass(frozen=True, slots=True)
class SharePointConfig:
    """Construction config for `SharePointConnector`.

    `tenant_id` and `client_id` identify the Entra app. Exactly one
    of `client_secret` or `federated_token` provides the credential
    half — fail loudly when neither or both is set so a misconfigured
    deployment never silently fails open.

    `sites` is an allowlist of site IDs OR `hostname:/sites/<name>`
    paths. Empty means "every site `?search=*` returns" — that is
    almost never what you want with Sites.Selected; pin it.

    `max_file_size_bytes` bounds the per-file body that `fetch()` will
    download. Larger files still surface as refs (so the operator can
    see what was skipped) but `fetch()` returns no Document.
    """

    tenant_id: str
    client_id: str
    client_secret: str | None = None
    federated_token: str | None = None
    sites: tuple[str, ...] = ()
    include_lists: bool = False
    max_file_size_bytes: int = 100 * 1024 * 1024
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if not self.client_id:
            raise ValueError("client_id must be non-empty")
        has_secret = bool(self.client_secret)
        has_federated = bool(self.federated_token)
        if has_secret == has_federated:
            # Both or neither: catastrophic misconfig. Both means the
            # operator may not realise which credential is in use;
            # neither means we'd silently send unauthenticated calls.
            raise ValueError(
                "exactly one of client_secret or federated_token must be set"
            )
        if self.max_file_size_bytes < 0:
            raise ValueError("max_file_size_bytes must be >= 0")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Tenant + client_id are not secret; site set bounds scope.
        suffix = "+".join(sorted(self.sites)) if self.sites else "*"
        return f"sharepoint:{self.tenant_id}:{self.client_id}:{suffix}"


@dataclass(slots=True)
class _CachedBearer:
    token: str
    expires_at: float


class SharePointConnector:
    """Read-only SourceConnector for SharePoint via Microsoft Graph."""

    kind = "sharepoint"

    def __init__(
        self,
        config: SharePointConfig,
        *,
        client: httpx.AsyncClient | None = None,
        now: "callable[[], float] | None" = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=_GRAPH_BASE, timeout=30.0
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._now = now or time.time
        self._cached: _CachedBearer | None = None
        self._token_lock = asyncio.Lock()
        # Per-drive deltaLink captured during discover, so cursor_after_run
        # can serialise it into the next-run cursor.
        self._delta_links: dict[str, str] = {}
        # Drive lookup so fetch() can hit /drives/{id}/items/{id}/content
        # without re-walking sites.
        self._drive_index: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )

    # --- discover / fetch ---------------------------------------------

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        prior = _decode_cursor(cursor)
        sites = await self._list_sites()
        for site in sites:
            site_id = site["id"]
            site_name = site.get("name") or site.get("displayName") or site_id
            drives = await self._list_drives(site_id)
            for drive in drives:
                drive_id = drive["id"]
                drive_name = drive.get("name", drive_id)
                key = f"{site_id}/{drive_id}"
                self._drive_index[drive_id] = drive_id
                resume = prior.get(key)
                async for ref in self._walk_drive_delta(
                    site_id=site_id,
                    site_name=site_name,
                    drive_id=drive_id,
                    drive_name=drive_name,
                    resume_link=resume,
                    filter=filter,
                ):
                    yield ref
            if self._config.include_lists:
                async for ref in self._walk_lists(
                    site_id=site_id, site_name=site_name, filter=filter
                ):
                    yield ref

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        kind = ref.metadata.get("kind")
        if kind == "file":
            async for doc in self._fetch_file(ref):
                yield doc
        elif kind == "list_item":
            async for doc in self._fetch_list_item(ref):
                yield doc
        # else: silently no-op — stale ref shape from a different connector

    def cursor_after_run(self) -> Cursor | None:
        """Persisted resume token: JSON {site/drive: deltaLink}."""
        if not self._delta_links:
            return None
        return json.dumps(dict(self._delta_links), sort_keys=True)

    async def close(self) -> None:
        self._delta_links.clear()
        self._drive_index.clear()
        self._cached = None
        if self._owns_client:
            await self._client.aclose()

    # --- internals: token --------------------------------------------

    async def _bearer(self) -> str:
        cached = self._cached
        if (
            cached is not None
            and cached.expires_at - _TOKEN_SKEW_SECONDS > self._now()
        ):
            return cached.token
        async with self._token_lock:
            cached = self._cached
            if (
                cached is not None
                and cached.expires_at - _TOKEN_SKEW_SECONDS > self._now()
            ):
                return cached.token
            fresh = await self._exchange_token()
            self._cached = fresh
            return fresh.token

    async def _exchange_token(self) -> _CachedBearer:
        data: dict[str, str] = {
            "client_id": self._config.client_id,
            "scope": _DEFAULT_SCOPE,
            "grant_type": "client_credentials",
        }
        if self._config.client_secret is not None:
            data["client_secret"] = self._config.client_secret
        else:
            # federated_token is enforced non-None by config validation.
            assert self._config.federated_token is not None
            data["client_assertion_type"] = _CLIENT_ASSERTION_TYPE
            data["client_assertion"] = self._config.federated_token
        url = _TOKEN_URL.format(tenant=self._config.tenant_id)
        resp = await self._client.post(
            url,
            data=data,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        token = payload["access_token"]
        expires_in = payload.get("expires_in", 3600)
        return _CachedBearer(
            token=token, expires_at=self._now() + float(expires_in)
        )

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self._bearer()}"}

    # --- internals: graph requests -----------------------------------

    async def _get_json(
        self, path_or_url: str, *, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = await self._auth_headers()
        resp = await self._client.get(path_or_url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _list_sites(self) -> list[dict[str, Any]]:
        if self._config.sites:
            out: list[dict[str, Any]] = []
            for site_ref in self._config.sites:
                # `hostname:/sites/foo` → resolve via /sites/{path}.
                # Plain id → use it verbatim.
                if ":" in site_ref and "/" in site_ref:
                    payload = await self._get_json(f"/v1.0/sites/{site_ref}")
                    out.append(payload)
                else:
                    out.append({"id": site_ref, "name": site_ref})
            return out
        payload = await self._get_json("/v1.0/sites", params={"search": "*"})
        return payload.get("value", []) or []

    async def _list_drives(self, site_id: str) -> list[dict[str, Any]]:
        payload = await self._get_json(f"/v1.0/sites/{site_id}/drives")
        return payload.get("value", []) or []

    async def _walk_drive_delta(
        self,
        *,
        site_id: str,
        site_name: str,
        drive_id: str,
        drive_name: str,
        resume_link: str | None,
        filter: SourceFilter,
    ) -> AsyncIterator[DocumentRef]:
        # Initial run hits the bare delta endpoint; resume re-uses the
        # absolute deltaLink the service handed back last time.
        next_url: str | None = (
            resume_link
            or f"/v1.0/sites/{site_id}/drives/{drive_id}/root/delta"
        )
        while next_url is not None:
            payload = await self._get_json(next_url)
            for item in payload.get("value", []) or []:
                # Folders carry a `folder` facet; files carry `file`.
                # Skip folders entirely — we only scan leaf payloads.
                if item.get("folder") is not None:
                    continue
                if item.get("file") is None:
                    continue
                ref = self._build_file_ref(
                    item=item,
                    site_id=site_id,
                    site_name=site_name,
                    drive_id=drive_id,
                    drive_name=drive_name,
                )
                if not _passes_filter(ref.path, filter):
                    continue
                yield ref
            delta_link = payload.get("@odata.deltaLink")
            if delta_link:
                self._delta_links[f"{site_id}/{drive_id}"] = delta_link
                # deltaLink is terminal: the page also carries no
                # @odata.nextLink, so the loop exits naturally.
                next_url = None
                continue
            next_url = payload.get("@odata.nextLink")

    def _build_file_ref(
        self,
        *,
        item: Mapping[str, Any],
        site_id: str,
        site_name: str,
        drive_id: str,
        drive_name: str,
    ) -> DocumentRef:
        item_id = str(item.get("id", ""))
        name = item.get("name", item_id)
        parent = item.get("parentReference") or {}
        parent_path = ""
        if isinstance(parent, Mapping):
            raw = parent.get("path", "")
            if isinstance(raw, str):
                # Graph returns `/drive/root:/folder/sub` — strip the
                # bookkeeping prefix so the rendered path is clean.
                _, _, parent_path = raw.partition("root:")
                parent_path = parent_path.lstrip("/")
        full_path = "/".join(
            p for p in (site_name, drive_name, parent_path, name) if p
        )
        size = item.get("size")
        file_facet = item.get("file") or {}
        mime = (
            file_facet.get("mimeType")
            if isinstance(file_facet, Mapping)
            else None
        ) or "application/octet-stream"
        etag = item.get("eTag") or item.get("cTag")
        last_modified_str = item.get("lastModifiedDateTime")
        last_modified = _parse_dt(last_modified_str)
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=full_path,
            native_url=item.get("webUrl"),
            content_type=str(mime),
            size=int(size) if isinstance(size, (int, float)) else None,
            etag=str(etag) if etag is not None else None,
            last_modified=last_modified,
            metadata={
                "kind": "file",
                "site_id": site_id,
                "drive_id": drive_id,
                "item_id": item_id,
                "name": str(name),
            },
        )

    async def _walk_lists(
        self,
        *,
        site_id: str,
        site_name: str,
        filter: SourceFilter,
    ) -> AsyncIterator[DocumentRef]:
        payload = await self._get_json(f"/v1.0/sites/{site_id}/lists")
        for lst in payload.get("value", []) or []:
            list_id = str(lst.get("id", ""))
            list_name = lst.get("displayName") or lst.get("name") or list_id
            items_payload = await self._get_json(
                f"/v1.0/sites/{site_id}/lists/{list_id}/items",
                params={"expand": "fields"},
            )
            for item in items_payload.get("value", []) or []:
                item_id = str(item.get("id", ""))
                full = f"{site_name}/lists/{list_name}/{item_id}"
                if not _passes_filter(full, filter):
                    continue
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=full,
                    native_url=item.get("webUrl"),
                    content_type="text/plain",
                    etag=item.get("eTag"),
                    last_modified=_parse_dt(item.get("lastModifiedDateTime")),
                    metadata={
                        "kind": "list_item",
                        "site_id": site_id,
                        "list_id": list_id,
                        "list_name": str(list_name),
                        "item_id": item_id,
                    },
                )

    # --- internals: fetch --------------------------------------------

    async def _fetch_file(
        self, ref: DocumentRef
    ) -> AsyncIterator[Document | DocumentChunk]:
        if (
            ref.size is not None
            and ref.size > self._config.max_file_size_bytes
        ):
            # Surface the skip via empty fetch — the discover ref still
            # exists for auditing.
            return
        drive_id = ref.metadata.get("drive_id")
        item_id = ref.metadata.get("item_id")
        if not drive_id or not item_id:
            return
        headers = await self._auth_headers()
        resp = await self._client.get(
            f"/v1.0/drives/{drive_id}/items/{item_id}/content",
            headers=headers,
            follow_redirects=True,
        )
        resp.raise_for_status()
        body = resp.content
        # Decide text vs binary by attempting UTF-8 decode for content
        # types that typically carry text. Anything else is binary —
        # the ContentExtractor (#8) handles MIME-aware parsing.
        ctype = ref.content_type.lower()
        text_like = ctype.startswith("text/") or ctype in {
            "application/json",
            "application/xml",
            "application/x-yaml",
        }
        text: str | None = None
        binary: bytes | None = None
        if text_like:
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                binary = body
        else:
            binary = body
        yield Document(
            ref=ref,
            text=text,
            binary=binary,
            fetched_at=datetime.now(UTC),
            content_hash=ref.etag,
        )

    async def _fetch_list_item(
        self, ref: DocumentRef
    ) -> AsyncIterator[Document | DocumentChunk]:
        site_id = ref.metadata.get("site_id")
        list_id = ref.metadata.get("list_id")
        item_id = ref.metadata.get("item_id")
        if not site_id or not list_id or not item_id:
            return
        payload = await self._get_json(
            f"/v1.0/sites/{site_id}/lists/{list_id}/items/{item_id}",
            params={"expand": "fields"},
        )
        fields = payload.get("fields") or {}
        if not isinstance(fields, Mapping):
            fields = {}
        text = _serialise_fields(fields)
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            content_hash=ref.etag,
        )


# --- helpers ------------------------------------------------------


def _passes_filter(path: str, filter: SourceFilter) -> bool:
    if filter.include and not _matches_any(path, filter.include):
        return False
    if filter.exclude and _matches_any(path, filter.exclude):
        return False
    return True


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _decode_cursor(cursor: Cursor | None) -> dict[str, str]:
    """Parse a JSON cursor into the per-drive deltaLink map.

    Cursor is `str` in the core API. We persist a JSON object and
    fall back to empty on any decode failure — a stale cursor format
    triggers a fresh delta walk rather than a crash.
    """
    if not cursor:
        return {}
    try:
        decoded = json.loads(cursor)
    except (ValueError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(k): str(v) for k, v in decoded.items()}


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Graph emits ISO 8601 with trailing Z. fromisoformat in 3.11+
        # accepts the Z suffix natively.
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _serialise_fields(fields: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if key.startswith("@") or key.startswith("_"):
            # Skip Graph bookkeeping (@odata.* and the internal
            # underscore-prefixed system columns).
            continue
        parts.append(f"{key}={value}")
    return "\n".join(parts)


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "tenant_id" not in config:
        raise ValueError("sharepoint connector config requires 'tenant_id'")
    if "client_id" not in config:
        raise ValueError("sharepoint connector config requires 'client_id'")
    return SharePointConnector(
        SharePointConfig(
            tenant_id=str(config["tenant_id"]),
            client_id=str(config["client_id"]),
            client_secret=(
                str(config["client_secret"])
                if config.get("client_secret") is not None
                else None
            ),
            federated_token=(
                str(config["federated_token"])
                if config.get("federated_token") is not None
                else None
            ),
            sites=tuple(str(s) for s in config.get("sites", ())),
            include_lists=bool(config.get("include_lists", False)),
            max_file_size_bytes=int(
                config.get("max_file_size_bytes", 100 * 1024 * 1024)
            ),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="sharepoint",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=True,
        content_hash_delta=True,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=("Sites.Selected", "Files.Read.All"),
    description=(
        "Microsoft SharePoint SourceConnector. Walks every document "
        "library in every Sites.Selected-granted site via Microsoft "
        "Graph; uses /root/delta for incremental resume and surfaces "
        "files (and optionally list items) as Documents. Supports "
        "client_secret and federated (workload identity) auth."
    ),
)


__all__ = ["SPEC", "SharePointConfig", "SharePointConnector"]
