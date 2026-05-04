from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner.secret_verifiers.base import VerifyContext
from pleno_pii_scanner.secret_verifiers.providers.github import GitHubVerifier


def _ctx(handler) -> VerifyContext:
    return VerifyContext(extra={"transport": httpx.MockTransport(handler)})


async def test_live_token_returns_login_in_metadata() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["Authorization"]
        captured["accept"] = request.headers["Accept"]
        return httpx.Response(
            200,
            json={"login": "octocat"},
            headers={"x-oauth-scopes": "repo, read:user"},
        )

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "live"
    assert result.metadata["login"] == "octocat"
    assert result.metadata["scopes"] == "repo, read:user"
    assert "ghp_x" not in result.detail
    assert captured["auth"] == "Bearer ghp_x"
    assert captured["accept"] == "application/vnd.github+json"


async def test_live_token_without_login_field_still_live() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "live"
    assert "login" not in result.metadata


async def test_live_token_with_non_dict_payload() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "live"
    assert "login" not in result.metadata


async def test_live_token_with_invalid_json_body() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "live"


async def test_revoked_returns_revoked_state() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    result = await GitHubVerifier().verify("ghp_dead", ctx=_ctx(handler))
    assert result.state == "revoked"
    assert "ghp_dead" not in result.detail


async def test_403_returns_rate_limited_with_remaining_header() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0"})

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "rate_limited"
    assert "remaining=0" in result.detail


async def test_5xx_returns_error_with_short_ttl() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "error"
    assert result.ttl_seconds == 60


async def test_unexpected_status_is_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "unknown"


async def test_timeout_is_mapped_to_error_state() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang", request=None)  # type: ignore[arg-type]

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "error"
    assert result.detail == "timeout"


async def test_transport_error_is_mapped_to_error_state() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=None)  # type: ignore[arg-type]

    result = await GitHubVerifier().verify("ghp_x", ctx=_ctx(handler))
    assert result.state == "error"
    assert "ConnectError" in result.detail


def test_entities_cover_documented_token_shapes() -> None:
    entities = GitHubVerifier().entities
    assert {"GITHUB_PAT", "GITHUB_FINE_GRAINED_PAT", "GITHUB_APP_TOKEN"} <= entities


@pytest.mark.parametrize("status", [200, 401, 403, 503, 418])
async def test_no_secret_appears_in_detail(status: int) -> None:
    secret = "ghp_topsecretvalue"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    result = await GitHubVerifier().verify(secret, ctx=_ctx(handler))
    assert secret not in result.detail
    for value in result.metadata.values():
        assert secret not in str(value)
