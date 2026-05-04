from __future__ import annotations

import httpx

from pleno_pii_scanner.secret_verifiers.base import VerifyContext
from pleno_pii_scanner.secret_verifiers.providers.openai import OpenAiVerifier


def _ctx(handler) -> VerifyContext:
    return VerifyContext(extra={"transport": httpx.MockTransport(handler)})


async def test_live_key_returns_model_count() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-x"
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "live"
    assert result.metadata["model_count"] == 2


async def test_live_key_without_data_field() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "live"
    assert "model_count" not in result.metadata


async def test_live_key_with_invalid_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"oops")

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "live"


async def test_401_is_revoked() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "revoked"


async def test_429_is_rate_limited() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "rate_limited"


async def test_5xx_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_unexpected_status_is_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "unknown"


async def test_timeout_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang", request=None)  # type: ignore[arg-type]

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_transport_error_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=None)  # type: ignore[arg-type]

    result = await OpenAiVerifier().verify("sk-x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_no_secret_in_detail_or_metadata() -> None:
    secret = "sk-supersecret"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    result = await OpenAiVerifier().verify(secret, ctx=_ctx(handler))
    assert secret not in result.detail
    for value in result.metadata.values():
        assert secret not in str(value)
