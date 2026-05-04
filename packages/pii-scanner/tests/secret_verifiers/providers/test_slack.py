from __future__ import annotations

import httpx

from pleno_pii_scanner.secret_verifiers.base import VerifyContext
from pleno_pii_scanner.secret_verifiers.providers.slack import SlackVerifier


def _ctx(handler) -> VerifyContext:
    return VerifyContext(extra={"transport": httpx.MockTransport(handler)})


async def test_live_token_returns_team_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer xoxb-x"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "user_id": "U1",
                "team_id": "T1",
                "team": "acme",
                "user": "bot",
                "url": "https://acme.slack.com/",
            },
        )

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "live"
    assert result.metadata["team_id"] == "T1"


async def test_invalid_auth_is_revoked() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "revoked"
    assert result.detail == "invalid_auth"


async def test_token_revoked_error_is_revoked() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "token_revoked"})

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "revoked"


async def test_ratelimited_returns_rate_limited() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "ratelimited"})

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "rate_limited"


async def test_unknown_slack_error_returns_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "weird_error"})

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "unknown"


async def test_5xx_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_invalid_json_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_non_dict_payload_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_payload_without_error_field_falls_to_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "unknown"


async def test_timeout_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang", request=None)  # type: ignore[arg-type]

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "error"
    assert result.detail == "timeout"


async def test_transport_error_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=None)  # type: ignore[arg-type]

    result = await SlackVerifier().verify("xoxb-x", ctx=_ctx(handler))
    assert result.state == "error"


async def test_no_secret_in_detail_or_metadata() -> None:
    secret = "xoxb-supersecretvalue"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "team_id": "T1"})

    result = await SlackVerifier().verify(secret, ctx=_ctx(handler))
    assert secret not in result.detail
    for value in result.metadata.values():
        assert secret not in str(value)
