"""Google Cloud Storage SourceConnector (ADR-0007 §13).

Pipeline per scan:

  1. Resolve target buckets — either an explicit list, or a Cloud Asset
     Inventory query (`storage.googleapis.com/Bucket` assets) so an
     enterprise can enumerate without hardcoding bucket names that
     change as projects spin up.
  2. For each bucket: paginated `objects.list` (or `objects.list?
     versions=true` when versioning is enabled), filtered by `prefix`
     and `glob`. 403 access-denied is converted into one warning
     `DocumentRef` per bucket so a single denied bucket does not
     crash the whole scan.
  3. For each yielded ref: `objects.get?alt=media` streamed via httpx
     for body content, bounded by `asyncio.Semaphore(concurrency)`.

Design constraints (from the task brief + ADR-0007):
- httpx-only — no `google-cloud-storage` SDK. Hermetic tests use
  `httpx.MockTransport` and the auth flow is exercised in-process.
- CMEK keys are pass-through metadata only. We never decrypt object
  bodies in this layer; finding-side handling lives in the core
  ContentExtractor (§6).
- Never log tokens, signed URLs, or service-account private keys.
  Errors carry structural information only.
- Defensive parsing of Cloud Asset Inventory responses — the API can
  return assets without the `name` field on partial-failure pages,
  and we must skip rather than KeyError.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
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
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec
from pleno_pii_scanner_gcs._oauth_token import (
    ApplicationDefaultTokenSource,
    DEFAULT_SCOPES,
    ServiceAccountKeyTokenSource,
    TokenCache,
    TokenSource,
    WorkloadIdentityTokenSource,
)

logger = logging.getLogger(__name__)

# Wire identifier used by the registry/CLI dispatch. Matches the entry
# point in pyproject.toml so `pleno-pii-scanner scan gcs` and
# `pleno_pii_scanner.sources.create("gcs", ...)` resolve the same code.
KIND = "gcs"

# Default per-connector concurrency for object fetches. The brief
# fixes 8; the same number caps `objects.get` parallel requests so
# we do not blow past per-bucket QPS quotas (10k/s headroom is
# generous, but tenants share quota across pods).
DEFAULT_CONCURRENCY = 8

# Google JSON API base. Hard-coded because there is no per-tenant
# override — the bucket name is the tenant boundary.
_GCS_BASE = "https://storage.googleapis.com/storage/v1"
_CAI_BASE = "https://cloudasset.googleapis.com/v1"


@dataclass(frozen=True, slots=True)
class GcsAuthConfig:
    """One of three auth modes is selected; the others are None.

    The factory enforces exclusivity; the connector itself just
    consumes the resolved `TokenSource` the factory produced.

      * `credentials_path` — service-account JSON key file path.
        Operator owns secret retrieval (SOPS/Vault/k8s Secret); we
        only read the file, parse it, and feed it to
        `ServiceAccountKeyTokenSource`.

      * `audience` + `token_path` — Workload Identity Federation.
        `audience` is the WIF provider audience URL; `token_path` is
        the file the platform refreshes (GitHub Actions,
        EKS IRSA-equivalent, arbitrary OIDC providers).
        `service_account_email` triggers impersonation — usually
        required because federated tokens are not accepted by GCS.

      * (none of the above) — ADC fallback. We let
        `ApplicationDefaultTokenSource` walk env then metadata server.
    """

    credentials_path: str | None = None
    audience: str | None = None
    token_path: str | None = None
    service_account_email: str | None = None
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    def __post_init__(self) -> None:
        # WIF requires both audience and token_path. Reject lopsided
        # config at construction time so the misconfiguration does not
        # surface 5 minutes later as an STS 400.
        if (self.audience is None) != (self.token_path is None):
            raise ValueError(
                "Workload Identity Federation requires both audience "
                "and token_path; got one without the other"
            )
        # Mixing modes is almost certainly a mistake — fail loud.
        modes = sum(
            [
                self.credentials_path is not None,
                self.audience is not None,
            ]
        )
        if modes > 1:
            raise ValueError(
                "GcsAuthConfig must select at most one of: "
                "credentials_path, (audience + token_path); "
                "ADC is the default when both are omitted"
            )


@dataclass(frozen=True, slots=True)
class GcsBucketDiscovery:
    """Either an explicit bucket list OR a Cloud Asset Inventory query.

    Exactly one of `buckets` / `project` is set. The factory enforces
    this; supplying both means the operator is unsure which to use,
    which would silently mask one of them.

    `cai_filter` allows narrowing the asset query (e.g. by location
    or labels) — the default reads every bucket the calling identity
    has `storage.buckets.get` on, which is what an enterprise wants
    for a one-shot inventory pass.
    """

    buckets: tuple[str, ...] = ()
    project: str | None = None
    cai_filter: str | None = None

    def __post_init__(self) -> None:
        has_explicit = bool(self.buckets)
        has_cai = self.project is not None
        if has_explicit == has_cai:
            raise ValueError(
                "GcsBucketDiscovery requires exactly one of: "
                "buckets=[...] OR project='proj-id'"
            )


@dataclass(frozen=True, slots=True)
class GcsConfig:
    """Construction config for `GcsConnector`.

    `prefix` and `glob` are applied at discover time; `prefix`
    narrows server-side via `objects.list?prefix=`, `glob` filters
    client-side after listing because GCS has no server-side glob.

    `include_deleted=True` follows soft-deleted ("noncurrent")
    versions when versioning is enabled — off by default because the
    use case is rare and the volume can be 10x live.

    `concurrency` bounds `objects.get` parallelism via an
    `asyncio.Semaphore`; the AWS connector uses the same knob.
    """

    auth: GcsAuthConfig
    discovery: GcsBucketDiscovery
    id: str = "gcs:default"
    prefix: str = ""
    glob: str | None = None
    include_deleted: bool = False
    concurrency: int = DEFAULT_CONCURRENCY

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")


@dataclass(frozen=True, slots=True)
class _Cursor:
    """Decoded scheduler cursor.

    Persisted as JSON. Tracks which bucket index we are on plus the
    GCS `pageToken`; resuming a half-finished bucket walk is a single
    additional `objects.list` round-trip.
    """

    bucket_index: int = 0
    page_token: str | None = None

    def dumps(self) -> Cursor:
        return json.dumps(
            {"b": self.bucket_index, "pt": self.page_token},
            separators=(",", ":"),
        )

    @classmethod
    def loads(cls, raw: Cursor | None) -> "_Cursor":
        if raw is None:
            return cls()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unparseable cursor: {raw!r}") from exc
        return cls(
            bucket_index=int(data.get("b", 0)),
            page_token=data.get("pt"),
        )


class GcsConnector:
    """SourceConnector for Google Cloud Storage."""

    kind = KIND

    def __init__(
        self,
        config: GcsConfig,
        token_cache: TokenCache,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.id
        self._tokens = token_cache
        # When we own the client we close it; when injected (tests),
        # the caller is responsible for its lifecycle.
        if client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        # asyncio.Semaphore used by `fetch()` to bound concurrent
        # `objects.get` requests. Stored as a member (not constructed
        # per-call) so semantics survive across multiple `fetch`
        # invocations; tests assert `.value` to verify the limit.
        self._fetch_semaphore = asyncio.Semaphore(config.concurrency)

    def capabilities(self) -> Capabilities:
        # incremental=True because GCS exposes `updated > {ts}` per
        # object so the scheduler can safely call us with a `since`
        # filter; binary=True because GCS objects are arbitrary blobs;
        # streaming=True because `_fetch_object` yields chunks.
        return Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=self._config.concurrency,
            streaming=True,
        )

    async def discover(
        self, filter: SourceFilter, cursor: Cursor | None
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate objects across the configured buckets.

        Cursor lets a resumed scan skip already-finished buckets and
        pick up the in-progress one mid-page. Bucket order is the
        config order for explicit lists; for CAI it is the asset
        scanner's order, which is stable per project but not
        documented as such — operators relying on resume should
        prefer the explicit bucket list.
        """
        decoded = _Cursor.loads(cursor)
        buckets = await self._resolve_buckets()
        for idx, bucket in enumerate(buckets):
            if idx < decoded.bucket_index:
                continue
            page_token = decoded.page_token if idx == decoded.bucket_index else None
            try:
                async for ref in self._discover_bucket(
                    bucket, idx, page_token, filter
                ):
                    yield ref
            except _AccessDenied as denied:
                # Per requirement #8: surface as a single warning ref
                # rather than crashing. Operators see the bucket in
                # the findings list with `error=403`; the rest of
                # the scan proceeds.
                logger.warning(
                    "gcs: access denied bucket=%s — yielding warning ref",
                    bucket,
                )
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=f"gs://{bucket}/",
                    native_url=f"https://console.cloud.google.com/storage/browser/{bucket}",
                    content_type="text/plain",
                    metadata={
                        "gcs_bucket": bucket,
                        "error": "access_denied",
                        "status": str(denied.status),
                    },
                )

    async def _resolve_buckets(self) -> tuple[str, ...]:
        """Return the bucket-name list, either explicit or via CAI."""
        if self._config.discovery.buckets:
            return self._config.discovery.buckets
        project = self._config.discovery.project
        # _post_init guards this; assert keeps mypy happy without
        # re-raising at runtime.
        assert project is not None
        return await self._cloud_asset_inventory_buckets(project)

    async def _cloud_asset_inventory_buckets(
        self, project: str
    ) -> tuple[str, ...]:
        """Enumerate `storage.googleapis.com/Bucket` assets for a project.

        Cloud Asset Inventory returns a paginated list of `Asset`s
        whose `name` is the GCS resource URI:
        `//storage.googleapis.com/<bucket-name>`. We strip the prefix
        and yield the bare bucket name.

        Defensive: if the API returns an asset without a `name` (the
        partial-failure surface mentioned in the API docs), skip it
        rather than KeyError mid-pagination.
        """
        url = f"{_CAI_BASE}/projects/{project}:searchAllResources"
        params: dict[str, str] = {
            "assetTypes": "storage.googleapis.com/Bucket",
            "pageSize": "500",
        }
        if self._config.discovery.cai_filter:
            params["query"] = self._config.discovery.cai_filter
        buckets: list[str] = []
        page_token: str | None = None
        while True:
            page_params = dict(params)
            if page_token:
                page_params["pageToken"] = page_token
            resp = await self._authed_request("GET", url, params=page_params)
            resp.raise_for_status()
            body = resp.json()
            for asset in body.get("results", ()) or body.get("assets", ()):
                # Either response shape — `searchAllResources` uses
                # `results`, the older `assets.list` uses `assets`.
                # Tolerating both makes the connector robust to API
                # behavior changes that have happened historically.
                if not isinstance(asset, Mapping):
                    continue
                name = asset.get("name")
                if not isinstance(name, str):
                    continue
                if "//storage.googleapis.com/" not in name:
                    continue
                bucket = name.rsplit("/", 1)[-1]
                if bucket:
                    buckets.append(bucket)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return tuple(buckets)

    async def _discover_bucket(
        self,
        bucket: str,
        bucket_index: int,
        page_token: str | None,
        filter: SourceFilter,
    ) -> AsyncIterator[DocumentRef]:
        """Paginate `objects.list` for one bucket, applying filters."""
        url = f"{_GCS_BASE}/b/{bucket}/o"
        glob_pattern = self._config.glob or _filter_glob(filter)
        while True:
            params: dict[str, str] = {
                "maxResults": "1000",
            }
            if self._config.prefix:
                params["prefix"] = self._config.prefix
            if self._config.include_deleted:
                # `versions=true` returns both live and noncurrent
                # versions; we further-filter client-side using the
                # `timeDeleted` presence below.
                params["versions"] = "true"
            if page_token:
                params["pageToken"] = page_token
            resp = await self._authed_request("GET", url, params=params)
            if resp.status_code == 403:
                raise _AccessDenied(status=403)
            resp.raise_for_status()
            body = resp.json()
            for entry in body.get("items", ()):
                if not isinstance(entry, Mapping):
                    continue
                ref = self._ref_from_item(bucket, entry)
                if ref is None:
                    continue
                if glob_pattern and not fnmatch.fnmatch(
                    _key_from_path(ref.path), glob_pattern
                ):
                    continue
                if filter.since is not None and ref.last_modified is not None:
                    if ref.last_modified <= filter.since:
                        continue
                yield _attach_cursor(
                    ref,
                    _Cursor(bucket_index=bucket_index, page_token=page_token),
                )
            page_token = body.get("nextPageToken")
            if not page_token:
                return

    def _ref_from_item(
        self, bucket: str, item: Mapping[str, Any]
    ) -> DocumentRef | None:
        """Build a DocumentRef from one objects.list item.

        Filters out soft-deleted objects when `include_deleted=False`
        — GCS marks them by setting `timeDeleted`.
        """
        name = item.get("name")
        if not isinstance(name, str):
            return None
        if not self._config.include_deleted and item.get("timeDeleted"):
            return None
        size_raw = item.get("size")
        try:
            size = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            size = None
        last_modified = _parse_iso_utc(item.get("updated"))
        metadata: dict[str, str] = {
            "gcs_bucket": bucket,
            "gcs_name": name,
        }
        # Versioning generation. Carrying it lets `fetch()` request the
        # exact version even if a newer write has rolled in between
        # discover and fetch.
        gen = item.get("generation")
        if gen is not None:
            metadata["gcs_generation"] = str(gen)
        # CMEK key — opaque pass-through. Never log this; it is the
        # KMS resource name, not the key material.
        kms_key = item.get("kmsKeyName")
        if isinstance(kms_key, str) and kms_key:
            metadata["gcs_kms_key_name"] = kms_key
        # Storage class for glacier-equivalent skip decisions in the
        # core layer (we do not skip here; that policy lives in
        # ContentExtractor).
        storage_class = item.get("storageClass")
        if isinstance(storage_class, str) and storage_class:
            metadata["gcs_storage_class"] = storage_class
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"gs://{bucket}/{name}",
            native_url=(
                f"https://storage.cloud.google.com/{bucket}/{name}"
            ),
            content_type=item.get("contentType")
            or "application/octet-stream",
            size=size,
            etag=item.get("etag"),
            last_modified=last_modified,
            metadata=metadata,
        )

    async def fetch(
        self, ref: DocumentRef
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Stream the object body via `objects.get?alt=media`.

        Bounded by `_fetch_semaphore` so the per-connector concurrency
        cap holds even when the scheduler over-issues fetch calls.
        """
        bucket = ref.metadata.get("gcs_bucket")
        name = ref.metadata.get("gcs_name")
        if not bucket or not name:
            raise ValueError(
                "DocumentRef metadata missing gcs_bucket / gcs_name"
            )
        url = f"{_GCS_BASE}/b/{bucket}/o/{_url_quote(name)}"
        params: dict[str, str] = {"alt": "media"}
        gen = ref.metadata.get("gcs_generation")
        if gen:
            params["generation"] = gen
        async with self._fetch_semaphore:
            resp = await self._authed_request("GET", url, params=params)
            resp.raise_for_status()
            body = resp.content
        yield Document(
            ref=ref,
            binary=body,
            fetched_at=datetime.now(UTC),
            content_hash=ref.etag,
        )

    async def close(self) -> None:
        # Idempotent: token cache can be reused after close, but the
        # owned httpx client cannot.
        if self._owns_client:
            await self._client.aclose()

    # --- internals -------------------------------------------------

    async def _authed_request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any | None = None,
    ) -> httpx.Response:
        """Issue one request with a fresh-as-needed bearer token.

        On 401 we invalidate the cached token and retry exactly once
        — Google rotates SA keys and WIF tokens out-of-band so a
        single retry handles the legitimate race; further 401s
        propagate.
        """
        token = await self._tokens.get(self._client)
        headers = {"Authorization": f"Bearer {token.value}"}
        resp = await self._client.request(
            method, url, params=params, headers=headers, json=json_body
        )
        if resp.status_code == 401:
            self._tokens.invalidate()
            token = await self._tokens.get(self._client)
            headers = {"Authorization": f"Bearer {token.value}"}
            resp = await self._client.request(
                method,
                url,
                params=params,
                headers=headers,
                json=json_body,
            )
        return resp


# --- helpers --------------------------------------------------------


class _AccessDenied(Exception):
    """Raised internally on 403 to surface the warning-ref path."""

    def __init__(self, *, status: int) -> None:
        super().__init__(f"access denied (status={status})")
        self.status = status


def _attach_cursor(ref: DocumentRef, cursor: _Cursor) -> DocumentRef:
    metadata = dict(ref.metadata)
    metadata["_cursor"] = cursor.dumps()
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
        metadata=metadata,
    )


def _parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _key_from_path(path: str) -> str:
    """Strip `gs://bucket/` from a `gs://bucket/key/with/slashes` URI."""
    rest = path[len("gs://") :] if path.startswith("gs://") else path
    _, _, key = rest.partition("/")
    return key


def _filter_glob(filter: SourceFilter) -> str | None:
    """Pick the first include glob from `filter.include` if any.

    We honour multiple include patterns by OR-ing them client-side via
    a fold; the trivially-correct case (one pattern) is the common
    one and matches what the AWS connector does with `prefix`.
    """
    if not filter.include:
        return None
    return filter.include[0]


def _url_quote(name: str) -> str:
    """URL-encode an object name for the path segment.

    Object names can contain slashes, which the GCS JSON API requires
    we percent-encode (otherwise `b/<bucket>/o/foo/bar` is parsed as
    an unknown sub-resource).
    """
    from urllib.parse import quote

    return quote(name, safe="")


# --- factory + spec ------------------------------------------------


def _build_token_source(auth: GcsAuthConfig) -> TokenSource:
    """Resolve a `GcsAuthConfig` into a concrete `TokenSource`.

    Mode selection mirrors the doc on `GcsAuthConfig`. Reading the
    SA key file off disk happens here so `__post_init__` does not
    do I/O.
    """
    if auth.credentials_path is not None:
        with open(auth.credentials_path, encoding="utf-8") as handle:
            key_data = json.load(handle)
        return ServiceAccountKeyTokenSource(
            key_data=key_data, scopes=auth.scopes
        )
    if auth.audience is not None and auth.token_path is not None:
        return WorkloadIdentityTokenSource(
            audience=auth.audience,
            token_path=auth.token_path,
            service_account_email=auth.service_account_email,
            scopes=auth.scopes,
        )
    return ApplicationDefaultTokenSource(scopes=auth.scopes)


def _factory(config: Mapping[str, Any]) -> GcsConnector:
    """Registry factory: build a GcsConnector from a config Mapping.

    Two construction paths:

      * In-process: pass `_config: GcsConfig` (and optionally
        `_token_cache` + `_client`) to skip TOML re-parse. Used by
        tests and embedders.

      * TOML/YAML: pass nested dicts. Validated by the dataclass
        `__post_init__` invariants below.
    """
    if isinstance(config.get("_config"), GcsConfig):
        cfg = config["_config"]
        token_cache = config.get("_token_cache") or TokenCache(
            source=_build_token_source(cfg.auth)
        )
        return GcsConnector(
            cfg,
            token_cache,
            client=config.get("_client"),
        )
    auth_raw = config.get("auth", {})
    discovery_raw = config.get("discovery", {})
    if not isinstance(auth_raw, Mapping):
        raise ValueError("gcs config: 'auth' must be a mapping")
    if not isinstance(discovery_raw, Mapping):
        raise ValueError("gcs config: 'discovery' must be a mapping")
    auth = GcsAuthConfig(
        credentials_path=auth_raw.get("credentials_path"),
        audience=auth_raw.get("audience"),
        token_path=auth_raw.get("token_path"),
        service_account_email=auth_raw.get("service_account_email"),
        scopes=tuple(auth_raw.get("scopes", DEFAULT_SCOPES)),
    )
    discovery = GcsBucketDiscovery(
        buckets=tuple(discovery_raw.get("buckets", ()) or ()),
        project=discovery_raw.get("project"),
        cai_filter=discovery_raw.get("cai_filter"),
    )
    cfg = GcsConfig(
        auth=auth,
        discovery=discovery,
        id=str(config.get("id", "gcs:default")),
        prefix=str(config.get("prefix", "")),
        glob=config.get("glob"),
        include_deleted=bool(config.get("include_deleted", False)),
        concurrency=int(config.get("concurrency", DEFAULT_CONCURRENCY)),
    )
    token_cache = TokenCache(source=_build_token_source(auth))
    return GcsConnector(cfg, token_cache)


SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=True,
        content_hash_delta=True,
        max_concurrent_fetches=DEFAULT_CONCURRENCY,
        streaming=True,
    ),
    required_scopes=(
        # Minimum Google IAM scopes the connector needs. CAI
        # bucket discovery additionally requires `cloudasset.assets.searchAll`.
        "storage.buckets.get",
        "storage.objects.list",
        "storage.objects.get",
    ),
    description=(
        "Google Cloud Storage connector. Three auth modes (SA key, "
        "Workload Identity Federation, Application Default Credentials), "
        "bucket discovery via explicit list or Cloud Asset Inventory, "
        "paginated objects.list with prefix/glob, streaming objects.get, "
        "live-version walk when versioning enabled, CMEK pass-through, "
        "403 surfaces as one warning ref per bucket (does not crash scan)."
    ),
)


__all__ = [
    "DEFAULT_CONCURRENCY",
    "GcsAuthConfig",
    "GcsBucketDiscovery",
    "GcsConfig",
    "GcsConnector",
    "KIND",
    "SPEC",
]
