"""ConfluenceConnector — Cloud + Data Center `SourceConnector` (Task #28).

Single connector kind (`confluence`) backed by two REST flavors selected
at construction time. The wire-level differences (paginator quirks,
URL prefix, 503 vs 429) live inside `api.py`; this module owns the
discover/fetch protocol surface and the storage-format → text mapping.

Pipeline per scan run:

1. Enumerate spaces (`/space`, paginated). Optional config
   `spaces=("ENG", "SEC")` narrows to an allowlist.
2. Per space, enumerate pages (`/space/{key}/content/page`,
   paginated, expanding `body.storage,version,space`). Filter
   client-side by `lastModified >= cursor` so DC installs without the
   server-side CQL filter still get incremental scans.
3. Per page, fetch comments (`/content/{id}/child/comment`,
   paginated) and attachment refs (`/content/{id}/child/attachment`,
   paginated). Each page → one `Document` whose text concatenates the
   page body + comment bodies + serialized attachment refs.

The cursor is JSON-encoded as `{"high_water": "<isoformat>"}`. Future
fields are namespaced under that JSON object so we can extend without
breaking existing checkpoints — readers ignore unknown keys, writers
preserve them.

ADR-0007 §13.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from pleno_pii_scanner.credentials.broker import Credential
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
from pleno_pii_scanner_confluence.api import (
    AuthMode,
    BasicAuth,
    BearerAuth,
    ConfluenceApi,
    Flavor,
)
from pleno_pii_scanner_confluence.storage import storage_to_text


# Connector kind exported via the `pleno_pii_scanner.connectors` entry
# point group (see pyproject.toml). One kind covers both flavors; the
# wire flavor is selected by config.
KIND = "confluence"


# `expand` query parameter for `/space/{key}/content/page`. Requests
# that the server hydrate the body, version, and space objects in the
# same payload — saves a round-trip per page (the alternative is one
# `/content/{id}?expand=body.storage` call per row, which on a 10k-page
# space is the difference between a 100s and a 30min scan).
_PAGE_EXPAND = "body.storage,version,space"


@dataclass(frozen=True, slots=True)
class ConfluenceConfig:
    """Construction config for `ConfluenceConnector`.

    `flavor` selects the wire protocol. `base_url` is required for
    both flavors — Cloud has no shared default (every site is
    `<site>.atlassian.net/wiki`) and DC is by definition self-hosted.

    `spaces` is an optional allowlist; empty means "every space the
    credential can see". `include_archived` lets the operator opt
    into archived/trashed pages (default: skip — they cannot
    receive new PII).

    `ca_bundle_path` is honored only for DC installs behind a private
    CA. Cloud uses public Mozilla certs.
    """

    flavor: Flavor
    base_url: str
    spaces: tuple[str, ...] = ()
    include_archived: bool = False
    page_size: int = 100
    request_timeout: float = 30.0
    ca_bundle_path: str | None = None
    id: str | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if self.flavor not in ("cloud", "datacenter"):
            raise ValueError(
                f"ConfluenceConfig.flavor must be 'cloud' or 'datacenter'; "
                f"got {self.flavor!r}"
            )
        if not self.base_url:
            raise ValueError(
                "ConfluenceConfig.base_url is required for both flavors "
                "(Cloud has no shared default; DC is self-hosted)"
            )
        if self.page_size < 1 or self.page_size > 250:
            # Confluence's documented per-request ceiling is 250 on v1
            # and 100 on v2. Reject obvious misconfiguration upfront so
            # the connector doesn't 400 partway through enumeration.
            raise ValueError("page_size must be between 1 and 250")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be > 0")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Strip scheme + `/wiki` so two profiles pointing at the same
        # site collapse to one id under the scheduler's bucket map.
        host = _host_from_base_url(self.base_url)
        return f"confluence-{self.flavor}:{host}"

    def resolved_tenant_id(self) -> str:
        return self.tenant_id or self.resolved_id()


class ConfluenceConnector:
    """`SourceConnector` for Confluence Cloud + Data Center.

    Owns one `ConfluenceApi` (HTTP session) for the connector lifetime.
    Discover yields one `DocumentRef` per page and stashes the page
    body + comments + attachments in a per-instance cache so `fetch()`
    can synthesize the `Document` without re-issuing the API call.
    """

    kind = KIND

    def __init__(
        self,
        config: ConfluenceConfig,
        credential: Credential,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: "Any | None" = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._credential = credential
        # Validate auth shape upfront so a misconfigured profile fails
        # at construction rather than mid-discover.
        self._auth: AuthMode = _build_auth(config.flavor, credential)
        self._api = ConfluenceApi(
            flavor=config.flavor,
            base_url=config.base_url,
            auth=self._auth,
            transport=transport,
            timeout=config.request_timeout,
            ca_bundle_path=config.ca_bundle_path,
            sleep=sleep,
        )
        # page_id -> cached payload for fetch(). Populated in discover()
        # so a discover→fetch round-trip is one HTTP request set, not
        # two.
        self._page_cache: dict[str, _PageBundle] = {}
        # Tracks the highest version.when timestamp seen during the
        # current discover() so `cursor_after_run()` can serialize it.
        self._high_water: datetime | None = None

    @property
    def api(self) -> ConfluenceApi:
        return self._api

    @property
    def config(self) -> ConfluenceConfig:
        return self._config

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    def bucket_key(self) -> BucketKey:
        """BucketKey for the global rate limiter.

        Confluence enforces rate limits per-site (Cloud) or per-app
        (DC); both map to one bucket per connector instance. We expose
        the tenant id explicitly so two connectors sharing a site (e.g.
        a "production" + "audit" profile) collapse onto the same
        bucket.
        """
        return BucketKey(
            connector_kind=self.kind,
            tenant_id=self._config.resolved_tenant_id(),
        )

    async def close(self) -> None:
        # Drop the per-page cache before tearing down the HTTP session
        # so a re-used connector instance does not retain stale page
        # bodies in memory across runs.
        self._page_cache.clear()
        await self._api.aclose()

    # ------------------------------------------------------------------
    # discover
    # ------------------------------------------------------------------

    async def discover(
        self,
        filter: SourceFilter,  # noqa: ARG002 — Confluence has no native include/exclude
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        """Yield one DocumentRef per page across every (allow-listed) space.

        `cursor` is a JSON-encoded high-water mark (`version.when`).
        Pages with `version.when <= cursor` are skipped client-side so
        the connector behaves identically against Cloud v1 (which
        supports CQL `lastModified >= ...`) and DC (which historically
        did not).

        Each yielded ref carries `_cursor` in metadata so the scheduler
        can checkpoint mid-run; the cursor advances monotonically,
        re-emitting the most-recent timestamp seen so far.
        """
        prior_high_water = _decode_cursor(cursor)
        # Reset per-discover state so a re-used connector does not leak
        # caches / high-water from the previous run.
        self._page_cache.clear()
        self._high_water = prior_high_water
        for space_key in await self._resolve_spaces():
            async for page in self._enumerate_pages(space_key):
                ref = await self._page_to_ref(space_key, page, prior_high_water)
                if ref is not None:
                    yield ref

    async def _resolve_spaces(self) -> list[str]:
        """Return the space-key list for this run.

        With an explicit allowlist we trust the config and avoid the
        list call entirely (saves an API hit on profiles pinned to one
        space). Otherwise we page `/space` and surface every key the
        credential can see.
        """
        if self._config.spaces:
            return list(self._config.spaces)
        keys: list[str] = []
        async for entry in self._api.paginate(
            "/rest/api/space", page_size=self._config.page_size
        ):
            key = entry.get("key")
            if isinstance(key, str) and key:
                keys.append(key)
        return keys

    async def _enumerate_pages(
        self, space_key: str
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Page through `/space/{key}/content/page` for a space."""
        params = {
            "expand": _PAGE_EXPAND,
        }
        async for page in self._api.paginate(
            f"/rest/api/space/{space_key}/content/page",
            params=params,
            page_size=self._config.page_size,
        ):
            yield page

    async def _page_to_ref(
        self,
        space_key: str,
        page: Mapping[str, Any],
        prior_high_water: datetime | None,
    ) -> DocumentRef | None:
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id:
            return None
        if not self._config.include_archived and _is_archived(page):
            return None
        version = page.get("version") or {}
        last_modified = _parse_iso(version.get("when"))
        # Incremental skip: if we have a checkpoint and this page's
        # version.when is not newer, skip it. The `<=` comparison is
        # intentional — a page modified at exactly the cursor instant
        # was already reported on the prior run.
        if (
            prior_high_water is not None
            and last_modified is not None
            and last_modified <= prior_high_water
        ):
            return None
        title = page.get("title") or page_id
        body = ((page.get("body") or {}).get("storage") or {}).get("value") or ""
        # Hydrate comments + attachments now (rather than in fetch())
        # because each page-listing entry is already a fully-expanded
        # payload — issuing the supplementary calls here keeps the
        # fetch() side a pure cache lookup, which makes the discover→
        # fetch concurrency story trivial.
        comments = await self._collect_comments(page_id)
        attachments = await self._collect_attachments(page_id)
        bundle = _PageBundle(
            page_id=page_id,
            space_key=space_key,
            title=str(title),
            body_storage=str(body),
            version_when=last_modified,
            comments=tuple(comments),
            attachments=tuple(attachments),
        )
        self._page_cache[page_id] = bundle
        # Advance the high-water mark in lockstep with discover order.
        if last_modified is not None:
            if self._high_water is None or last_modified > self._high_water:
                self._high_water = last_modified
        cursor_value = _encode_cursor(self._high_water) if self._high_water else None
        metadata: dict[str, str] = {
            "page_id": page_id,
            "space_key": space_key,
            "title": str(title),
            "flavor": self._config.flavor,
        }
        if cursor_value is not None:
            metadata["_cursor"] = cursor_value
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"confluence://{space_key}/{page_id}",
            native_url=_browse_url(self._config, page),
            parent_chain=(f"confluence://{space_key}",),
            content_type="text/plain",
            last_modified=last_modified,
            metadata=metadata,
        )

    async def _collect_comments(self, page_id: str) -> list[str]:
        """Page through child comments and return their storage bodies as text.

        DC + Cloud both return `/content/{id}/child/comment` with the
        same shape; the storage body lives at `body.storage.value` when
        we ask for `expand=body.storage`.
        """
        out: list[str] = []
        async for comment in self._api.paginate(
            f"/rest/api/content/{page_id}/child/comment",
            params={"expand": "body.storage"},
            page_size=self._config.page_size,
        ):
            body = ((comment.get("body") or {}).get("storage") or {}).get("value")
            if isinstance(body, str) and body:
                out.append(storage_to_text(body))
        return out

    async def _collect_attachments(self, page_id: str) -> list[tuple[str, str]]:
        """Page through child attachments; return `(title, downloadUrl)` pairs.

        We deliberately do NOT download attachment bodies — that's a
        separate (and much heavier) connector responsibility. Emitting
        the URL keeps the finding linkable without pulling tens of GB
        of attachments into the scanner's working set.
        """
        out: list[tuple[str, str]] = []
        async for attachment in self._api.paginate(
            f"/rest/api/content/{page_id}/child/attachment",
            page_size=self._config.page_size,
        ):
            title = attachment.get("title")
            links = attachment.get("_links") or {}
            href = links.get("download") or links.get("webui")
            if isinstance(title, str) and isinstance(href, str):
                out.append((title, _resolve_link(self._config, href)))
        return out

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Synthesize a text Document from the cached page bundle."""
        page_id = ref.metadata.get("page_id")
        if not page_id:
            return
        bundle = self._page_cache.get(page_id)
        if bundle is None:
            # Either the ref came from a different connector instance
            # or discover() never ran. Mirror the silent-empty idiom
            # from the bitbucket / discord connectors rather than
            # raising — the scheduler treats empty fetch as "nothing
            # to scan" and moves on.
            return
        text = _serialise_bundle(bundle)
        if not text:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
        )

    # ------------------------------------------------------------------
    # cursor
    # ------------------------------------------------------------------

    def cursor_after_run(self) -> Cursor | None:
        """Return the JSON-encoded high-water cursor, or None if nothing seen.

        `Cursor` is a `str` alias in the core API (see
        `pleno_pii_scanner.sources.base`). We return a plain string —
        never `Cursor(value=...)` — and the scheduler persists it
        verbatim into the checkpoint store.
        """
        if self._high_water is None:
            return None
        return _encode_cursor(self._high_water)


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PageBundle:
    """In-memory cache entry: everything `fetch()` needs from one page."""

    page_id: str
    space_key: str
    title: str
    body_storage: str
    version_when: datetime | None
    comments: tuple[str, ...] = ()
    attachments: tuple[tuple[str, str], ...] = ()


def _serialise_bundle(bundle: _PageBundle) -> str:
    """Render a `_PageBundle` as the text the detector pipeline scans.

    Layout (one logical block per line group):

        title=<title>
        space=<space_key>
        version=<isoformat>

        <body text>

        comment=<text>
        ...

        attachment=<title>, url=<href>
        ...
    """
    parts: list[str] = []
    parts.append(f"title={bundle.title}")
    parts.append(f"space={bundle.space_key}")
    if bundle.version_when is not None:
        parts.append(f"version={bundle.version_when.isoformat()}")
    body_text = storage_to_text(bundle.body_storage)
    if body_text:
        parts.append("")
        parts.append(body_text)
    for comment in bundle.comments:
        if comment:
            parts.append("")
            parts.append(f"comment={comment}")
    for title, href in bundle.attachments:
        parts.append(f"attachment={title}, url={href}")
    return "\n".join(parts).strip()


def _build_auth(flavor: Flavor, credential: Credential) -> AuthMode:
    """Validate the credential payload shape per flavor.

    Cloud accepts:
      * Bearer (OAuth 2.0 access_token) — preferred for new installs.
      * Basic (`email` + `api_token`) — the standard "API token" flow.
    DC accepts:
      * Bearer (Personal Access Token).
      * Basic (`username` + `password`).
    """
    payload = credential.payload
    token = payload.get("access_token") or payload.get("token")
    if isinstance(token, str) and token:
        return BearerAuth(token=token)
    if flavor == "cloud":
        username = payload.get("email") or payload.get("username")
        password = payload.get("api_token") or payload.get("password")
    else:
        username = payload.get("username")
        password = payload.get("password")
    if (
        isinstance(username, str)
        and isinstance(password, str)
        and username
        and password
    ):
        return BasicAuth(username=username, password=password)
    raise ValueError(
        f"confluence-{flavor} credential.payload requires either "
        f"`access_token`/`token` (Bearer) or "
        f"{'`email`+`api_token`' if flavor == 'cloud' else '`username`+`password`'}"
    )


def _parse_iso(value: Any) -> datetime | None:
    """Parse a Confluence ISO-8601 timestamp; tolerate `Z` suffix.

    Returns None for anything not a parseable string — defensive
    against partial payloads where `version` is present but `when` is
    missing.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_archived(page: Mapping[str, Any]) -> bool:
    """Return True if Confluence marks this page as archived/trashed.

    Cloud uses `status="archived"` / `"trashed"`; DC uses the same
    field. Defensive: missing status is treated as live.
    """
    status = page.get("status")
    return isinstance(status, str) and status in {"archived", "trashed"}


