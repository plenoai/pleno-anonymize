"""Tests for GcsConnector — bucket discovery, pagination, fetch,
versioning, 403 handling, glob/prefix filters, factory, lifecycle.

All HTTP traffic is intercepted by `httpx.MockTransport`. No
network. No real GCS. The token cache is short-circuited with a
canned `AccessToken` so we exercise the GCS API surface without
re-running the full OAuth flow per test.
"""

from __future__ import annotations

import json
from collections.abc import Callable
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
from pleno_pii_scanner_gcs import (
    DEFAULT_CONCURRENCY,
    GcsAuthConfig,
    GcsBucketDiscovery,
    GcsConfig,
    GcsConnector,
    SPEC,
    AccessToken,
    TokenCache,
)
from pleno_pii_scanner_gcs._oauth_token import TokenSource


def _canned_cache() -> TokenCache:
    """A TokenCache that hands out a fixed token without HTTP I/O."""

    class _Static(TokenSource):
        async def acquire(self, _client):
            return AccessToken(
                value="canned-token",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

    return TokenCache(source=_Static())


def _make_handler(
    routes: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler from a `path-suffix → responder` map."""

    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, responder in routes.items():
            if request.url.path.endswith(suffix) or suffix in str(request.url):
                return responder(request)
        return httpx.Response(404, content=b"unmatched: " + str(request.url).encode())

    return handler


def _explicit_config(
    *buckets: str,
    prefix: str = "",
    glob: str | None = None,
    include_deleted: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    id: str = "gcs:test",
) -> GcsConfig:
    return GcsConfig(
        auth=GcsAuthConfig(),
        discovery=GcsBucketDiscovery(buckets=buckets),
        id=id,
        prefix=prefix,
        glob=glob,
        include_deleted=include_deleted,
        concurrency=concurrency,
    )


# --- config validation --------------------------------------------


class TestConfigValidation:
    def test_auth_rejects_lopsided_wif(self) -> None:
        with pytest.raises(ValueError, match="audience and token_path"):
            GcsAuthConfig(audience="aud-only")

    def test_auth_rejects_token_path_without_audience(self) -> None:
        with pytest.raises(ValueError, match="audience and token_path"):
            GcsAuthConfig(token_path="/x")

    def test_auth_rejects_two_modes(self) -> None:
        with pytest.raises(ValueError, match="at most one"):
            GcsAuthConfig(
                credentials_path="/x",
                audience="//iam.googleapis.com/x",
                token_path="/y",
            )

    def test_auth_default_is_adc(self) -> None:
        # No fields → ADC mode. Construction must succeed.
        cfg = GcsAuthConfig()
        assert cfg.credentials_path is None
        assert cfg.audience is None

    def test_discovery_rejects_both_modes(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GcsBucketDiscovery(buckets=("b",), project="p")

    def test_discovery_rejects_neither(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            GcsBucketDiscovery()

    def test_concurrency_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="concurrency"):
            GcsConfig(
                auth=GcsAuthConfig(),
                discovery=GcsBucketDiscovery(buckets=("b",)),
                concurrency=0,
            )


# --- protocol surface ---------------------------------------------


class TestProtocol:
    async def test_runtime_isinstance(self) -> None:
        c = GcsConnector(_explicit_config("b1"), _canned_cache())
        try:
            assert isinstance(c, SourceConnector)
        finally:
            await c.close()

    async def test_capabilities_reflect_concurrency(self) -> None:
        c = GcsConnector(_explicit_config("b1", concurrency=4), _canned_cache())
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
        # We assert the limit by reading the semaphore's `_value`
        # rather than racing real fetches — the ADR §7 covers the
        # parallel-test orthogonally.
        c = GcsConnector(_explicit_config("b1", concurrency=3), _canned_cache())
        try:
            assert c._fetch_semaphore._value == 3
        finally:
            await c.close()


# --- discover: explicit bucket list -------------------------------


class TestDiscoverExplicit:
    async def test_paginated_listing(self) -> None:
        page1 = {
            "items": [
                {
                    "name": "a/one.txt",
                    "size": "10",
                    "updated": "2026-01-01T00:00:00Z",
                    "etag": "e1",
                    "contentType": "text/plain",
                    "generation": "111",
                },
                {
                    "name": "a/two.txt",
                    "size": "20",
                    "updated": "2026-01-02T00:00:00Z",
                    "etag": "e2",
                    "contentType": "text/plain",
                    "kmsKeyName": "projects/p/locations/global/keyRings/r/cryptoKeys/k",
                    "storageClass": "STANDARD",
                },
            ],
            "nextPageToken": "page2",
        }
        page2 = {
            "items": [
                {
                    "name": "a/three.txt",
                    "size": "30",
                    "updated": "2026-01-03T00:00:00Z",
                    "etag": "e3",
                    "contentType": "text/plain",
                }
            ]
        }

        seen_tokens: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer canned-token"
            assert "/b/bucket-a/o" in str(request.url)
            token = request.url.params.get("pageToken")
            seen_tokens.append(token)
            return httpx.Response(200, json=page2 if token == "page2" else page1)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(
                _explicit_config("bucket-a"), _canned_cache(), client=client
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == [
            "gs://bucket-a/a/one.txt",
            "gs://bucket-a/a/two.txt",
            "gs://bucket-a/a/three.txt",
        ]
        # CMEK key flowed through to metadata, opaque pass-through.
        assert (
            refs[1].metadata["gcs_kms_key_name"]
            == "projects/p/locations/global/keyRings/r/cryptoKeys/k"
        )
        # Two pages fetched.
        assert seen_tokens == [None, "page2"]

    async def test_prefix_passed_to_api(self) -> None:
        seen_prefix: dict[str, str | None] = {"v": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_prefix["v"] = request.url.params.get("prefix")
            return httpx.Response(200, json={"items": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(
                _explicit_config("b1", prefix="logs/"),
                _canned_cache(),
                client=client,
            )
            try:
                _ = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen_prefix["v"] == "logs/"

    async def test_glob_filters_client_side(self) -> None:
        body = {
            "items": [
                {"name": "a.log", "size": "1", "updated": "2026-01-01T00:00:00Z"},
                {"name": "b.txt", "size": "1", "updated": "2026-01-01T00:00:00Z"},
                {"name": "c.log", "size": "1", "updated": "2026-01-01T00:00:00Z"},
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(
                _explicit_config("b1", glob="*.log"),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == [
            "gs://b1/a.log",
            "gs://b1/c.log",
        ]

    async def test_filter_include_used_when_no_explicit_glob(self) -> None:
        body = {
            "items": [
                {"name": "x.json", "size": "1", "updated": "2026-01-01T00:00:00Z"},
                {"name": "y.csv", "size": "1", "updated": "2026-01-01T00:00:00Z"},
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [
                    r async for r in c.discover(SourceFilter(include=("*.csv",)), None)
                ]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["gs://b1/y.csv"]

    async def test_since_filter_drops_old_items(self) -> None:
        body = {
            "items": [
                {"name": "old", "size": "1", "updated": "2025-01-01T00:00:00Z"},
                {"name": "new", "size": "1", "updated": "2026-06-01T00:00:00Z"},
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
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
        assert [r.path for r in refs] == ["gs://b1/new"]


# --- versioning ---------------------------------------------------


class TestVersioning:
    async def test_include_deleted_passes_versions_param(self) -> None:
        seen: dict[str, str | None] = {"v": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["v"] = request.url.params.get("versions")
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "name": "a",
                            "size": "1",
                            "updated": "2026-01-01T00:00:00Z",
                            "timeDeleted": "2026-01-02T00:00:00Z",
                        }
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(
                _explicit_config("b1", include_deleted=True),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert seen["v"] == "true"
        # When include_deleted=True, soft-deleted items are emitted.
        assert len(refs) == 1

    async def test_default_skips_soft_deleted(self) -> None:
        body = {
            "items": [
                {
                    "name": "live",
                    "size": "1",
                    "updated": "2026-01-01T00:00:00Z",
                },
                {
                    "name": "dead",
                    "size": "1",
                    "updated": "2026-01-01T00:00:00Z",
                    "timeDeleted": "2026-01-02T00:00:00Z",
                },
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["gs://b1/live"]


# --- 403 access denied --------------------------------------------


class TestAccessDenied:
    async def test_403_yields_warning_ref_and_continues(self) -> None:
        # Two buckets: first 403s, second succeeds. Whole scan still
        # finishes with one warning ref + one real ref.
        def handler(request: httpx.Request) -> httpx.Response:
            if "/b/denied/o" in str(request.url):
                return httpx.Response(403, json={"error": "Forbidden"})
            if "/b/ok/o" in str(request.url):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "name": "x",
                                "size": "1",
                                "updated": "2026-01-01T00:00:00Z",
                            }
                        ]
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(
                _explicit_config("denied", "ok"),
                _canned_cache(),
                client=client,
            )
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].metadata.get("error") == "access_denied"
        assert refs[0].metadata.get("status") == "403"
        assert refs[0].path == "gs://denied/"
        assert refs[1].path == "gs://ok/x"


# --- Cloud Asset Inventory bucket discovery -----------------------


class TestCloudAssetInventory:
    async def test_searchAllResources_paginated(self) -> None:
        cai_pages = [
            {
                "results": [
                    {"name": "//storage.googleapis.com/proj-bucket-1"},
                    {"name": "//storage.googleapis.com/proj-bucket-2"},
                ],
                "nextPageToken": "p2",
            },
            {
                "results": [
                    {"name": "//storage.googleapis.com/proj-bucket-3"},
                ]
            },
        ]
        idx = {"i": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "cloudasset.googleapis.com" in url:
                page = cai_pages[idx["i"]]
                idx["i"] += 1
                return httpx.Response(200, json=page)
            # Per-bucket listing: empty body.
            return httpx.Response(200, json={"items": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = GcsConfig(
                auth=GcsAuthConfig(),
                discovery=GcsBucketDiscovery(project="proj"),
            )
            c = GcsConnector(cfg, _canned_cache(), client=client)
            try:
                buckets = await c._resolve_buckets()
            finally:
                await c.close()
        assert buckets == (
            "proj-bucket-1",
            "proj-bucket-2",
            "proj-bucket-3",
        )

    async def test_cai_skips_malformed_assets(self) -> None:
        body = {
            "results": [
                {"name": "//storage.googleapis.com/good"},
                {"name": "//cloudfunctions.googleapis.com/wrong-type"},
                {"name": 12345},  # non-string
                "not-a-mapping",
                {"no_name_field": True},
                {"name": "//storage.googleapis.com/"},  # empty bucket
            ]
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if "cloudasset.googleapis.com" in str(request.url):
                return httpx.Response(200, json=body)
            return httpx.Response(200, json={"items": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = GcsConfig(
                auth=GcsAuthConfig(),
                discovery=GcsBucketDiscovery(project="p", cai_filter="loc:us"),
            )
            c = GcsConnector(cfg, _canned_cache(), client=client)
            try:
                names = await c._resolve_buckets()
            finally:
                await c.close()
        assert names == ("good",)

    async def test_cai_assets_field_also_accepted(self) -> None:
        body = {
            "assets": [
                {"name": "//storage.googleapis.com/legacy-shape"},
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            cfg = GcsConfig(
                auth=GcsAuthConfig(),
                discovery=GcsBucketDiscovery(project="p"),
            )
            c = GcsConnector(cfg, _canned_cache(), client=client)
            try:
                names = await c._resolve_buckets()
            finally:
                await c.close()
        assert names == ("legacy-shape",)


# --- fetch ---------------------------------------------------------


class TestFetch:
    async def test_fetch_returns_document(self) -> None:
        list_body = {
            "items": [
                {
                    "name": "secrets.txt",
                    "size": "12",
                    "updated": "2026-01-01T00:00:00Z",
                    "etag": "e-secret",
                    "generation": "42",
                }
            ]
        }
        payload = b"hello-world\n"

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/o/secrets.txt" in url and request.url.params.get("alt") == "media":
                # Generation is forwarded so the version is pinned.
                assert request.url.params.get("generation") == "42"
                return httpx.Response(200, content=payload)
            return httpx.Response(200, json=list_body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
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

    async def test_fetch_rejects_ref_without_metadata(self) -> None:
        c = GcsConnector(_explicit_config("b1"), _canned_cache())
        try:
            ref = DocumentRef(source_id=c.id, source_kind=c.kind, path="gs://b1/x")
            with pytest.raises(ValueError, match="gcs_bucket"):
                async for _ in c.fetch(ref):
                    pass
        finally:
            await c.close()

    async def test_fetch_url_quotes_object_name(self) -> None:
        seen_path: dict[str, str] = {"v": ""}

        def handler(request: httpx.Request) -> httpx.Response:
            # Use raw_path to inspect the wire-format URL — httpx
            # decodes `request.url.path` for ergonomics, but the wire
            # carried the percent-encoded form.
            seen_path["v"] = request.url.raw_path.decode()
            return httpx.Response(200, content=b"x")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                ref = DocumentRef(
                    source_id=c.id,
                    source_kind=c.kind,
                    path="gs://b1/has space/and+plus",
                    metadata={
                        "gcs_bucket": "b1",
                        "gcs_name": "has space/and+plus",
                    },
                )
                async for _ in c.fetch(ref):
                    pass
            finally:
                await c.close()
        # Slashes are %2F, spaces are %20, + is %2B.
        assert "has%20space%2Fand%2Bplus" in seen_path["v"]


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
                # First request gets 401.
                return httpx.Response(401, json={})
            # Second sends the new token and succeeds.
            assert request.headers["Authorization"] == "Bearer tok-2"
            return httpx.Response(200, json={"items": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), cache, client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs == []
        assert calls["n"] == 2  # Source.acquire called twice.
        assert request_count["n"] == 2


# --- cursor round-trip --------------------------------------------


class TestCursor:
    async def test_cursor_dumps_and_loads(self) -> None:
        body = {
            "items": [{"name": "a", "size": "1", "updated": "2026-01-01T00:00:00Z"}]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
                # Yielded refs carry the cursor for the scheduler.
                cur = refs[0].metadata["_cursor"]
                # Round-trip via discover with the same cursor.
                refs2 = [r async for r in c.discover(SourceFilter(), cur)]
            finally:
                await c.close()
        assert json.loads(cur)["b"] == 0
        assert refs2  # resumed at same bucket index, walked again

    async def test_cursor_skips_finished_buckets(self) -> None:
        # Two buckets in config; cursor says we are mid-walk on bucket
        # index=1, so bucket index=0 must be skipped entirely.
        seen_buckets: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            for name in ("first-bucket", "second-bucket"):
                if f"/b/{name}/o" in url:
                    seen_buckets.append(name)
                    return httpx.Response(200, json={"items": []})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(
                _explicit_config("first-bucket", "second-bucket"),
                _canned_cache(),
                client=client,
            )
            try:
                cursor = json.dumps({"b": 1, "pt": None})
                _ = [r async for r in c.discover(SourceFilter(), cursor)]
            finally:
                await c.close()
        # Bucket 0 was skipped; only bucket 1 was hit.
        assert seen_buckets == ["second-bucket"]

    async def test_cursor_unparseable_raises(self) -> None:
        c = GcsConnector(_explicit_config("b1"), _canned_cache())
        try:
            with pytest.raises(ValueError, match="unparseable cursor"):
                async for _ in c.discover(SourceFilter(), "not-json"):
                    pass
        finally:
            await c.close()


# --- factory + spec -----------------------------------------------


class TestFactory:
    def test_spec_metadata(self) -> None:
        assert SPEC.kind == "gcs"
        assert SPEC.version == "0.1.0"
        assert "storage.objects.list" in SPEC.required_scopes

    def test_factory_minimal_explicit(self) -> None:
        register(SPEC)
        c = create(
            "gcs",
            {
                "auth": {},
                "discovery": {"buckets": ["b1"]},
            },
        )
        assert isinstance(c, GcsConnector)
        # Default ID applied.
        assert c.id == "gcs:default"

    def test_factory_full(self, service_account_key_file: str) -> None:
        register(SPEC)
        c = create(
            "gcs",
            {
                "id": "scan-1",
                "auth": {"credentials_path": service_account_key_file},
                "discovery": {"project": "proj", "cai_filter": "loc:us"},
                "prefix": "logs/",
                "glob": "*.json",
                "include_deleted": True,
                "concurrency": 2,
            },
        )
        assert c.id == "scan-1"
        assert c._config.glob == "*.json"
        assert c._config.include_deleted is True
        assert c._fetch_semaphore._value == 2

    def test_factory_wif(self, tmp_path) -> None:
        register(SPEC)
        token_file = tmp_path / "oidc"
        token_file.write_text("x")
        c = create(
            "gcs",
            {
                "auth": {
                    "audience": "//iam.googleapis.com/x",
                    "token_path": str(token_file),
                    "service_account_email": "x@p.iam.gserviceaccount.com",
                },
                "discovery": {"buckets": ["b1"]},
            },
        )
        assert isinstance(c, GcsConnector)

    def test_factory_inprocess_path(self) -> None:
        register(SPEC)
        cfg = _explicit_config("b1")
        c = create(
            "gcs",
            {
                "_config": cfg,
                "_token_cache": _canned_cache(),
            },
        )
        assert isinstance(c, GcsConnector)

    def test_factory_inprocess_default_token_cache(self) -> None:
        register(SPEC)
        cfg = _explicit_config("b1")
        # No `_token_cache` provided: the factory builds one from the
        # auth config (here ADC).
        c = create("gcs", {"_config": cfg})
        assert isinstance(c, GcsConnector)

    def test_factory_rejects_non_mapping_auth(self) -> None:
        with pytest.raises(ValueError, match="auth.*mapping"):
            SPEC.factory({"auth": "wrong", "discovery": {"buckets": ["b"]}})

    def test_factory_rejects_non_mapping_discovery(self) -> None:
        with pytest.raises(ValueError, match="discovery.*mapping"):
            SPEC.factory({"auth": {}, "discovery": "wrong"})


# --- lifecycle ----------------------------------------------------


class TestLifecycle:
    async def test_close_owned_client(self) -> None:
        c = GcsConnector(_explicit_config("b1"), _canned_cache())
        assert c._owns_client
        await c.close()

    async def test_close_does_not_close_external_client(self) -> None:
        client = httpx.AsyncClient()
        c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
        await c.close()
        assert not client.is_closed
        await client.aclose()

    async def test_close_idempotent(self) -> None:
        c = GcsConnector(_explicit_config("b1"), _canned_cache())
        await c.close()
        # Calling close again must be a no-op — httpx tolerates the
        # double-aclose. The connector itself does not track a
        # "closed" flag because the owned client already does.
        await c.close()


# --- ref parsing edge cases ---------------------------------------


class TestRefParsing:
    async def test_ref_drops_item_with_non_string_name(self) -> None:
        body = {
            "items": [
                {"name": None, "size": "1"},
                {"name": "good", "size": "1", "updated": "2026-01-01T00:00:00Z"},
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["gs://b1/good"]

    async def test_ref_drops_non_mapping_items(self) -> None:
        body = {
            "items": [
                "not-a-mapping",
                {"name": "good", "size": "1", "updated": "2026-01-01T00:00:00Z"},
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert [r.path for r in refs] == ["gs://b1/good"]

    async def test_ref_handles_invalid_size_string(self) -> None:
        body = {
            "items": [
                {
                    "name": "x",
                    "size": "not-a-number",
                    "updated": "2026-01-01T00:00:00Z",
                }
            ]
        }

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].size is None

    async def test_ref_handles_naive_iso_updated(self) -> None:
        # Defensive: GCS always emits Z-suffixed timestamps, but if a
        # mock/test/fork ever returns naive ISO, we still produce a UTC
        # datetime rather than raising.
        body = {"items": [{"name": "x", "size": "1", "updated": "2026-01-01T00:00:00"}]}

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].last_modified is not None
        assert refs[0].last_modified.tzinfo == UTC

    async def test_ref_handles_non_string_updated(self) -> None:
        # Defensive: non-string `updated` (None, number) → silently
        # treated as missing rather than raising.
        body = {"items": [{"name": "x", "size": "1", "updated": 12345}]}

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].last_modified is None

    async def test_ref_handles_invalid_updated(self) -> None:
        body = {"items": [{"name": "x", "size": "1", "updated": "not-a-date"}]}

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            c = GcsConnector(_explicit_config("b1"), _canned_cache(), client=client)
            try:
                refs = [r async for r in c.discover(SourceFilter(), None)]
            finally:
                await c.close()
        assert refs[0].last_modified is None
