"""Tests for AzureBlobConnector — multi-account, container discovery
(both modes), pagination, fetch, soft-deleted, 403 → warning ref,
401 retry, factory variations, lifecycle, ref parsing edge cases.

All HTTP traffic is intercepted by `httpx.MockTransport`. No
network. No real Azure. The token cache is short-circuited with a
canned `AccessToken` so we exercise the Azure REST surface without
re-running the full Entra/IMDS flow per test.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    Document,
    DocumentRef,
    SourceConnector,
    SourceFilter,
    create,
    register,
)
from pleno_pii_scanner_azure_blob import (
    AZURE_STORAGE_API_VERSION,
    DEFAULT_CONCURRENCY,
    SPEC,
    AccessToken,
    AzureAccount,
    AzureBlobAuthConfig,
    AzureBlobConfig,
    AzureBlobConnector,
    AzureBlobDiscovery,
    ContainerSpec,
    SharedKeyCredential,
    TokenCache,
)
from pleno_pii_scanner_azure_blob._auth import TokenSource


# --- helpers ------------------------------------------------------


def _canned_cache() -> TokenCache:
    """A TokenCache that hands out a fixed token without HTTP I/O."""

    class _Static(TokenSource):
        async def acquire(self, _client):
            return AccessToken(
                value="canned-token",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

    return TokenCache(source=_Static())


def _account(name: str = "acct1", subscription: str = "sub-1") -> AzureAccount:
    return AzureAccount(storage_account=name, subscription_id=subscription)


def _explicit_config(
    *containers: tuple[str, str],
    accounts: tuple[AzureAccount, ...] | None = None,
    prefix: str = "",
    glob: str | None = None,
    include_versions: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    id: str = "azure_blob:test",
) -> AzureBlobConfig:
    """Build a config with explicit container list. Defaults to MI auth."""
    if accounts is None:
        # Derive accounts from the container list.
        names = sorted({a for a, _ in containers})
        accounts = tuple(_account(n) for n in names) or (_account(),)
    return AzureBlobConfig(
        auth=AzureBlobAuthConfig(managed_identity=True),
        discovery=AzureBlobDiscovery(
            containers=tuple(ContainerSpec(account=a, name=c) for a, c in containers)
        ),
        accounts=accounts,
        id=id,
        prefix=prefix,
        glob=glob,
        include_versions=include_versions,
        concurrency=concurrency,
    )


def _list_xml(
    *blobs: dict[str, str | None],
    next_marker: str | None = None,
) -> bytes:
    """Build a BlobList XML payload for the mock transport.

    Each blob dict accepts: name, size, last_modified, etag,
    content_type, deleted, version_id, cmk_sha256.
    """
    parts: list[str] = ['<?xml version="1.0" encoding="utf-8"?>']
    parts.append("<EnumerationResults>")
    parts.append("<Blobs>")
    for b in blobs:
        parts.append("<Blob>")
        if b.get("name") is not None:
            parts.append(f"<Name>{b['name']}</Name>")
        version_id = b.get("version_id")
        if version_id:
            parts.append(f"<VersionId>{version_id}</VersionId>")
        if b.get("deleted"):
            parts.append("<Deleted>true</Deleted>")
        # Properties block is optional.
        if any(
            b.get(k)
            for k in (
                "size",
                "last_modified",
                "etag",
                "content_type",
                "cmk_sha256",
            )
        ):
            parts.append("<Properties>")
            if b.get("last_modified"):
                parts.append(f"<Last-Modified>{b['last_modified']}</Last-Modified>")
            if b.get("etag"):
                parts.append(f'<Etag>"{b["etag"]}"</Etag>')
            if b.get("size") is not None:
                parts.append(f"<Content-Length>{b['size']}</Content-Length>")
            if b.get("content_type"):
                parts.append(f"<Content-Type>{b['content_type']}</Content-Type>")
            if b.get("cmk_sha256"):
                parts.append(
                    f"<CustomerProvidedKeySha256>{b['cmk_sha256']}"
                    "</CustomerProvidedKeySha256>"
                )
            parts.append("</Properties>")
        parts.append("</Blob>")
    parts.append("</Blobs>")
    if next_marker:
        parts.append(f"<NextMarker>{next_marker}</NextMarker>")
    else:
        parts.append("<NextMarker />")
    parts.append("</EnumerationResults>")
    return "".join(parts).encode("utf-8")


def _container_list_xml(*names: str, next_marker: str | None = None) -> bytes:
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<EnumerationResults>",
        "<Containers>",
    ]
    for name in names:
        parts.append(f"<Container><Name>{name}</Name></Container>")
    parts.append("</Containers>")
    if next_marker:
        parts.append(f"<NextMarker>{next_marker}</NextMarker>")
    else:
        parts.append("<NextMarker />")
    parts.append("</EnumerationResults>")
    return "".join(parts).encode("utf-8")


# --- config validation -------------------------------------------


class TestConfigValidation:
    def test_auth_rejects_lopsided_wif(self) -> None:
        with pytest.raises(ValueError, match="Workload Identity"):
            AzureBlobAuthConfig(tenant_id="t")

    def test_auth_rejects_partial_wif_pair(self) -> None:
        with pytest.raises(ValueError, match="Workload Identity"):
            AzureBlobAuthConfig(tenant_id="t", client_id="c")

    def test_auth_rejects_no_mode(self) -> None:
        with pytest.raises(ValueError, match="must select one"):
            AzureBlobAuthConfig()

    def test_auth_rejects_two_modes(self) -> None:
        with pytest.raises(ValueError, match="EXACTLY one"):
            AzureBlobAuthConfig(
                tenant_id="t",
                client_id="c",
                oidc_token_path="/x",
                managed_identity=True,
            )

    def test_auth_wif_complete(self) -> None:
        AzureBlobAuthConfig(
            tenant_id="t",
            client_id="c",
            oidc_token_path="/x",
        )

    def test_auth_managed_identity(self) -> None:
        AzureBlobAuthConfig(managed_identity=True)

    def test_auth_account_keys(self) -> None:
        key = base64.b64encode(b"k" * 32).decode("ascii")
        AzureBlobAuthConfig(account_keys={"acct": key})

    def test_discovery_rejects_both(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            AzureBlobDiscovery(
                containers=(ContainerSpec(account="a", name="c"),),
                accounts=("a",),
            )

    def test_discovery_rejects_neither(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            AzureBlobDiscovery()

    def test_concurrency_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="concurrency"):
            AzureBlobConfig(
                auth=AzureBlobAuthConfig(managed_identity=True),
                discovery=AzureBlobDiscovery(accounts=("acct",)),
                accounts=(_account(),),
                concurrency=0,
            )

    def test_config_requires_at_least_one_account(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            AzureBlobConfig(
                auth=AzureBlobAuthConfig(managed_identity=True),
                discovery=AzureBlobDiscovery(accounts=("acct",)),
                accounts=(),
            )

    def test_container_account_must_be_registered(self) -> None:
        with pytest.raises(ValueError, match="not.*in.*accounts"):
            AzureBlobConfig(
                auth=AzureBlobAuthConfig(managed_identity=True),
                discovery=AzureBlobDiscovery(
                    containers=(ContainerSpec(account="missing", name="c"),)
                ),
                accounts=(_account("present"),),
            )

    def test_discovery_account_must_be_registered(self) -> None:
        with pytest.raises(ValueError, match="not in.*accounts"):
            AzureBlobConfig(
                auth=AzureBlobAuthConfig(managed_identity=True),
                discovery=AzureBlobDiscovery(accounts=("missing",)),
                accounts=(_account("present"),),
            )


# --- protocol surface ---------------------------------------------


class TestProtocol:
    async def test_runtime_isinstance(self) -> None:
        c = AzureBlobConnector(_explicit_config(("acct1", "c1")), _canned_cache())
        try:
            assert isinstance(c, SourceConnector)
        finally:
            await c.close()

    async def test_capabilities_reflect_concurrency(self) -> None:
        c = AzureBlobConnector(
            _explicit_config(("acct1", "c1"), concurrency=4),
            _canned_cache(),
        )
        try:
            caps = c.capabilities()
            assert caps == Capabilities(
                incremental=True,
                binary=True,
                content_hash_delta=True,
                max_concurrent_fetches=4,
                streaming=True,
            )
        finally:
            await c.close()

    async def test_concurrency_limit_is_semaphore_value(self) -> None:
        c = AzureBlobConnector(
            _explicit_config(("acct1", "c1"), concurrency=3),
            _canned_cache(),
        )
        try:
            assert c._fetch_semaphore._value == 3
        finally:
            await c.close()


# --- discover: explicit containers --------------------------------


class TestDiscoverExplicit:
    async def test_paginated_listing(self) -> None:
        page1 = _list_xml(
            {
                "name": "logs/one.txt",
                "size": "10",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                "etag": "e1",
                "content_type": "text/plain",
            },
            {
                "name": "logs/two.txt",
                "size": "20",
                "last_modified": "Thu, 02 Jan 2026 00:00:00 GMT",
                "etag": "e2",
                "content_type": "text/plain",
                "cmk_sha256": "ABCDEF==",
            },
            next_marker="page2",
        )
        page2 = _list_xml(
            {
                "name": "logs/three.txt",
                "size": "30",
                "last_modified": "Fri, 03 Jan 2026 00:00:00 GMT",
                "etag": "e3",
                "content_type": "text/plain",
            }
        )
        seen_markers: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer canned-token"
            assert request.headers["x-ms-version"] == AZURE_STORAGE_API_VERSION
            assert "x-ms-date" in request.headers
            assert "/c1" in str(request.url)
            assert request.url.params.get("restype") == "container"
            assert request.url.params.get("comp") == "list"
            marker = request.url.params.get("marker")
            seen_markers.append(marker)
            return httpx.Response(200, content=page2 if marker == "page2" else page1)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == [
            "azure-blob://acct1/c1/logs/one.txt",
            "azure-blob://acct1/c1/logs/two.txt",
            "azure-blob://acct1/c1/logs/three.txt",
        ]
        # CMK SHA256 flows through opaquely.
        assert refs[1].metadata["azure_cmk_ref"] == "ABCDEF=="
        # Subscription id flows through too.
        assert refs[0].metadata["azure_subscription_id"] == "sub-1"
        # Two pages fetched.
        assert seen_markers == [None, "page2"]

    async def test_prefix_passed_to_api(self) -> None:
        seen_prefix: dict[str, str | None] = {"v": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_prefix["v"] = request.url.params.get("prefix")
            return httpx.Response(200, content=_list_xml())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1"), prefix="logs/"),
                _canned_cache(),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen_prefix["v"] == "logs/"

    async def test_glob_filters_client_side(self) -> None:
        body = _list_xml(
            {
                "name": "a.log",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
            {
                "name": "b.txt",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
            {
                "name": "c.log",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1"), glob="*.log"),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == [
            "azure-blob://acct1/c1/a.log",
            "azure-blob://acct1/c1/c.log",
        ]

    async def test_filter_include_used_when_no_explicit_glob(self) -> None:
        body = _list_xml(
            {
                "name": "x.json",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
            {
                "name": "y.csv",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [
                    r async for r in c.discover(SourceFilter(include=("*.csv",)), None)
                ]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["azure-blob://acct1/c1/y.csv"]

    async def test_since_filter_drops_old_items(self) -> None:
        body = _list_xml(
            {
                "name": "old",
                "size": "1",
                "last_modified": "Sun, 01 Jan 2023 00:00:00 GMT",
            },
            {
                "name": "new",
                "size": "1",
                "last_modified": "Tue, 01 Jun 2027 00:00:00 GMT",
            },
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [
                    r
                    async for r in c.discover(
                        SourceFilter(since=datetime(2026, 1, 1, tzinfo=UTC)),
                        None,
                    )
                ]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["azure-blob://acct1/c1/new"]


# --- soft-deleted / versions --------------------------------------


class TestVersioning:
    async def test_default_skips_soft_deleted(self) -> None:
        body = _list_xml(
            {
                "name": "live",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            },
            {
                "name": "dead",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                "deleted": "yes",
            },
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["azure-blob://acct1/c1/live"]

    async def test_include_versions_emits_deleted_with_include_param(self) -> None:
        body = _list_xml(
            {
                "name": "x",
                "size": "1",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                "deleted": "yes",
                "version_id": "v-001",
            }
        )
        seen_include: dict[str, str | None] = {"v": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_include["v"] = request.url.params.get("include")
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1"), include_versions=True),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen_include["v"] == "versions,deleted"
        assert len(refs) == 1
        assert refs[0].metadata["azure_version_id"] == "v-001"
        assert refs[0].metadata["azure_deleted"] == "true"


# --- 403 access denied --------------------------------------------


class TestAccessDenied:
    async def test_403_yields_warning_ref_and_continues(self) -> None:
        # Two containers in the same account: first 403s, second
        # succeeds. Whole scan still finishes with one warning ref +
        # one real ref.
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/denied" in url:
                return httpx.Response(403, text="Forbidden")
            if "/ok" in url:
                return httpx.Response(
                    200,
                    content=_list_xml(
                        {
                            "name": "x",
                            "size": "1",
                            "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                        }
                    ),
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "denied"), ("acct1", "ok")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].metadata.get("error") == "access_denied"
        assert refs[0].metadata.get("status") == "403"
        assert refs[0].path == "azure-blob://acct1/denied/"
        assert refs[1].path == "azure-blob://acct1/ok/x"


# --- multi-account + per-account container discovery -------------


class TestPerAccountDiscovery:
    async def test_lists_containers_per_account_then_lists_blobs(self) -> None:
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            seen_paths.append(url)
            if (
                "acct1." in url
                and request.url.params.get("comp") == "list"
                and request.url.params.get("restype") is None
            ):
                # Container enumeration for acct1.
                return httpx.Response(200, content=_container_list_xml("c1", "c2"))
            if (
                "acct2." in url
                and request.url.params.get("comp") == "list"
                and request.url.params.get("restype") is None
            ):
                return httpx.Response(200, content=_container_list_xml("c3"))
            # Blob listing inside any container.
            return httpx.Response(
                200,
                content=_list_xml(
                    {
                        "name": "f",
                        "size": "1",
                        "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                    }
                ),
            )

        cfg = AzureBlobConfig(
            auth=AzureBlobAuthConfig(managed_identity=True),
            discovery=AzureBlobDiscovery(accounts=("acct1", "acct2")),
            accounts=(_account("acct1"), _account("acct2", "sub-2")),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(cfg, _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == [
            "azure-blob://acct1/c1/f",
            "azure-blob://acct1/c2/f",
            "azure-blob://acct2/c3/f",
        ]
        assert refs[2].metadata["azure_subscription_id"] == "sub-2"

    async def test_container_enumeration_paginates_via_next_marker(self) -> None:
        pages = [
            _container_list_xml("c1", next_marker="m2"),
            _container_list_xml("c2"),
        ]
        idx = {"i": 0}
        seen_markers: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("restype") is None:
                marker = request.url.params.get("marker")
                seen_markers.append(marker)
                page = pages[idx["i"]]
                idx["i"] += 1
                return httpx.Response(200, content=page)
            return httpx.Response(200, content=_list_xml())

        cfg = AzureBlobConfig(
            auth=AzureBlobAuthConfig(managed_identity=True),
            discovery=AzureBlobDiscovery(accounts=("acct1",)),
            accounts=(_account("acct1"),),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(cfg, _canned_cache(), client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen_markers == [None, "m2"]

    async def test_container_enumeration_skips_missing_name(self) -> None:
        # Defensive: tolerate `<Container/>` without `<Name>` rather
        # than crashing the entire scan.
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b"<EnumerationResults><Containers>"
            b"<Container><Name>good</Name></Container>"
            b"<Container></Container>"
            b"<Container><Name></Name></Container>"
            b"</Containers><NextMarker /></EnumerationResults>"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("restype") is None:
                return httpx.Response(200, content=body)
            return httpx.Response(200, content=_list_xml())

        cfg = AzureBlobConfig(
            auth=AzureBlobAuthConfig(managed_identity=True),
            discovery=AzureBlobDiscovery(accounts=("acct1",)),
            accounts=(_account("acct1"),),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(cfg, _canned_cache(), client=client)
            try:
                names = await c._list_containers(c._accounts_by_name["acct1"])
            finally:
                await c.close()
        assert names == ("good",)

    async def test_container_enumeration_403_yields_warning_ref(self) -> None:
        # Per-account `?comp=list` 403 surfaces a single warning ref
        # (scope=container_list) so the rest of the scan proceeds —
        # symmetric with the per-container 403 behavior.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.params.get("restype") is None:
                return httpx.Response(403, text="Forbidden")
            return httpx.Response(200, content=_list_xml())

        cfg = AzureBlobConfig(
            auth=AzureBlobAuthConfig(managed_identity=True),
            discovery=AzureBlobDiscovery(accounts=("acct1",)),
            accounts=(_account("acct1"),),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(cfg, _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert len(refs) == 1
        assert refs[0].metadata["error"] == "access_denied"
        assert refs[0].metadata["scope"] == "container_list"
        assert refs[0].metadata["azure_storage_account"] == "acct1"


# --- fetch ---------------------------------------------------------


class TestFetch:
    async def test_fetch_returns_document(self) -> None:
        list_body = _list_xml(
            {
                "name": "secrets.txt",
                "size": "12",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
                "etag": "e-secret",
            }
        )
        payload = b"hello-world\n"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "secrets.txt" in url and request.url.params.get("restype") is None:
                return httpx.Response(200, content=payload)
            return httpx.Response(200, content=list_body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                docs = []
                async for d in c.fetch(refs[0]):
                    docs.append(d)
            finally:
                await c.close()
        assert len(docs) == 1
        assert isinstance(docs[0], Document)
        assert docs[0].binary == payload
        assert docs[0].content_hash == "e-secret"

    async def test_fetch_pins_version_id(self) -> None:
        seen_version: dict[str, str | None] = {"v": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_version["v"] = request.url.params.get("versionid")
            return httpx.Response(200, content=b"x")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="azure-blob://acct1/c1/x",
                    metadata={
                        "azure_storage_account": "acct1",
                        "azure_container": "c1",
                        "azure_blob_name": "x",
                        "azure_version_id": "v-9",
                    },
                )
                async for _ in c.fetch(ref):
                    pass
            finally:
                await c.close()
        assert seen_version["v"] == "v-9"

    async def test_fetch_rejects_ref_without_metadata(self) -> None:
        c = AzureBlobConnector(_explicit_config(("acct1", "c1")), _canned_cache())
        try:
            ref = DocumentRef(
                source_id=c.id, source_kind=c.kind, path="azure-blob://acct1/c1/x"
            )
            with pytest.raises(ValueError, match="azure_storage_account"):
                async for _ in c.fetch(ref):
                    pass
        finally:
            await c.close()

    async def test_fetch_rejects_ref_with_unknown_account(self) -> None:
        c = AzureBlobConnector(_explicit_config(("acct1", "c1")), _canned_cache())
        try:
            ref = DocumentRef(
                source_id=c.id,
                source_kind=c.kind,
                path="azure-blob://acct9/c/x",
                metadata={
                    "azure_storage_account": "acct9",
                    "azure_container": "c",
                    "azure_blob_name": "x",
                },
            )
            with pytest.raises(KeyError, match="acct9"):
                async for _ in c.fetch(ref):
                    pass
        finally:
            await c.close()

    async def test_concurrency_semaphore_bounds_parallel_fetches(self) -> None:
        # The semaphore must serialize concurrent fetches above its
        # capacity. We instrument the handler with an `asyncio.Event`
        # to confirm the cap holds even when callers gather().
        # `httpx.MockTransport` accepts async handlers transparently.
        in_flight = {"n": 0, "max": 0}
        gate = asyncio.Event()

        async def handler_async(request: httpx.Request) -> httpx.Response:
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
            await gate.wait()
            in_flight["n"] -= 1
            return httpx.Response(200, content=b"x")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler_async)
        ) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1"), concurrency=2),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [
                    DocumentRef(
                        source_id=c.id,
                        source_kind=c.kind,
                        path=f"azure-blob://acct1/c1/{i}",
                        metadata={
                            "azure_storage_account": "acct1",
                            "azure_container": "c1",
                            "azure_blob_name": f"{i}",
                        },
                    )
                    for i in range(5)
                ]

                async def consume(r):
                    async for _ in c.fetch(r):
                        pass

                tasks = [asyncio.create_task(consume(r)) for r in refs]
                # Let the first batch reach the handler, then release.
                await asyncio.sleep(0.05)
                gate.set()
                await asyncio.gather(*tasks)
            finally:
                await c.close()
        # Concurrency bound = 2, so peak in-flight requests must
        # never exceed 2 even though we issued 5 in parallel.
        assert in_flight["max"] <= 2


# --- 401 retry path -----------------------------------------------


class TestAuthRetry:
    async def test_401_invalidates_cache_and_retries_once(self) -> None:
        calls = {"n": 0}

        class _RotatingSource(TokenSource):
            async def acquire(self, _client):
                calls["n"] += 1
                return AccessToken(
                    value=f"tok-{calls['n']}",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

        cache = TokenCache(source=_RotatingSource())
        request_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            request_count["n"] += 1
            if request_count["n"] == 1:
                return httpx.Response(401, content=b"<error/>")
            assert request.headers["Authorization"] == "Bearer tok-2"
            return httpx.Response(200, content=_list_xml())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")), cache, client=client
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs == []
        assert calls["n"] == 2  # Source.acquire called twice.
        assert request_count["n"] == 2


# --- Shared Key auth path -----------------------------------------


class TestSharedKeyAuth:
    async def test_request_signed_with_shared_key(self) -> None:
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        captured_auth: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_auth["v"] = request.headers.get("Authorization", "")
            return httpx.Response(200, content=_list_xml())

        cfg = AzureBlobConfig(
            auth=AzureBlobAuthConfig(account_keys={"acct1": key_b64}),
            discovery=AzureBlobDiscovery(
                containers=(ContainerSpec(account="acct1", name="c1"),)
            ),
            accounts=(_account("acct1"),),
        )
        shared_keys = {
            "acct1": SharedKeyCredential(account_name="acct1", account_key_b64=key_b64)
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(cfg, None, shared_keys=shared_keys, client=client)
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert captured_auth["v"].startswith("SharedKey acct1:")

    async def test_no_auth_configured_raises(self) -> None:
        # Defensive: a connector built without bearer cache AND
        # without shared keys is misconfigured. The first request
        # surfaces the misconfig rather than 401-looping silently.
        cfg = _explicit_config(("acct1", "c1"))
        c = AzureBlobConnector(cfg, None, shared_keys=None)
        try:
            with pytest.raises(RuntimeError, match="no auth"):
                _ = [r async for r in c.discover(SourceFilter(), None)]
        finally:
            await c.close()


# --- cursor round-trip --------------------------------------------


class TestCursor:
    async def test_cursor_dumps_and_loads(self) -> None:
        body = _list_xml(
            {"name": "a", "size": "1", "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT"}
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                cur = refs[0].metadata["_cursor"]
                refs2 = [r async for r in c.discover(SourceFilter(), cur)]
            finally:
                await c.close()
        assert json.loads(cur)["i"] == 0
        assert refs2

    async def test_cursor_skips_finished_pairs(self) -> None:
        seen_containers: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            for name in ("first", "second"):
                if f"/{name}" in url:
                    seen_containers.append(name)
                    return httpx.Response(200, content=_list_xml())
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "first"), ("acct1", "second")),
                _canned_cache(),
                client=client,
            )
            try:
                cursor = json.dumps({"i": 1, "m": None})
                _ = [r async for r in c.discover(SourceFilter(), cursor)]
            finally:
                await c.close()
        assert seen_containers == ["second"]

    async def test_cursor_unparseable_raises(self) -> None:
        c = AzureBlobConnector(_explicit_config(("acct1", "c1")), _canned_cache())
        try:
            with pytest.raises(ValueError, match="unparseable cursor"):
                async for _ in c.discover(SourceFilter(), "not-json"):
                    pass
        finally:
            await c.close()


# --- factory + spec -----------------------------------------------


class TestFactory:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "azure_blob"
        assert SPEC.version == "0.1.0"
        assert any("blobs/read" in s for s in SPEC.required_scopes)

    def test_factory_minimal_managed_identity(self) -> None:
        register(SPEC)
        c = create(
            "azure_blob",
            {
                "auth": {"managed_identity": True},
                "discovery": {"accounts": ["acct1"]},
                "accounts": [{"storage_account": "acct1"}],
            },
        )
        assert isinstance(c, AzureBlobConnector)
        assert c.id == "azure_blob:default"

    def test_factory_workload_identity(self, tmp_path) -> None:
        register(SPEC)
        token_file = tmp_path / "oidc"
        token_file.write_text("x")
        c = create(
            "azure_blob",
            {
                "id": "scan-1",
                "auth": {
                    "tenant_id": "t",
                    "client_id": "c",
                    "oidc_token_path": str(token_file),
                },
                "discovery": {"containers": [{"account": "acct1", "name": "c1"}]},
                "accounts": [{"storage_account": "acct1", "subscription_id": "sub-1"}],
                "prefix": "logs/",
                "glob": "*.json",
                "include_versions": True,
                "concurrency": 2,
            },
        )
        assert c.id == "scan-1"
        assert c._config.glob == "*.json"
        assert c._config.include_versions is True
        assert c._fetch_semaphore._value == 2

    def test_factory_account_keys(self) -> None:
        register(SPEC)
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        c = create(
            "azure_blob",
            {
                "auth": {"account_keys": {"acct1": key_b64}},
                "discovery": {"containers": [{"account": "acct1", "name": "c1"}]},
                "accounts": [{"storage_account": "acct1"}],
            },
        )
        assert isinstance(c, AzureBlobConnector)
        assert "acct1" in c._shared_keys

    def test_factory_lighthouse_multi_account(self) -> None:
        register(SPEC)
        c = create(
            "azure_blob",
            {
                "auth": {"managed_identity": True},
                "discovery": {"accounts": ["acct1", "acct2"]},
                "accounts": [
                    {
                        "storage_account": "acct1",
                        "subscription_id": "sub-1",
                    },
                    {
                        "storage_account": "acct2",
                        "subscription_id": "sub-2",
                    },
                ],
            },
        )
        assert isinstance(c, AzureBlobConnector)
        assert {a.subscription_id for a in c._config.accounts} == {
            "sub-1",
            "sub-2",
        }

    def test_factory_inprocess_path(self) -> None:
        register(SPEC)
        cfg = _explicit_config(("acct1", "c1"))
        c = create(
            "azure_blob",
            {"_config": cfg, "_token_cache": _canned_cache()},
        )
        assert isinstance(c, AzureBlobConnector)

    def test_factory_inprocess_default_token_cache(self) -> None:
        register(SPEC)
        cfg = _explicit_config(("acct1", "c1"))
        c = create("azure_blob", {"_config": cfg})
        assert isinstance(c, AzureBlobConnector)

    def test_factory_inprocess_default_for_shared_key(self) -> None:
        register(SPEC)
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        cfg = AzureBlobConfig(
            auth=AzureBlobAuthConfig(account_keys={"acct1": key_b64}),
            discovery=AzureBlobDiscovery(
                containers=(ContainerSpec(account="acct1", name="c1"),)
            ),
            accounts=(_account("acct1"),),
        )
        c = create("azure_blob", {"_config": cfg})
        assert "acct1" in c._shared_keys

    def test_factory_rejects_non_mapping_auth(self) -> None:
        with pytest.raises(ValueError, match="auth.*mapping"):
            SPEC.factory(
                {
                    "auth": "wrong",
                    "discovery": {"accounts": ["a"]},
                    "accounts": [{"storage_account": "a"}],
                }
            )

    def test_factory_rejects_non_mapping_discovery(self) -> None:
        with pytest.raises(ValueError, match="discovery.*mapping"):
            SPEC.factory(
                {
                    "auth": {"managed_identity": True},
                    "discovery": "wrong",
                    "accounts": [{"storage_account": "a"}],
                }
            )

    def test_factory_rejects_non_list_accounts(self) -> None:
        with pytest.raises(ValueError, match="accounts.*list"):
            SPEC.factory(
                {
                    "auth": {"managed_identity": True},
                    "discovery": {"accounts": ["a"]},
                    "accounts": "wrong",
                }
            )


# --- lifecycle ----------------------------------------------------


class TestLifecycle:
    async def test_close_owned_client(self) -> None:
        c = AzureBlobConnector(_explicit_config(("acct1", "c1")), _canned_cache())
        assert c._owns_client
        await c.close()

    async def test_close_does_not_close_external_client(self) -> None:
        client = httpx.AsyncClient()
        c = AzureBlobConnector(
            _explicit_config(("acct1", "c1")),
            _canned_cache(),
            client=client,
        )
        await c.close()
        assert not client.is_closed
        await client.aclose()

    async def test_close_idempotent(self) -> None:
        c = AzureBlobConnector(_explicit_config(("acct1", "c1")), _canned_cache())
        await c.close()
        await c.close()


# --- ref / XML parsing edge cases ---------------------------------


class TestRefParsing:
    async def test_ref_drops_blob_with_missing_name(self) -> None:
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b"<EnumerationResults><Blobs>"
            b"<Blob><Properties><Content-Length>1</Content-Length></Properties></Blob>"
            b"<Blob><Name>good</Name>"
            b"<Properties><Content-Length>1</Content-Length>"
            b"<Last-Modified>Wed, 01 Jan 2026 00:00:00 GMT</Last-Modified>"
            b"</Properties></Blob>"
            b"</Blobs><NextMarker /></EnumerationResults>"
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["azure-blob://acct1/c1/good"]

    async def test_ref_handles_invalid_size(self) -> None:
        body = _list_xml(
            {
                "name": "x",
                "size": "not-a-number",
                "last_modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            }
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].size is None

    async def test_ref_handles_invalid_last_modified(self) -> None:
        body = _list_xml(
            {
                "name": "x",
                "size": "1",
                "last_modified": "not-a-date",
            }
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        # parsedate_to_datetime returns None on garbage; we tolerate it.
        assert refs[0].last_modified is None

    async def test_ref_handles_missing_properties(self) -> None:
        body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b"<EnumerationResults><Blobs>"
            b"<Blob><Name>bare</Name></Blob>"
            b"</Blobs><NextMarker /></EnumerationResults>"
        )

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["azure-blob://acct1/c1/bare"]
        assert refs[0].size is None
        assert refs[0].etag is None

    async def test_malformed_xml_raises_value_error(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<not-xml")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = AzureBlobConnector(
                _explicit_config(("acct1", "c1")),
                _canned_cache(),
                client=client,
            )
            try:
                with pytest.raises(ValueError, match="malformed Azure XML"):
                    _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