def _encode_cursor(when: datetime) -> str:
    """JSON-encode a high-water timestamp under a forward-compatible key.

    We wrap in a JSON object (rather than encoding the bare ISO string)
    so future cursor evolution — adding per-space high-water marks,
    say — does not require a breaking change in the checkpoint format:
    decoders that don't recognise new keys just ignore them.
    """
    return json.dumps({"high_water": when.isoformat()}, sort_keys=True)


def _decode_cursor(cursor: Cursor | None) -> datetime | None:
    """Parse a JSON cursor; tolerate any malformed value.

    A malformed cursor must NEVER crash the connector — the scheduler
    persists whatever string `cursor_after_run()` returns and we have
    to assume operators may roll the format forwards/back. Falling back
    to "no cursor" trades one extra full re-walk for never blocking a
    scan on a stale checkpoint.
    """
    if not cursor:
        return None
    try:
        decoded = json.loads(cursor)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    raw = decoded.get("high_water")
    return _parse_iso(raw)


def _host_from_base_url(base_url: str) -> str:
    """Strip scheme + trailing path so two profiles for the same site
    derive the same scheduler bucket id.
    """
    host = base_url
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    # Drop trailing path (`/wiki` on Cloud) so Cloud + DC ids stay
    # comparable on the host portion alone.
    return host.split("/", 1)[0].rstrip("/")


