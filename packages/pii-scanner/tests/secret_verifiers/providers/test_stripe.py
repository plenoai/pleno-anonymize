from __future__ import annotations

import httpx

from pleno_pii_scanner.secret_verifiers.base import VerifyContext
from pleno_pii_scanner.secret_verifiers.providers.stripe import StripeVerifier


def _ctx(handler) -> VerifyContext:
    return VerifyContext(extra={"transport": httpx.MockTransport(handler)})


async def test_live_key_marks_mode_live() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": "acct_123", "country": "US", "default_currency": "usd"},
        )

    result = await StripeVerifier().verify("sk_live_abc", ctx=_ctx(handler))
    assert result.state == "live"
    assert result.metadata["mode"] == "live"
    assert result.metadata["id"] == "acct_123"


async def test_test_key_marks_mode_test() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "acct_test"})

    result = await StripeVerifier().verify("sk_test_abc", ctx=_ctx(handler))
    assert result.state == "live"
    assert result.metadata["mode"] == "test"


async def test_restricted_test_key_marks_mode_test() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "acct_test"})

    result = await StripeVerifier().verify("rk_test_abc", ctx=_ctx(handler))
    assert result.metadata["mode"] == "test"


async def test_live_key_with_invalid_json_falls_back() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    result = await StripeVerifier().verify("sk_live_x", ctx=_ctx(handler))
    assert result.state == "live"
    assert result.metadata["mode"] == "live"


async def test_401_is_revoked() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    result = await StripeVerifier().verify("sk_live_dead", ctx=_ctx(handler))
    assert result.state == "revoked"


async def test_429_is_rate_limited() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    result = await StripeVerifier().verify("sk_live_x", ctx=_ctx(handler))
    assert result.state == "rate_limited"


async def test_5xx_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    result = await StripeVerifier().verify("sk_live_x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_unexpected_status_is_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    result = await StripeVerifier().verify("sk_live_x", ctx=_ctx(handler))
    assert result.state == "unknown"


async def test_timeout_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang", request=None)  # type: ignore[arg-type]

    result = await StripeVerifier().verify("sk_live_x", ctx=_ctx(handler))
    assert result.state == "error"
    assert result.detail == "timeout"


async def test_transport_error_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=None)  # type: ignore[arg-type]

    result = await StripeVerifier().verify("sk_live_x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_no_secret_appears_in_output() -> None:
    secret = "sk_live_supersecret"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "acct_x"})

    result = await StripeVerifier().verify(secret, ctx=_ctx(handler))
    assert secret not in result.detail
    for value in result.metadata.values():
        assert secret not in str(value)
