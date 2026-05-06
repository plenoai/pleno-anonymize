"""Tests for OCI registry auth: realm-token negotiation + parsing."""

from __future__ import annotations


import httpx
import pytest

from pleno_pii_scanner_oci.auth import (
    AnonymousAuth,
    BasicAuth,
    StaticAuth,
    parse_challenge,
)


class TestStaticAuth:
    def test_headers(self) -> None:
        h = StaticAuth("abc123").headers("repo:foo:pull")
        assert h["Authorization"] == "Bearer abc123"


class TestParseChallenge:
    def test_full(self) -> None:
        params = parse_challenge(
            'Bearer realm="https://auth.example/token",service="r.example",scope="repository:lib/alpine:pull"'
        )
        assert params["realm"] == "https://auth.example/token"
        assert params["service"] == "r.example"
        assert params["scope"] == "repository:lib/alpine:pull"

    def test_only_bearer_supported(self) -> None:
        with pytest.raises(ValueError, match="only Bearer"):
            parse_challenge("Basic realm=foo")

    def test_quoted_value_with_comma(self) -> None:
        params = parse_challenge('Bearer realm="https://auth/token",scope="a,b,c"')
        assert params["scope"] == "a,b,c"

    def test_ignores_malformed_pair(self) -> None:
        params = parse_challenge('Bearer realm="https://auth/token",noequalshere')
        assert "realm" in params
        assert "noequalshere" not in params


class TestBasicAuth:
    async def test_token_exchange(self) -> None:
        seen_request: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_request["url"] = str(request.url)
            seen_request["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={"token": "token-from-realm"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            auth = BasicAuth("user", "pass")
            token = await auth.fetch_token(
                client, "https://auth.example/token", "repo:foo:pull", "r.example"
            )
        assert token == "token-from-realm"
        assert "service=r.example" in seen_request["url"]
        assert (
            "scope=repo%3Afoo%3Apull" in seen_request["url"]
            or "scope=repo:foo:pull" in seen_request["url"]
        )
        assert seen_request["auth"].startswith("Basic ")

    async def test_accepts_access_token_field(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "alt"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = await BasicAuth("u", "p").fetch_token(
                client, "https://x/token", "s", "v"
            )
        assert token == "alt"

    async def test_missing_token_field_rejected(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unrelated": "value"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="neither 'token'"):
                await BasicAuth("u", "p").fetch_token(
                    client, "https://x/token", "s", "v"
                )


class TestAnonymousAuth:
    async def test_no_credentials_sent(self) -> None:
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"token": "anon"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            token = await AnonymousAuth().fetch_token(
                client, "https://x/token", "s", "v"
            )
        assert token == "anon"
        assert captured["auth"] is None

    async def test_missing_token_rejected(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError, match="neither 'token'"):
                await AnonymousAuth().fetch_token(client, "https://x/token", "s", "v")
