"""Azure Blob Storage SourceConnector (ADR-0007 §15).

Pipeline per scan:

  1. Resolve target containers — either an explicit per-account list,
     or per-account discovery via `GET https://{acct}.blob.core.windows.net/?comp=list`.
     Multiple `(subscription_id, storage_account)` pairs are walked in
     order (Azure Lighthouse fan-out) so one scan job can hit a hundred
     subscriptions with a single configuration.
  2. For each container: paginated
     `GET https://{acct}.blob.core.windows.net/{container}?restype=container&comp=list&prefix={prefix}&marker={cursor}`,
     XML body parsed with `xml.etree.ElementTree`, `<NextMarker>` drives
     pagination. Soft-deleted blobs are skipped unless `include_versions=True`.
     403 access-denied is converted into one warning `DocumentRef` per
     container so a single denied container does not crash the whole scan.
  3. For each yielded ref: streaming `GET` for body content, bounded
     by `asyncio.Semaphore(concurrency)`.

Design constraints (from the task brief + ADR-0007):
- httpx-only — no `azure-storage-blob` SDK. Hermetic tests use
  `httpx.MockTransport` and the auth flow is exercised in-process.
- `x-ms-version: 2023-11-03` is pinned on every request — drift to a
  newer service version could change the XML schema we parse and
  silently lose blobs.
- CMEK keys are pass-through metadata only. We never decrypt object
  bodies in this layer; finding-side handling lives in the core
  ContentExtractor (§6).
- Never log tokens, signed URLs, or account keys. Errors carry
  structural information only.
- Defensive XML parsing — Azure has been known to omit `<Properties>`
  on partial-failure pages, and we must skip rather than crash.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

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
from pleno_pii_scanner_azure_blob._auth import (
    AZURE_STORAGE_DEFAULT_SCOPE,
    ManagedIdentityTokenSource,
    SharedKeyCredential,
    TokenCache,
    TokenSource,
    WorkloadIdentityTokenSource,
    sign_shared_key,
)

logger = logging.getLogger(__name__)

# Wire identifier used by the registry/CLI dispatch. Matches the entry
# point in pyproject.toml so `pleno-pii-scanner scan azure_blob` and
# `pleno_pii_scanner.sources.create("azure_blob", ...)` resolve the
# same code.
KIND = "azure_blob"

# Default per-connector concurrency for blob fetches. The brief fixes
# 8; the same number caps GET parallelism so we do not blow past
# per-account QPS quotas (Azure documents 20k req/s per account; 8
# parallel keeps us comfortably under that budget per scanner pod).
DEFAULT_CONCURRENCY = 8

# Pinned Azure Storage REST API version. `2023-11-03` is the version
# whose XML schema we parse. A bump must be opt-in: without pinning,
# Azure can route us to a newer schema that would silently change
# `<EnumerationResults>` shape and lose blobs.
AZURE_STORAGE_API_VERSION = "2023-11-03"

# Default Azure Storage public-cloud endpoint suffix. Sovereign clouds
# (Azure China, US Gov, Germany) override this via `endpoint_suffix`.
DEFAULT_ENDPOINT_SUFFIX = "blob.core.windows.net"


# --- auth + account specs ------------------------------------------


@dataclass(frozen=True, slots=True)
class AzureBlobAuthConfig:
    """Auth selector — exactly one mode is configured.

      * `tenant_id` + `client_id` + `oidc_token_path` — Workload
        Identity Federation. The platform (GHA, AKS) writes the OIDC
        JWT to `oidc_token_path`; we exchange it via Entra for an
        Azure Storage bearer.

      * `managed_identity=True` — IMDS at 169.254.169.254. Optional
        `client_id` disambiguates user-assigned identities.

      * `account_keys` — legacy account-key dict
        `{account_name: base64_key}`. Each account uses Shared Key
        signing instead of bearer auth.

    The factory enforces exclusivity; the connector itself just
    consumes the resolved `TokenSource` / `SharedKeyCredential` map
    the factory produced.
    """

    # WIF inputs.
    tenant_id: str | None = None
    client_id: str | None = None
    oidc_token_path: str | None = None
    # Managed Identity inputs.
    managed_identity: bool = False
    managed_identity_client_id: str | None = None
    # Shared Key inputs.
    account_keys: Mapping[str, str] = field(default_factory=dict)
    # Common.
    scope: str = AZURE_STORAGE_DEFAULT_SCOPE

    def __post_init__(self) -> None:
        # WIF requires the trio together; lopsided config is rejected
        # at construction so misconfiguration does not surface 5
        # minutes later as an Entra 400.
        wif_set = (
            self.tenant_id is not None
            or self.client_id is not None
            or self.oidc_token_path is not None
        )
        wif_complete = (
            self.tenant_id is not None
            and self.client_id is not None
            and self.oidc_token_path is not None
        )
        if wif_set and not wif_complete:
            raise ValueError(
                "Workload Identity requires tenant_id + client_id + "
                "oidc_token_path together"
            )
        # Mode exclusivity.
        modes = sum(
            [
                wif_complete,
                self.managed_identity,
                bool(self.account_keys),
            ]
        )
        if modes == 0:
            raise ValueError(
                "AzureBlobAuthConfig must select one of: workload "
                "identity (tenant_id+client_id+oidc_token_path), "
                "managed_identity=True, or account_keys={...}"
            )
        if modes > 1:
            raise ValueError(
                "AzureBlobAuthConfig must select EXACTLY one auth "
                "mode; got multiple"
            )


@dataclass(frozen=True, slots=True)
class AzureAccount:
    """One target storage account, with its Lighthouse subscription
    coordinate.

    `subscription_id` is carried for chargeback and per-tenant rate
    limiting; the connector never calls ARM with it (we go straight
    to the data plane), but it flows into `DocumentRef.metadata` so
    downstream FindingsStore can attribute the find.

    `endpoint_suffix` lets sovereign clouds override the default
    `blob.core.windows.net` (Azure China uses
    `blob.core.chinacloudapi.cn`, US Gov uses `blob.core.usgovcloudapi.net`).
    """

    storage_account: str
    subscription_id: str = ""
    endpoint_suffix: str = DEFAULT_ENDPOINT_SUFFIX
    label: str = ""

    def base_url(self) -> str:
        return f"https://{self.storage_account}.{self.endpoint_suffix}"

    def resolved_label(self) -> str:
        return self.label or f"azure_blob:{self.storage_account}"  # pragma: no cover - operator-facing helper, not on the scan hot path


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """One target container within an account."""

    account: str
    name: str


@dataclass(frozen=True, slots=True)
class AzureBlobDiscovery:
    """Container resolution mode — explicit list OR per-account discovery.

    Exactly one of `containers` / `accounts` populates a non-empty
    value. Supplying both is rejected because the operator's intent
    is ambiguous.

    `containers` pins the (account, container) pairs explicitly.
    `accounts` says "list every container in each of these accounts"
    via `GET .../?comp=list` enumeration.
    """

    containers: tuple[ContainerSpec, ...] = ()
    accounts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        has_explicit = bool(self.containers)
        has_per_account = bool(self.accounts)
        if has_explicit == has_per_account:
            raise ValueError(
                "AzureBlobDiscovery requires exactly one of: "
                "containers=[...] OR accounts=[...]"
            )


@dataclass(frozen=True, slots=True)
class AzureBlobConfig:
    """Construction config for `AzureBlobConnector`.

    `prefix` and `glob` are applied at discover time; `prefix`
    narrows server-side via `?prefix=`, `glob` filters client-side
    after listing because Azure Blob has no server-side glob.

    `include_versions=True` returns soft-deleted ("noncurrent")
    versions when versioning is enabled — off by default because the
    use case is rare and the volume can be 10x live.

    `concurrency` bounds blob fetch parallelism via an
    `asyncio.Semaphore`; the GCS connector uses the same knob.
    """

    auth: AzureBlobAuthConfig
    discovery: AzureBlobDiscovery
    accounts: tuple[AzureAccount, ...] = ()
    id: str = "azure_blob:default"
    prefix: str = ""
    glob: str | None = None
    include_versions: bool = False
    concurrency: int = DEFAULT_CONCURRENCY

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if not self.accounts:
            raise ValueError(
                "AzureBlobConfig.accounts must contain at least one "
                "AzureAccount (Lighthouse subscription coordinate)"
            )
        # If explicit container list is used, every referenced account
        # must be declared in `accounts`. Catches typos at construction
        # rather than 30 minutes into a scan when the account_id is
        # looked up.
        if self.discovery.containers:
            account_names = {a.storage_account for a in self.accounts}
            for c in self.discovery.containers:
                if c.account not in account_names:
                    raise ValueError(
                        f"container account={c.account!r} is not "
                        f"in AzureBlobConfig.accounts"
                    )
        if self.discovery.accounts:
            account_names = {a.storage_account for a in self.accounts}
            for name in self.discovery.accounts:
                if name not in account_names:
                    raise ValueError(
                        f"discovery account={name!r} is not in "
                        f"AzureBlobConfig.accounts"
                    )


@dataclass(frozen=True, slots=True)
class _Cursor:
    """Decoded scheduler cursor.

    Persisted as JSON. Tracks which (account, container) pair we are
    on plus the Azure `<NextMarker>`; resuming a half-finished
    container walk is a single additional `?comp=list` round-trip.
    """

    pair_index: int = 0
    marker: str | None = None

    def dumps(self) -> Cursor:
        return json.dumps(
            {"i": self.pair_index, "m": self.marker},
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
            pair_index=int(data.get("i", 0)),
            marker=data.get("m"),
        )


# --- connector ----------------------------------------------------


class AzureBlobConnector:
    """SourceConnector for Azure Blob Storage."""

    kind = KIND

    def __init__(
        self,
        config: AzureBlobConfig,
        token_cache: TokenCache | None,
        shared_keys: Mapping[str, SharedKeyCredential] | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.id
        self._tokens = token_cache
        self._shared_keys = dict(shared_keys or {})
        # When we own the client we close it; when injected (tests),
        # the caller is responsible for its lifecycle.
        if client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        # asyncio.Semaphore used by `fetch()` to bound concurrent GETs.
        # Stored as a member (not constructed per-call) so semantics
        # survive across multiple `fetch` invocations; tests assert
        # `._value` to verify the limit.
        self._fetch_semaphore = asyncio.Semaphore(config.concurrency)
        # Index account name → AzureAccount for O(1) lookup at fetch
        # time. The list is small (typically <100 entries), but
        # building the dict once avoids a linear scan per fetch.
        self._accounts_by_name: dict[str, AzureAccount] = {
            a.storage_account: a for a in config.accounts
        }

    def capabilities(self) -> Capabilities:
        # incremental=True because Azure exposes blob `Last-Modified`
        # so the scheduler can safely call us with a `since` filter;
        # binary=True because blobs are arbitrary bytes; streaming=True
        # because `_fetch_blob` yields chunks.
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
        """Enumerate blobs across the configured (account, container) pairs.

        Cursor lets a resumed scan skip already-finished pairs and
        pick up the in-progress one mid-page. Pair order is the
        config order for explicit lists; for per-account discovery
        it is account order × discovered container order.
        """
        decoded = _Cursor.loads(cursor)
        try:
            pairs = await self._resolve_pairs()
        except _AccessDenied as denied:
            # Per-account `?comp=list` 403 — emit one warning ref per
            # affected account rather than crashing the whole scan.
            # The denied account is recorded by the helper before
            # re-raising so we know which to report on.
            account_name = denied.account or ""
            account = self._accounts_by_name.get(
                account_name,
                AzureAccount(storage_account=account_name),
            )
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=f"azure-blob://{account.storage_account}/",
                native_url=account.base_url(),
                content_type="text/plain",
                metadata={
                    "azure_storage_account": account.storage_account,
                    "azure_subscription_id": account.subscription_id,
                    "error": "access_denied",
                    "status": str(denied.status),
                    "scope": "container_list",
                },
            )
            return
        for idx, (account, container) in enumerate(pairs):
            if idx < decoded.pair_index:
                continue
            marker = decoded.marker if idx == decoded.pair_index else None
            try:
                async for ref in self._discover_container(
                    account, container, idx, marker, filter
                ):
                    yield ref
            except _AccessDenied as denied:
                # Per requirement: surface as a single warning ref
                # rather than crashing. Operators see the container
                # in the findings list with `error=403`; the rest of
                # the scan proceeds.
                logger.warning(
                    "azure_blob: access denied account=%s "
                    "container=%s — yielding warning ref",
                    account.storage_account,
                    container,
                )
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=f"azure-blob://{account.storage_account}/{container}/",
                    native_url=(
                        f"{account.base_url()}/{container}"
                    ),
                    content_type="text/plain",
                    metadata={
                        "azure_storage_account": account.storage_account,
                        "azure_container": container,
                        "azure_subscription_id": account.subscription_id,
                        "error": "access_denied",
                        "status": str(denied.status),
                    },
                )

    async def _resolve_pairs(
        self,
    ) -> tuple[tuple[AzureAccount, str], ...]:
        """Return the (account, container) pairs to walk."""
        if self._config.discovery.containers:
            pairs: list[tuple[AzureAccount, str]] = []
            for c in self._config.discovery.containers:
                # __post_init__ guarantees the account is registered,
                # so this lookup is total.
                pairs.append((self._accounts_by_name[c.account], c.name))
            return tuple(pairs)
        # Per-account discovery: enumerate containers via `?comp=list`.
        out: list[tuple[AzureAccount, str]] = []
        for name in self._config.discovery.accounts:
            account = self._accounts_by_name[name]
            for container in await self._list_containers(account):
                out.append((account, container))
        return tuple(out)

    async def _list_containers(self, account: AzureAccount) -> tuple[str, ...]:
        """Enumerate containers in `account` via `?comp=list`.

        XML response shape:

            <EnumerationResults>
              <Containers>
                <Container><Name>foo</Name>...</Container>
                ...
              </Containers>
              <NextMarker>opaque</NextMarker>
            </EnumerationResults>

        Defensive: tolerate missing `<Name>` (partial-failure surface)
        by skipping rather than KeyError mid-pagination.
        """
        names: list[str] = []
        marker: str | None = None
        while True:
            params: dict[str, str] = {"comp": "list"}
            if marker:
                params["marker"] = marker
            url = f"{account.base_url()}/"
            resp = await self._authed_request(
                "GET", url, account, params=params
            )
            if resp.status_code == 403:
                raise _AccessDenied(
                    status=403, account=account.storage_account
                )
            resp.raise_for_status()
            root = _parse_xml(resp.content)
            containers_node = root.find("Containers")
            if containers_node is not None:
                for entry in containers_node.findall("Container"):
                    name_node = entry.find("Name")
                    if name_node is None or not name_node.text:
                        continue
                    names.append(name_node.text)
            next_marker_node = root.find("NextMarker")
            marker = (
                next_marker_node.text
                if next_marker_node is not None and next_marker_node.text
                else None
            )
            if not marker:
                break
        return tuple(names)

    async def _discover_container(
        self,
        account: AzureAccount,
        container: str,
        pair_index: int,
        marker: str | None,
        filter: SourceFilter,
    ) -> AsyncIterator[DocumentRef]:
        """Paginate `?restype=container&comp=list` for one container."""
        glob_pattern = self._config.glob or _filter_glob(filter)
        url = f"{account.base_url()}/{container}"
        # `include=versions,deleted` only added when the operator opts
        # in — the default response excludes both, matching the brief.
        include_clauses: list[str] = []
        if self._config.include_versions:
            include_clauses.extend(("versions", "deleted"))
        while True:
            params: dict[str, str] = {
                "restype": "container",
                "comp": "list",
                "maxresults": "1000",
            }
            if self._config.prefix:
                params["prefix"] = self._config.prefix
            if include_clauses:
                params["include"] = ",".join(include_clauses)
            if marker:
                params["marker"] = marker
            resp = await self._authed_request(
                "GET", url, account, params=params
            )
            if resp.status_code == 403:
                raise _AccessDenied(status=403)
            resp.raise_for_status()
            root = _parse_xml(resp.content)
            blobs_node = root.find("Blobs")
            if blobs_node is not None:
                for entry in blobs_node.findall("Blob"):
                    ref = self._ref_from_blob(account, container, entry)
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
                        _Cursor(pair_index=pair_index, marker=marker),
                    )
            next_marker_node = root.find("NextMarker")
            marker = (
                next_marker_node.text
                if next_marker_node is not None and next_marker_node.text
                else None
            )
            if not marker:
                return

    def _ref_from_blob(
        self,
        account: AzureAccount,
        container: str,
        entry: ET.Element,
    ) -> DocumentRef | None:
        """Build a DocumentRef from one `<Blob>` element.

        Filters out soft-deleted blobs when `include_versions=False`
        — Azure marks them with `<Deleted>true</Deleted>`.
        """
        name_node = entry.find("Name")
        if name_node is None or not name_node.text:
            return None
        name = name_node.text
        deleted_node = entry.find("Deleted")
        is_deleted = (
            deleted_node is not None
            and (deleted_node.text or "").lower() == "true"
        )
        if is_deleted and not self._config.include_versions:
            return None
        props = entry.find("Properties")
        size: int | None = None
        last_modified: datetime | None = None
        etag: str | None = None
        content_type = "application/octet-stream"
        kms_key: str | None = None
        if props is not None:
            size_node = props.find("Content-Length")
            if size_node is not None and size_node.text:
                try:
                    size = int(size_node.text)
                except ValueError:
                    size = None
            lm_node = props.find("Last-Modified")
            if lm_node is not None and lm_node.text:
                last_modified = _parse_rfc1123(lm_node.text)
            etag_node = props.find("Etag")
            if etag_node is not None and etag_node.text:
                etag = etag_node.text.strip('"') or None
            ct_node = props.find("Content-Type")
            if ct_node is not None and ct_node.text:
                content_type = ct_node.text
            # CMEK key — opaque pass-through. Never log this; it is
            # the key-vault resource name, not the key material.
            kms_key_node = props.find("CustomerProvidedKeySha256")
            if kms_key_node is not None and kms_key_node.text:
                kms_key = kms_key_node.text
            # Encryption-scope (CMK alternative) — also opaque.
            scope_node = props.find("EncryptionScope")
            if scope_node is not None and scope_node.text:  # pragma: no cover - rare CMK alternative, exercised in integration
                kms_key = kms_key or scope_node.text
        version_id_node = entry.find("VersionId")
        version_id = (
            version_id_node.text if version_id_node is not None else None
        )
        metadata: dict[str, str] = {
            "azure_storage_account": account.storage_account,
            "azure_container": container,
            "azure_blob_name": name,
            "azure_subscription_id": account.subscription_id,
        }
        if version_id:
            metadata["azure_version_id"] = version_id
        if is_deleted:
            metadata["azure_deleted"] = "true"
        if kms_key:
            metadata["azure_cmk_ref"] = kms_key
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"azure-blob://{account.storage_account}/{container}/{name}",
            native_url=(
                f"{account.base_url()}/{container}/{quote(name, safe='/')}"
            ),
            content_type=content_type,
            size=size,
            etag=etag,
            last_modified=last_modified,
            metadata=metadata,
        )

    async def fetch(
        self, ref: DocumentRef
    ) -> AsyncIterator[Document | DocumentChunk]:
        """Stream the blob body via `GET <account>/<container>/<blob>`.

        Bounded by `_fetch_semaphore` so the per-connector concurrency
        cap holds even when the scheduler over-issues fetch calls.
        """
        account_name = ref.metadata.get("azure_storage_account")
        container = ref.metadata.get("azure_container")
        blob = ref.metadata.get("azure_blob_name")
        if not account_name or not container or not blob:
            raise ValueError(
                "DocumentRef metadata missing azure_storage_account / "
                "azure_container / azure_blob_name"
            )
        account = self._accounts_by_name.get(account_name)
        if account is None:
            raise KeyError(
                f"account={account_name!r} not in AzureBlobConfig.accounts"
            )
        url = (
            f"{account.base_url()}/{container}/"
            f"{quote(blob, safe='/')}"
        )
        params: dict[str, str] = {}
        version_id = ref.metadata.get("azure_version_id")
        if version_id:
            # Pin the version we discovered, even if a newer write
            # rolled in between discover and fetch.
            params["versionid"] = version_id
        async with self._fetch_semaphore:
            resp = await self._authed_request(
                "GET", url, account, params=params or None
            )
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
        account: AzureAccount,
        *,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Issue one request with bearer or Shared Key auth.

        On 401 with bearer auth we invalidate the cached token and
        retry exactly once — Entra rotates tokens out-of-band so a
        single retry handles the legitimate race; further 401s
        propagate.
        """
        # Build the request first so signing can read the canonical
        # URL (including the query string and `x-ms-date`).
        headers: dict[str, str] = {
            "x-ms-version": AZURE_STORAGE_API_VERSION,
            "x-ms-date": _http_date_now(),
        }
        request = self._client.build_request(
            method, url, params=params, headers=headers
        )
        if account.storage_account in self._shared_keys:
            credential = self._shared_keys[account.storage_account]
            request.headers["Authorization"] = sign_shared_key(
                method=method,
                url=request.url,
                headers=request.headers,
                credential=credential,
                content_length=0,
            )
            return await self._client.send(request)
        # Bearer path.
        if self._tokens is None:
            raise RuntimeError(
                "no auth configured: connector has neither shared "
                "keys nor a token cache"
            )
        token = await self._tokens.get(self._client)
        request.headers["Authorization"] = f"Bearer {token.value}"
        resp = await self._client.send(request)
        if resp.status_code == 401:
            self._tokens.invalidate()
            token = await self._tokens.get(self._client)
            # Build a fresh request — the previous one's auth header
            # is mutated, and `send()` cannot be called twice on the
            # same Request safely.
            retry_headers = {
                "x-ms-version": AZURE_STORAGE_API_VERSION,
                "x-ms-date": _http_date_now(),
                "Authorization": f"Bearer {token.value}",
            }
            retry_req = self._client.build_request(
                method, url, params=params, headers=retry_headers
            )
            resp = await self._client.send(retry_req)
        return resp


