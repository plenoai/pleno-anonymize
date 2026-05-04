"""Tests for AzureDevOpsApi: header injection, pagination, rate-limit, CA bundle."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner_azure_devops.api import (
    CONTINUATION_TOKEN_HEADER,
    DEFAULT_API_VERSION,
    SERVICES_DEFAULT_HOST,
    AzureDevOpsApi,
)
from pleno_pii_scanner_azure_devops.auth import AzureDevOpsAuth


def _api(handler, *, base_url: str | None = None, **kw) -> AzureDevOpsApi:
    return AzureDevOpsApi(
        base_url=base_url or f"{SERVICES_DEFAULT_HOST}/contoso",
        auth=AzureDevOpsAuth.pat("p"),
        transport=httpx.MockTransport(handler),
        **kw,
    )


class TestConstants:
    def test_continuation_token_header_lowercase(self) -> None:
        # Tests assert against the lowercased form; document it here.
        assert CONTINUATION_TOKEN_HEADER == "x-ms-continuationtoken"


class TestHeaders:
    async def test_authorization_pat_basic(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            seen["accept"] = request.headers.get("Accept", "")
            seen["ua"] = request.headers.get("User-Agent", "")
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            await api.get("/_apis/projects")
            assert seen["auth"].startswith("Basic ")
            assert seen["accept"] == "application/json"
            assert "pleno-pii-scanner-azure-devops" in seen["ua"]
        finally:
            await api.aclose()

    async def test_authorization_oauth_bearer(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            return httpx.Response(200, json={"value": []})

        api = AzureDevOpsApi(
            base_url=f"{SERVICES_DEFAULT_HOST}/contoso",
            auth=AzureDevOpsAuth.oauth("xyz"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/_apis/projects")
            assert seen["auth"] == "Bearer xyz"
        finally:
            await api.aclose()

    async def test_api_version_query_stamped(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            await api.get("/_apis/projects")
            assert f"api-version={DEFAULT_API_VERSION}" in seen["url"]
        finally:
            await api.aclose()

    async def test_custom_api_version(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"value": []})

        api = _api(handler, api_version="6.0")
        try:
            await api.get("/_apis/projects")
            assert "api-version=6.0" in seen["url"]
            assert api.api_version == "6.0"
        finally:
            await api.aclose()

    async def test_extra_params_merged_with_api_version(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            await api.get("/_apis/projects", params={"$top": "100"})
            assert "%24top=100" in seen["url"] or "$top=100" in seen["url"]
            assert "api-version=" in seen["url"]
        finally:
            await api.aclose()


class TestPaths:
    async def test_relative_joined(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            await api.get("/_apis/projects")
            assert seen["url"].startswith(
                "https://dev.azure.com/contoso/_apis/projects?"
            )
        finally:
            await api.aclose()

    async def test_absolute_passthrough(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            await api.get("https://example.com/x")
            assert seen["url"].startswith("https://example.com/x?")
        finally:
            await api.aclose()

    def test_base_url_strips_trailing_slash(self) -> None:
        api = AzureDevOpsApi(
            base_url="https://dev.azure.com/contoso/",
            auth=AzureDevOpsAuth.pat("p"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
        )
        assert api.base_url == "https://dev.azure.com/contoso"


class TestRateLimited:
    async def test_429_with_retry_after(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "30"})

        api = _api(handler)
        try:
            with pytest.raises(RateLimited, match="429"):
                await api.get("/_apis/projects")
        finally:
            await api.aclose()

    async def test_429_without_retry_after_still_raises(self) -> None:
        # Upstream omitting the header is malformed but observed; we
        # still raise so the scheduler backs off (with a default).
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        api = _api(handler)
        try:
            with pytest.raises(RateLimited, match="429"):
                await api.get("/x")
        finally:
            await api.aclose()

    async def test_403_does_not_raise_rate_limited(self) -> None:
        # Azure DevOps does not use a secondary 403 form. A real 403
        # is auth/authz failure and must surface to the caller.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "forbidden"})

        api = _api(handler)
        try:
            response = await api.get("/x")
            assert response.status_code == 403
        finally:
            await api.aclose()


class TestPagination:
    async def test_two_pages_via_continuation_header(self) -> None:
        # Page 1: returns header. Page 2: no header => loop ends.
        page = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page["n"] += 1
            if page["n"] == 1:
                return httpx.Response(
                    200,
                    json={"value": [{"name": "p1"}]},
                    headers={CONTINUATION_TOKEN_HEADER: "tok-2"},
                )
            assert "continuationToken=tok-2" in str(request.url)
            return httpx.Response(200, json={"value": [{"name": "p2"}]})

        api = _api(handler)
        try:
            collected: list[str] = []
            async for response in api.get_paginated("/_apis/projects"):
                for v in response.json()["value"]:
                    collected.append(v["name"])
            assert collected == ["p1", "p2"]
            assert page["n"] == 2
        finally:
            await api.aclose()

    async def test_single_page_when_header_absent(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"value": [{"name": "only"}]})

        api = _api(handler)
        try:
            pages = [r async for r in api.get_paginated("/_apis/projects")]
            assert len(pages) == 1
        finally:
            await api.aclose()

    async def test_continuation_token_is_threaded_in_query(self) -> None:
        seen: list[str] = []
        page = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            page["n"] += 1
            if page["n"] < 3:
                return httpx.Response(
                    200,
                    json={"value": []},
                    headers={CONTINUATION_TOKEN_HEADER: f"t{page['n']}"},
                )
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            _ = [r async for r in api.get_paginated("/_apis/projects")]
            assert "continuationToken" not in seen[0]
            assert "continuationToken=t1" in seen[1]
            assert "continuationToken=t2" in seen[2]
        finally:
            await api.aclose()

    async def test_extra_params_preserved_across_pages(self) -> None:
        page = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            assert "stateFilter=wellFormed" in str(request.url)
            page["n"] += 1
            if page["n"] == 1:
                return httpx.Response(
                    200,
                    json={"value": []},
                    headers={CONTINUATION_TOKEN_HEADER: "x"},
                )
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            _ = [
                r
                async for r in api.get_paginated(
                    "/_apis/projects", params={"stateFilter": "wellFormed"}
                )
            ]
            assert page["n"] == 2
        finally:
            await api.aclose()


class TestCABundle:
    async def test_ca_bundle_path_missing_file_raises(self, tmp_path: Path) -> None:
        # Constructing the API with a bogus CA path is OK (lazy); the
        # error surfaces on first request when ssl.create_default_context
        # tries to load the file.
        api = AzureDevOpsApi(
            base_url="https://example/",
            auth=AzureDevOpsAuth.pat("p"),
            ca_bundle_path=tmp_path / "nope.pem",
        )
        try:
            with pytest.raises((FileNotFoundError, ssl.SSLError)):
                await api.get("/_apis/projects")
        finally:
            await api.aclose()

    async def test_ca_bundle_path_real_file_loads(self, tmp_path: Path) -> None:
        # We don't have a real CA, but Python's certifi-vendored bundle
        # ships one; alternatively, we can write a self-signed cert PEM.
        # Simplest portable trick: generate a 1-key minimal self-signed
        # cert at runtime. Avoids depending on certifi.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import UTC, datetime, timedelta

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [x509.NameAttribute(x509.NameOID.COMMON_NAME, "test-ca")]
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
            .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(key, hashes.SHA256())
        )
        ca_path = tmp_path / "ca.pem"
        ca_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        # Build the API with the CA bundle. We can't actually issue a
        # request (no server), but client construction must succeed.
        api = AzureDevOpsApi(
            base_url="https://example/",
            auth=AzureDevOpsAuth.pat("p"),
            ca_bundle_path=ca_path,
        )
        try:
            client = api._ensure_client()
            assert client is not None
        finally:
            await api.aclose()


class TestLifecycle:
    async def test_aclose_idempotent(self) -> None:
        api = _api(lambda _: httpx.Response(200, json={"value": []}))
        await api.aclose()
        await api.aclose()  # must not raise

    async def test_aclose_without_use(self) -> None:
        # Build but never request — _ensure_client never fired, but
        # aclose must still succeed and close the auth too.
        api = AzureDevOpsApi(
            base_url="https://x",
            auth=AzureDevOpsAuth.pat("p"),
        )
        await api.aclose()

    async def test_client_reused_across_requests(self) -> None:
        n = {"calls": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            n["calls"] += 1
            return httpx.Response(200, json={"value": []})

        api = _api(handler)
        try:
            await api.get("/x")
            client = api._client
            await api.get("/y")
            assert api._client is client  # same instance
            assert n["calls"] == 2
        finally:
            await api.aclose()