def _browse_url(config: ConfluenceConfig, page: Mapping[str, Any]) -> str | None:
    """Render a human-clickable browse URL for findings dashboards.

    Confluence embeds the canonical browse path under
    `_links.webui` on every content payload; we resolve it relative to
    the base URL.
    """
    links = page.get("_links") or {}
    webui = links.get("webui")
    if not isinstance(webui, str) or not webui:
        return None
    return _resolve_link(config, webui)


def _resolve_link(config: ConfluenceConfig, href: str) -> str:
    """Resolve a `_links.*` href against the configured base URL.

    Confluence emits these as either absolute URLs or paths relative
    to the site root; we normalise to an absolute URL the operator can
    paste into a browser. Unknown schemes pass through unchanged so we
    do not corrupt operator-injected URLs in tests.
    """
    if href.startswith("http://") or href.startswith("https://"):
        return href
    base = config.base_url.rstrip("/")
    if not href.startswith("/"):
        href = "/" + href
    return f"{base}{href}"


# ---------------------------------------------------------------------
# Factory + Spec
# ---------------------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    """Build a connector from a plain config mapping.

    The credential is fetched separately (CredentialBroker) and threaded
    through under `_credential` by the scheduler, mirroring the
    bitbucket factory contract.
    """
    cred_obj = config.get("_credential")
    if not isinstance(cred_obj, Credential):
        raise ValueError(
            "confluence factory requires a resolved Credential under "
            "config['_credential'] (set by the scheduler from CredentialBroker)"
        )
    flavor_raw = config.get("flavor", "cloud")
    if flavor_raw not in ("cloud", "datacenter"):
        raise ValueError(
            f"confluence connector config['flavor'] must be 'cloud' or "
            f"'datacenter'; got {flavor_raw!r}"
        )
    base_url = config.get("base_url")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError(
            "confluence connector config['base_url'] is required "
            "(Cloud: https://<site>.atlassian.net/wiki; "
            "DC: https://confluence.<host>)"
        )
    return ConfluenceConnector(
        ConfluenceConfig(
            flavor=flavor_raw,
            base_url=base_url,
            spaces=_string_tuple(config.get("spaces")),
            include_archived=bool(config.get("include_archived", False)),
            page_size=int(config.get("page_size", 100)),
            request_timeout=float(config.get("request_timeout", 30.0)),
            ca_bundle_path=(
                str(config["ca_bundle_path"])
                if config.get("ca_bundle_path") is not None
                else None
            ),
            id=str(config["id"]) if config.get("id") is not None else None,
            tenant_id=(
                str(config["tenant_id"])
                if config.get("tenant_id") is not None
                else None
            ),
        ),
        credential=cred_obj,
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    """Accept list/tuple of strings; reject everything else loudly.

    A bare string is almost certainly an operator typo (one space key
    instead of a list); reject so they catch it before the scan
    launches.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raise ValueError(
            "confluence connector list-typed configs (spaces) "
            "must be a list, not a bare string"
        )
    if not isinstance(value, Iterable):
        raise ValueError("confluence connector list-typed configs must be iterable")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(
                "confluence connector list-typed configs must contain non-empty strings"
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
        content_hash_delta=False,
        max_concurrent_fetches=4,
    ),
    required_scopes=(
        # Cloud OAuth 2.0 scope; DC PATs and Cloud API tokens inherit
        # the user's space permissions. Surface only the read-only
        # scopes here so the operator's CI step does not over-grant.
        "read:confluence-content.summary",
        "read:confluence-content.all",
        "read:confluence-space.summary",
    ),
    description=(
        "Atlassian Confluence Cloud + Data Center connector. "
        "Single kind, two wire flavors (`flavor=cloud|datacenter`). "
        "Space + page enumeration with storage-format XHTML→text "
        "conversion (preserves rich-text-body inside macros, drops "
        "macro parameters); comments + attachment refs concatenated; "
        "incremental cursor on `version.when`; 429/503 backoff via "
        "Retry-After; private-CA support for DC installs (ca_bundle_path). "
        "ADR-0007 §13."
    ),
)


__all__ = [
    "KIND",
    "SPEC",
    "ConfluenceConfig",
    "ConfluenceConnector",
]
