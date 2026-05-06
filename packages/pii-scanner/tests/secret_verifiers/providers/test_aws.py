from __future__ import annotations

import datetime as _dt

import httpx

from pleno_pii_scanner.secret_verifiers.base import VerifyContext
from pleno_pii_scanner.secret_verifiers.providers.aws import (
    AwsVerifier,
    _between,
    _derive_signing_key,
    sigv4_sign_post,
)


def test_between_returns_none_when_start_missing() -> None:
    assert _between("hello", "<x>", "</x>") is None


def test_between_returns_none_when_end_missing() -> None:
    assert _between("<x>hello", "<x>", "</x>") is None


def test_between_extracts_payload() -> None:
    assert _between("<x>hi</x>", "<x>", "</x>") == "hi"


def _ctx(handler, **extra) -> VerifyContext:
    base = {"transport": httpx.MockTransport(handler)}
    base.update(extra)
    return VerifyContext(extra=base)


# AWS docs fixture: derive_signing_key for the SigV4 worked example.
# https://docs.aws.amazon.com/general/latest/gr/signature-v4-examples.html
def test_derive_signing_key_matches_aws_docs_example() -> None:
    key = _derive_signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "20120215",
        "us-east-1",
        "iam",
    )
    assert (
        key.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"
    )


def test_sigv4_sign_post_is_deterministic_for_fixed_inputs() -> None:
    now = _dt.datetime(2024, 1, 1, 0, 0, 0, tzinfo=_dt.UTC)
    headers = sigv4_sign_post(
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        session_token=None,
        region="us-east-1",
        host="sts.us-east-1.amazonaws.com",
        body="Action=GetCallerIdentity&Version=2011-06-15",
        now=now,
    )
    # Frozen-input regression check: any change to the canonical
    # request, signing key derivation, or header set will flip this.
    assert headers["X-Amz-Date"] == "20240101T000000Z"
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 "
        "Credential=AKIAIOSFODNN7EXAMPLE/20240101/us-east-1/sts/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, "
        "Signature=e9721ac88355e640c5d4905545f059e8497163ab63b644b68a673cb59461df4f"
    )


def test_sigv4_sign_post_includes_session_token_in_signed_headers() -> None:
    now = _dt.datetime(2024, 1, 1, 0, 0, 0, tzinfo=_dt.UTC)
    headers = sigv4_sign_post(
        access_key_id="AKIA",
        secret_access_key="s",
        session_token="FQoG...",
        region="us-east-1",
        host="sts.us-east-1.amazonaws.com",
        body="Action=GetCallerIdentity&Version=2011-06-15",
        now=now,
    )
    assert headers["X-Amz-Security-Token"] == "FQoG..."
    assert "x-amz-security-token" in headers["Authorization"]


async def test_missing_secret_returns_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called when secret is missing")

    result = await AwsVerifier().verify("AKIAIOSFODNN7EXAMPLE", ctx=_ctx(handler))
    assert result.state == "unknown"
    assert "aws_secret_access_key" in result.detail


async def test_non_string_secret_returns_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    result = await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(handler, aws_secret_access_key=123),
    )
    assert result.state == "unknown"


async def test_live_credentials_extract_arn_account_userid() -> None:
    sample = """<?xml version="1.0"?>
<GetCallerIdentityResponse><GetCallerIdentityResult>
<Arn>arn:aws:iam::123456789012:user/alice</Arn>
<Account>123456789012</Account>
<UserId>AIDAEXAMPLE</UserId>
</GetCallerIdentityResult></GetCallerIdentityResponse>
"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "Authorization" in request.headers
        assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        return httpx.Response(200, content=sample.encode())

    result = await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(
            handler,
            aws_secret_access_key="secret",
            aws_region="us-west-2",
            aws_session_token="tok",
        ),
    )
    assert result.state == "live"
    assert result.metadata["arn"] == "arn:aws:iam::123456789012:user/alice"
    assert result.metadata["account"] == "123456789012"
    assert result.metadata["user_id"] == "AIDAEXAMPLE"


async def test_live_response_without_arn_falls_back_in_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<x/>")

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "live"
    assert "arn" not in result.metadata


async def test_403_with_error_code_is_revoked() -> None:
    body = "<ErrorResponse><Error><Code>InvalidClientTokenId</Code></Error></ErrorResponse>"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=body.encode())

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "revoked"
    assert "InvalidClientTokenId" in result.detail


async def test_401_without_error_code_is_revoked_with_default_detail() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"<x/>")

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "revoked"
    assert "unauthorized" in result.detail


async def test_429_is_rate_limited() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "rate_limited"


async def test_5xx_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "error"


async def test_unexpected_status_is_unknown() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "unknown"


async def test_timeout_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang", request=None)  # type: ignore[arg-type]

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "error"


async def test_transport_error_is_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns", request=None)  # type: ignore[arg-type]

    result = await AwsVerifier().verify(
        "AKIA", ctx=_ctx(handler, aws_secret_access_key="s")
    )
    assert result.state == "error"


async def test_no_secret_appears_in_output() -> None:
    secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<x><Arn>arn:aws:iam::1:user/x</Arn></x>")

    result = await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(
            handler,
            aws_secret_access_key=secret,
            aws_session_token="sessiontoken",
        ),
    )
    assert secret not in result.detail
    for value in result.metadata.values():
        assert secret not in str(value)


async def test_non_string_session_token_is_ignored() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["Authorization"]
        captured["security_token"] = request.headers.get("X-Amz-Security-Token", "")
        return httpx.Response(200, content=b"<x/>")

    result = await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(
            handler,
            aws_secret_access_key="s",
            aws_session_token=12345,
        ),
    )
    assert result.state == "live"
    assert captured["security_token"] == ""


async def test_aws_now_override_used_when_provided() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["date"] = request.headers["X-Amz-Date"]
        return httpx.Response(200, content=b"<x/>")

    fixed = _dt.datetime(2025, 6, 1, 12, 0, 0, tzinfo=_dt.UTC)
    await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(
            handler,
            aws_secret_access_key="s",
            _aws_now=fixed,
        ),
    )
    assert captured["date"] == "20250601T120000Z"


async def test_non_datetime_aws_now_is_ignored() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<x/>")

    result = await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(
            handler,
            aws_secret_access_key="s",
            _aws_now="not-a-datetime",
        ),
    )
    assert result.state == "live"


async def test_empty_region_falls_back_to_default() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.headers["Host"]
        return httpx.Response(200, content=b"<x/>")

    await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(handler, aws_secret_access_key="s", aws_region=""),
    )
    assert captured["host"] == "sts.us-east-1.amazonaws.com"


async def test_non_string_region_falls_back_to_default() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["host"] = request.headers["Host"]
        return httpx.Response(200, content=b"<x/>")

    await AwsVerifier().verify(
        "AKIA",
        ctx=_ctx(handler, aws_secret_access_key="s", aws_region=42),
    )
    assert captured["host"] == "sts.us-east-1.amazonaws.com"
