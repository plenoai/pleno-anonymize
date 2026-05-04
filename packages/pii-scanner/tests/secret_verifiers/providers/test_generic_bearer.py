from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner.secret_verifiers.base import VerifyContext
from pleno_pii_scanner.secret_verifiers.providers.generic_bearer import (
    GenericBearerVerifier,
)


def _ctx(handler) -> VerifyContext:
    return VerifyContext(extra={"transport": httpx.MockTransport(handler)})


async def test_unset_url_raises_runtime_error() -> None:
    verifier = GenericBearerVerifier()
    with pytest.raises(RuntimeError):
        await verifier.verify("token", ctx=VerifyContext())


async def test_success_status_returns_live() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200)

    verifier = GenericBearerVerifier(url="https://api.example.com/me")
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "live"
    assert result.metadata["status"] == 200
    assert captured["method"] == "GET"
    assert captured["auth"] == "Bearer tok"


async def test_revoked_status_returns_revoked() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    verifier = GenericBearerVerifier(url="https://api.example.com/me")
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "revoked"


async def test_custom_success_and_revoked_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    verifier = GenericBearerVerifier(
        url="https://api.example.com/ping",
        success_status=204,
        revoked_status=403,
    )
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "live"


async def test_custom_method_post() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        return httpx.Response(200)

    verifier = GenericBearerVerifier(
        url="https://api.example.com/me", method="post"
    )
    await verifier.verify("tok", ctx=_ctx(handler))
    assert captured["method"] == "POST"


async def test_429_is_rate_limited() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    verifier = GenericBearerVerifier(url="https://api.example.com/me")
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "rate_limited"


async def test_5xx_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    verifier = GenericBearerVerifier(url="https://api.example.com/me")
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "error"


async def test_unexpected_status_is_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    verifier = GenericBearerVerifier(url="https://api.example.com/me")
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "unknown"


async def test_timeout_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang", request=None)  # type: ignore[arg-type]

    verifier = GenericBearerVerifier(url="https://api.example.com/me")
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "error"
    assert result.detail == "timeout"


async def test_transport_error_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=None)  # type: ignore[arg-type]

    verifier = GenericBearerVerifier(url="https://api.example.com/me")
    result = await verifier.verify("tok", ctx=_ctx(handler))
    assert result.state == "error"


def test_custom_name_overrides_default() -> None:
    verifier = GenericBearerVerifier(
        url="https://api.example.com",
        entity="MY_INTERNAL_TOKEN",
        name="internal_api",
    )
    assert verifier.name == "internal_api"
    assert verifier.entities == frozenset({"MY_INTERNAL_TOKEN"})


def test_default_name_present() -> None:
    verifier = GenericBearerVerifier()
    assert verifier.name == "generic_bearer"