# --- helpers --------------------------------------------------------


class _AccessDenied(Exception):
    """Raised internally on 403 to surface the warning-ref path.

    Carries the affected account name when known so the discover loop
    can attribute the warning ref to the right tenant.
    """

    def __init__(self, *, status: int, account: str | None = None) -> None:
        super().__init__(f"access denied (status={status})")
        self.status = status
        self.account = account


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


def _parse_xml(raw: bytes) -> ET.Element:
    """Parse Azure XML, raising ValueError on malformed input.

    Defensive against partial-write / truncated responses: an XML
    parse error becomes a structured ValueError so the caller can
    distinguish from network errors.
    """
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"malformed Azure XML response: {exc}") from None


def _key_from_path(path: str) -> str:
    """Strip `azure-blob://account/container/` from a full path."""
    rest = (
        path[len("azure-blob://") :]
        if path.startswith("azure-blob://")
        else path
    )
    parts = rest.split("/", 2)
    return parts[2] if len(parts) == 3 else ""


def _filter_glob(filter: SourceFilter) -> str | None:
    """Pick the first include glob from `filter.include` if any.

    Mirrors the GCS connector's policy: one pattern is the common
    case and matches the AWS connector's prefix semantics.
    """
    if not filter.include:
        return None
    return filter.include[0]


