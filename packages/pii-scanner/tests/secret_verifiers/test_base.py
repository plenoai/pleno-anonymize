from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pleno_pii_scanner.secret_verifiers.base import (
    LivenessVerifier,
    VerificationResult,
    VerifyContext,
    hash_secret,
)


def test_hash_secret_is_stable_and_redacts_value() -> None:
    digest = hash_secret("ghp_secrettoken")
    assert len(digest) == 32
    assert all(c in "0123456789abcdef" for c in digest)
    assert "secrettoken" not in digest


def test_hash_secret_differs_per_input() -> None:
    assert hash_secret("a") != hash_secret("b")


def test_verify_context_defaults() -> None:
    ctx = VerifyContext()
    assert ctx.timeout_seconds == 5.0
    assert ctx.proxy_url is None
    assert ctx.ca_bundle is None
    assert dict(ctx.extra) == {}


def test_verify_context_overrides() -> None:
    ctx = VerifyContext(
        timeout_seconds=1.0,
        proxy_url="http://proxy:3128",
        ca_bundle=Path("/etc/ssl/cert.pem"),
        extra={"foo": "bar"},
    )
    assert ctx.timeout_seconds == 1.0
    assert ctx.proxy_url == "http://proxy:3128"
    assert ctx.ca_bundle == Path("/etc/ssl/cert.pem")
    assert ctx.extra["foo"] == "bar"


def test_verification_result_defaults() -> None:
    before = datetime.now(UTC)
    result = VerificationResult(state="live")
    after = datetime.now(UTC)
    assert result.state == "live"
    assert result.detail == ""
    assert dict(result.metadata) == {}
    assert result.ttl_seconds == 3600
    assert before <= result.checked_at <= after


def test_verification_result_explicit_metadata() -> None:
    result = VerificationResult(
        state="revoked",
        detail="401",
        metadata={"login": "alice"},
        ttl_seconds=120,
    )
    assert result.metadata["login"] == "alice"
    assert result.ttl_seconds == 120


def test_liveness_verifier_protocol_is_runtime_checkable() -> None:
    class _Stub:
        name = "stub"
        entities = frozenset({"X"})

        async def verify(self, value: str, *, ctx: VerifyContext) -> VerificationResult:
            return VerificationResult(state="unknown")

    assert isinstance(_Stub(), LivenessVerifier)


def test_non_conforming_object_is_not_liveness_verifier() -> None:
    class _NotProtocol:
        pass

    assert not isinstance(_NotProtocol(), LivenessVerifier)
