"""Tests for the httpx api wrapper — auth, pagination, 429 backoff."""

from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner_bitbucket.api import (
    DEFAULT_CLOUD_BASE_URL,
    BasicAuth,
    BearerAuth,
    BitbucketApi,
    BitbucketApiError,
    _retry_after_seconds,
)


# ---------------------------------------------------------------------
# Auth header construction
# ---------------------------------------------------------------------


class TestAuthHeaders:
    def test_basic_auth_header_value(self) -> None:
        # base64("alice:ATBB-abc123") = "YWxpY2U6QVRCQi1hYmMxMjM="
        auth = BasicAuth(username="alice", password="ATBB-abc123")
        assert auth.header_value() == "Basic YWxpY2U6QVRCQi1hYmMxMjM="

    def test_bearer_auth_header_value(self) -> None:
        assert BearerAuth(token="t1").header_value() == "Bearer t1"

    async def test_get_sets_authorization_header(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            seen["accept"] = request.headers["Accept"]
            seen["ua"] = request.headers["User-Agent"]
            return httpx.Response(200, json={})

        api = BitbucketApi(
            flavor="cloud",
            base_url=DEFAULT_CLOUD_BASE_URL,
            auth=BearerAuth(token="t1"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/repositories/acme")
            assert seen["auth"] == "Bearer t1"
            assert seen["accept"] == "application/json"
            assert seen["ua"] == "pleno-pii-scanner-bitbucket"
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------


class TestConstruction:
    def test_unsupported_flavor_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported bitbucket flavor"):
            BitbucketApi(
                flavor="ghe",  # type: ignore[arg-type]
                base_url="https://x",
                auth=BearerAuth(token="t"),
            )

    async def test_base_url_strip_trailing_slash(self) -> None:
        api = BitbucketApi(
            flavor="cloud",
            base_url="https://api.bitbucket.org/2.0/",
            auth=BearerAuth(token="t"),
        )
        try:
            assert api.base_url == "https://api.bitbucket.org/2.0"
            assert api.flavor == "cloud"
        finally:
            await api.aclose()

    def test_ca_bundle_passed_to_httpx_when_no_transport(self, tmp_path) -> None:
        # When no transport is injected and ca_bundle_path is given, the
        # underlying client should load that PEM into the trust store.
        # We generate a real self-signed cert here so SSLContext.load_verify_locations
        # accepts it; this exercises the production path end-to-end without
        # actually opening a socket.
        from datetime import datetime, timedelta, timezone

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "test-ca.local")]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        bundle = tmp_path / "ca.pem"
        bundle.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        api = BitbucketApi(
            flavor="server",
            base_url="https://bb.acme.internal/rest/api/1.0",
            auth=BearerAuth(token="t"),
            ca_bundle_path=str(bundle),
        )
        assert api.flavor == "server"


# ---------------------------------------------------------------------
# Pagination — Cloud
# ---------------------------------------------------------------------


class TestCloudPagination:
    async def test_walks_next_url_until_absent(self) -> None:
        pages = iter(
            [
                {
                    "values": [{"slug": "r1"}, {"slug": "r2"}],
                    "next": "https://api.bitbucket.org/2.0/repositories/acme?page=2",
                },
                {"values": [{"slug": "r3"}]},
            ]
        )

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(pages))

        api = BitbucketApi(
            flavor="cloud",
            base_url=DEFAULT_CLOUD_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            slugs = [r["slug"] async for r in api.paginate("/repositories/acme")]
            assert slugs == ["r1", "r2", "r3"]
        finally:
            await api.aclose()

    async def test_first_page_includes_pagelen_param(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["query"] = str(request.url.query)
            return httpx.Response(200, json={"values": []})

        api = BitbucketApi(
            flavor="cloud",
            base_url=DEFAULT_CLOUD_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            async for _ in api.paginate("/repositories/acme", page_size=25):
                pass
            assert "pagelen=25" in seen["query"]
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Pagination — Server
# ---------------------------------------------------------------------


class TestServerPagination:
    async def test_walks_isLastPage_with_nextPageStart(self) -> None:
        pages = iter(
            [
                {
                    "values": [{"slug": "p1"}, {"slug": "p2"}],
                    "isLastPage": False,
                    "nextPageStart": 2,
                },
                {
                    "values": [{"slug": "p3"}],
                    "isLastPage": True,
                },
            ]
        )
        seen_starts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_starts.append(str(request.url.query))
            return httpx.Response(200, json=next(pages))

        api = BitbucketApi(
            flavor="server",
            base_url="https://bb.acme/rest/api/1.0",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            slugs = [r["slug"] async for r in api.paginate("/projects/PROD/repos")]
            assert slugs == ["p1", "p2", "p3"]
            # First request must not include `start=`; second must.
            assert "start=" not in seen_starts[0]
            assert "start=2" in seen_starts[1]
        finally:
            await api.aclose()

    async def test_missing_nextPageStart_treated_as_terminal(self) -> None:
        # Defensive: atlassian-python-api shipped a bug for years where
        # `isLastPage=false` but `nextPageStart` was absent caused an
        # infinite loop. We treat that combination as terminal.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"values": [{"slug": "x"}], "isLastPage": False}
            )

        api = BitbucketApi(
            flavor="server",
            base_url="https://bb.acme/rest/api/1.0",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            slugs = [r["slug"] async for r in api.paginate("/projects/PROD/repos")]
            assert slugs == ["x"]
        finally:
            await api.aclose()

    async def test_default_isLastPage_true_when_field_absent(self) -> None:
        # Some custom Bitbucket-compatible servers omit the field on the
        # final page; we must default to True so we do not loop forever.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"values": [{"slug": "y"}]})

        api = BitbucketApi(
            flavor="server",
            base_url="https://bb.acme/rest/api/1.0",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            slugs = [r["slug"] async for r in api.paginate("/projects/PROD/repos")]
            assert slugs == ["y"]
        finally:
            await api.aclose()

    async def test_non_200_response_yields_empty(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"errors": [{"message": "not found"}]})

        api = BitbucketApi(
            flavor="server",
            base_url="https://bb.acme/rest/api/1.0",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            slugs = [r async for r in api.paginate("/projects/MISSING/repos")]
            assert slugs == []
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------


class TestRateLimit:
    async def test_429_then_success_returns_response(self) -> None:
        # First call gets a 429; the wrapper sleeps, retries, and the
        # second call succeeds. Sleep is patched to a coroutine that
        # records but does not actually sleep so the test stays fast.
        calls = {"count": 0}
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        def handler(_: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"})
            return httpx.Response(200, json={"ok": True})

        api = BitbucketApi(
            flavor="cloud",
            base_url=DEFAULT_CLOUD_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            response = await api.get("/repositories/acme")
            assert response.status_code == 200
            assert slept == [7.0]
        finally:
            await api.aclose()

    async def test_429_twice_raises_rate_limited(self) -> None:
        async def fake_sleep(_: float) -> None:
            return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        api = BitbucketApi(
            flavor="cloud",
            base_url=DEFAULT_CLOUD_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(RateLimited, match="bitbucket 429"):
                await api.get("/x")
        finally:
            await api.aclose()

    def test_retry_after_seconds_default_on_missing_header(self) -> None:
        response = httpx.Response(429)
        assert _retry_after_seconds(response) == 30.0

    def test_retry_after_seconds_unparseable_falls_back_to_default(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "not-a-number"})
        assert _retry_after_seconds(response) == 30.0

    def test_retry_after_seconds_clamps_negative_to_zero(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "-5"})
        assert _retry_after_seconds(response) == 0.0

    def test_retry_after_seconds_parses_integer_value(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "12"})
        assert _retry_after_seconds(response) == 12.0


# ---------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------


class TestUrlConstruction:
    async def test_relative_path_joined_to_base_url(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        api = BitbucketApi(
            flavor="server",
            base_url="https://bb.acme/rest/api/1.0",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("projects/PROD")
            assert seen["url"] == "https://bb.acme/rest/api/1.0/projects/PROD"
        finally:
            await api.aclose()

    async def test_absolute_url_passes_through(self) -> None:
        # Cloud's `next` field is an absolute URL — must not be
        # re-prefixed with base_url.
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        api = BitbucketApi(
            flavor="cloud",
            base_url=DEFAULT_CLOUD_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("https://other.host/api/v2/x")
            assert seen["url"].startswith("https://other.host/api/v2/x")
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Pagination depth guard
# ---------------------------------------------------------------------


class TestPaginationGuard:
    async def test_infinite_next_loop_caught(self, monkeypatch) -> None:
        # Patch the depth ceiling down so the test does not have to
        # exercise 10k pages — the guard logic is the same at any cap.
        from pleno_pii_scanner_bitbucket import api as api_mod

        monkeypatch.setattr(api_mod, "_MAX_PAGINATION_DEPTH", 3)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "values": [{"x": 1}],
                    "next": "https://api.bitbucket.org/2.0/repositories/acme?page=loop",
                },
            )

        api = api_mod.BitbucketApi(
            flavor="cloud",
            base_url=api_mod.DEFAULT_CLOUD_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(BitbucketApiError, match="pagination exceeded"):
                async for _ in api.paginate("/repositories/acme"):
                    pass
        finally:
            await api.aclose()