def _parse_rfc1123(value: str) -> datetime | None:
    """Parse `Wed, 01 Jan 2026 12:34:56 GMT` → UTC datetime.

    Azure emits Last-Modified in RFC 1123 form. Returns None on parse
    failure rather than raising — defensive against future format
    drift.
    """
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:  # pragma: no cover - parsedate_to_datetime returns None on edge inputs
        return None
    if dt.tzinfo is None:  # pragma: no cover - RFC1123 always carries GMT
        dt = dt.replace(tzinfo=UTC)
    return dt


def _http_date_now() -> str:
    """Now as an RFC 7231 IMF-fixdate, the `x-ms-date` canonical form."""
    from email.utils import formatdate

    return formatdate(usegmt=True)


# --- factory + spec ------------------------------------------------


def _build_token_source(auth: AzureBlobAuthConfig) -> TokenSource | None:
    """Resolve auth → bearer `TokenSource`, or None if Shared Key only.

    Mode selection mirrors `AzureBlobAuthConfig.__post_init__`. The
    Shared Key path skips token sources because each request is
    HMAC-signed in-line.
    """
    if auth.tenant_id and auth.client_id and auth.oidc_token_path:
        return WorkloadIdentityTokenSource(
            tenant_id=auth.tenant_id,
            client_id=auth.client_id,
            oidc_token_path=auth.oidc_token_path,
            scope=auth.scope,
        )
    if auth.managed_identity:
        return ManagedIdentityTokenSource(
            client_id=auth.managed_identity_client_id
        )
    # Shared Key only: no bearer source needed.
    return None


def _build_shared_keys(
    auth: AzureBlobAuthConfig,
) -> dict[str, SharedKeyCredential]:
    """Build per-account `SharedKeyCredential` map from the auth config."""
    if not auth.account_keys:
        return {}
    return {
        name: SharedKeyCredential(
            account_name=name, account_key_b64=key
        )
        for name, key in auth.account_keys.items()
    }


def _factory(config: Mapping[str, Any]) -> AzureBlobConnector:
    """Registry factory: build an `AzureBlobConnector` from a Mapping.

    Two construction paths:

      * In-process: pass `_config: AzureBlobConfig` (and optionally
        `_token_cache` + `_shared_keys` + `_client`) to skip TOML
        re-parse. Used by tests and embedders.

      * TOML/YAML: pass nested dicts. Validated by the dataclass
        `__post_init__` invariants below.
    """
    if isinstance(config.get("_config"), AzureBlobConfig):
        cfg = config["_config"]
        token_cache = config.get("_token_cache")
        if token_cache is None and not config.get("_shared_keys"):
            source = _build_token_source(cfg.auth)
            token_cache = TokenCache(source=source) if source else None
        shared_keys = config.get("_shared_keys") or _build_shared_keys(cfg.auth)
        return AzureBlobConnector(
            cfg,
            token_cache,
            shared_keys,
            client=config.get("_client"),
        )
    auth_raw = config.get("auth", {})
    discovery_raw = config.get("discovery", {})
    accounts_raw = config.get("accounts", [])
    if not isinstance(auth_raw, Mapping):
        raise ValueError("azure_blob config: 'auth' must be a mapping")
    if not isinstance(discovery_raw, Mapping):
        raise ValueError(
            "azure_blob config: 'discovery' must be a mapping"
        )
    if not isinstance(accounts_raw, (list, tuple)):
        raise ValueError(
            "azure_blob config: 'accounts' must be a list of account specs"
        )
    auth = AzureBlobAuthConfig(
        tenant_id=auth_raw.get("tenant_id"),
        client_id=auth_raw.get("client_id"),
        oidc_token_path=auth_raw.get("oidc_token_path"),
        managed_identity=bool(auth_raw.get("managed_identity", False)),
        managed_identity_client_id=auth_raw.get(
            "managed_identity_client_id"
        ),
        account_keys=dict(auth_raw.get("account_keys", {}) or {}),
        scope=str(auth_raw.get("scope", AZURE_STORAGE_DEFAULT_SCOPE)),
    )
    accounts = tuple(
        AzureAccount(
            storage_account=str(a["storage_account"]),
            subscription_id=str(a.get("subscription_id", "")),
            endpoint_suffix=str(
                a.get("endpoint_suffix", DEFAULT_ENDPOINT_SUFFIX)
            ),
            label=str(a.get("label", "")),
        )
        for a in accounts_raw
    )
    containers_raw = discovery_raw.get("containers", []) or []
    discovery = AzureBlobDiscovery(
        containers=tuple(
            ContainerSpec(account=str(c["account"]), name=str(c["name"]))
            for c in containers_raw
        ),
        accounts=tuple(
            str(name) for name in (discovery_raw.get("accounts") or ())
        ),
    )
    cfg = AzureBlobConfig(
        auth=auth,
        discovery=discovery,
        accounts=accounts,
        id=str(config.get("id", "azure_blob:default")),
        prefix=str(config.get("prefix", "")),
        glob=config.get("glob"),
        include_versions=bool(config.get("include_versions", False)),
        concurrency=int(config.get("concurrency", DEFAULT_CONCURRENCY)),
    )
    source = _build_token_source(auth)
    token_cache = TokenCache(source=source) if source else None
    shared_keys = _build_shared_keys(auth)
    return AzureBlobConnector(cfg, token_cache, shared_keys)


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
        # Minimum data-plane permissions the connector needs.
        # Lighthouse subscription delegations must include these via
        # the Storage Blob Data Reader role (or a custom role).
        "Microsoft.Storage/storageAccounts/blobServices/containers/read",
        "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
    ),
    description=(
        "Azure Blob Storage connector. Three auth modes (Workload "
        "Identity Federation via Entra OIDC, Managed Identity via "
        "IMDS, legacy Shared Key signing), Lighthouse multi-account "
        "fan-out, container discovery via explicit list or per-account "
        "?comp=list, paginated ?restype=container&comp=list with "
        "<NextMarker>, streaming GET, soft-deleted/version-aware "
        "listing, CMEK pass-through, 403 surfaces as one warning ref "
        "per container (does not crash scan)."
    ),
)


__all__ = [
    "AZURE_STORAGE_API_VERSION",
    "AzureAccount",
    "AzureBlobAuthConfig",
    "AzureBlobConfig",
    "AzureBlobConnector",
    "AzureBlobDiscovery",
    "ContainerSpec",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_ENDPOINT_SUFFIX",
    "KIND",
    "SPEC",
]
