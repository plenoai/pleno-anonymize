from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.secret_verifiers import (
    CRITICAL_LIVE_ENTITIES,
    HIGH_LIVE_ENTITIES,
    ProviderRegistry,
    VerificationCache,
    VerificationResult,
    VerifyContext,
    build_default_registry,
    severity_for,
    verify_finding,
)
from pleno_pii_scanner.secret_verifiers.providers.github import GitHubVerifier


def _finding(entity: str = "GITHUB_PAT", matched: str = "ghp_token") -> Finding:
    return Finding(
        entity=entity,
        file="x.py",
        line=1,
        col=0,
        score=0.9,
        snippet=matched,
        matched=matched,
        pattern_name="p",
    )


class _StubVerifier:
    def __init__(
        self, name: str, entities: frozenset[str], result: VerificationResult
    ) -> None:
        self.name = name
        self.entities = entities
        self.result = result
        self.calls: list[str] = []

    async def verify(self, value: str, *, ctx: VerifyContext) -> VerificationResult:
        self.calls.append(value)
        return self.result


def test_register_get_for_entity_names_iter_len() -> None:
    registry = ProviderRegistry()
    stub = _StubVerifier("stub", frozenset({"X"}), VerificationResult(state="live"))
    registry.register(stub)
    assert registry.get("stub") is stub
    assert registry.for_entity("X") is stub
    assert registry.names() == ["stub"]
    assert list(registry) == [stub]
    assert len(registry) == 1


def test_register_duplicate_name_rejected() -> None:
    registry = ProviderRegistry()
    registry.register(
        _StubVerifier("a", frozenset({"X"}), VerificationResult(state="live"))
    )
    with pytest.raises(ValueError):
        registry.register(
            _StubVerifier("a", frozenset({"Y"}), VerificationResult(state="live"))
        )


def test_register_duplicate_entity_rejected() -> None:
    registry = ProviderRegistry()
    registry.register(
        _StubVerifier("a", frozenset({"X"}), VerificationResult(state="live"))
    )
    with pytest.raises(ValueError):
        registry.register(
            _StubVerifier("b", frozenset({"X"}), VerificationResult(state="live"))
        )


def test_unregister_removes_provider_and_entity_index() -> None:
    registry = ProviderRegistry()
    stub = _StubVerifier("s", frozenset({"X", "Y"}), VerificationResult(state="live"))
    registry.register(stub)
    registry.unregister("s")
    assert registry.get("s") is None
    assert registry.for_entity("X") is None
    assert registry.for_entity("Y") is None


def test_unregister_unknown_is_noop() -> None:
    registry = ProviderRegistry()
    registry.unregister("missing")


def test_unregister_keeps_overwritten_entity_pointing_at_other() -> None:
    registry = ProviderRegistry()
    a = _StubVerifier("a", frozenset({"X"}), VerificationResult(state="live"))
    b = _StubVerifier("b", frozenset({"Y"}), VerificationResult(state="live"))
    registry.register(a)
    registry.register(b)
    registry._by_entity["X"] = b  # type: ignore[attr-defined]
    registry.unregister("a")
    assert registry.for_entity("X") is b


def test_for_entity_unknown_returns_none() -> None:
    registry = ProviderRegistry()
    assert registry.for_entity("X") is None


def test_severity_for_critical_entity_when_live() -> None:
    for entity in CRITICAL_LIVE_ENTITIES:
        assert severity_for("live", entity) == "critical"


def test_severity_for_high_entity_when_live() -> None:
    for entity in HIGH_LIVE_ENTITIES:
        assert severity_for("live", entity) == "high"


def test_severity_for_unknown_entity_falls_back_to_baseline() -> None:
    assert severity_for("live", "WHATEVER", baseline="medium") == "medium"


def test_severity_for_non_live_uses_baseline() -> None:
    assert severity_for("revoked", "GITHUB_PAT", baseline="low") == "low"
    assert severity_for("error", "GITHUB_PAT", baseline="medium") == "medium"


async def test_verify_finding_no_provider_returns_finding_unchanged() -> None:
    registry = ProviderRegistry()
    finding = _finding(entity="UNKNOWN_ENTITY")
    out = await verify_finding(finding, registry=registry)
    assert out is finding


async def test_verify_finding_live_marks_passed() -> None:
    registry = ProviderRegistry()
    registry.register(
        _StubVerifier("s", frozenset({"GITHUB_PAT"}), VerificationResult(state="live"))
    )
    out = await verify_finding(_finding(), registry=registry)
    assert out.verification == "passed"


async def test_verify_finding_revoked_marks_failed() -> None:
    registry = ProviderRegistry()
    registry.register(
        _StubVerifier(
            "s", frozenset({"GITHUB_PAT"}), VerificationResult(state="revoked")
        )
    )
    out = await verify_finding(_finding(), registry=registry)
    assert out.verification == "failed"


async def test_verify_finding_unknown_keeps_unverified() -> None:
    registry = ProviderRegistry()
    registry.register(
        _StubVerifier(
            "s", frozenset({"GITHUB_PAT"}), VerificationResult(state="unknown")
        )
    )
    out = await verify_finding(_finding(), registry=registry)
    assert out.verification == "unverified"


async def test_verify_finding_error_keeps_unverified() -> None:
    registry = ProviderRegistry()
    registry.register(
        _StubVerifier("s", frozenset({"GITHUB_PAT"}), VerificationResult(state="error"))
    )
    out = await verify_finding(_finding(), registry=registry)
    assert out.verification == "unverified"


async def test_verify_finding_uses_cache_hit() -> None:
    registry = ProviderRegistry()
    stub = _StubVerifier(
        "s", frozenset({"GITHUB_PAT"}), VerificationResult(state="live")
    )
    registry.register(stub)
    cache = VerificationCache()
    finding = _finding()
    await verify_finding(finding, registry=registry, cache=cache)
    await verify_finding(finding, registry=registry, cache=cache)
    assert len(stub.calls) == 1


async def test_verify_finding_does_not_cache_rate_limited() -> None:
    registry = ProviderRegistry()
    stub = _StubVerifier(
        "s",
        frozenset({"GITHUB_PAT"}),
        VerificationResult(state="rate_limited", ttl_seconds=60),
    )
    registry.register(stub)
    cache = VerificationCache()
    await verify_finding(_finding(), registry=registry, cache=cache)
    assert len(cache) == 0


async def test_verify_finding_caps_error_ttl_to_60s() -> None:
    registry = ProviderRegistry()
    stub = _StubVerifier(
        "s",
        frozenset({"GITHUB_PAT"}),
        VerificationResult(
            state="error", ttl_seconds=3600, checked_at=datetime.now(UTC)
        ),
    )
    registry.register(stub)
    cache = VerificationCache()
    await verify_finding(_finding(), registry=registry, cache=cache)
    cached = cache.get(next(iter(_iter_keys(cache))))
    assert cached is not None
    assert cached.ttl_seconds == 60


async def test_verify_finding_keeps_short_error_ttl() -> None:
    registry = ProviderRegistry()
    stub = _StubVerifier(
        "s",
        frozenset({"GITHUB_PAT"}),
        VerificationResult(state="error", ttl_seconds=10),
    )
    registry.register(stub)
    cache = VerificationCache()
    await verify_finding(_finding(), registry=registry, cache=cache)
    cached = cache.get(next(iter(_iter_keys(cache))))
    assert cached is not None
    assert cached.ttl_seconds == 10


async def test_verify_finding_default_ctx_used_when_omitted() -> None:
    seen: list[float] = []

    class _CaptureCtx:
        name = "c"
        entities = frozenset({"GITHUB_PAT"})

        async def verify(self, value: str, *, ctx: VerifyContext) -> VerificationResult:
            seen.append(ctx.timeout_seconds)
            return VerificationResult(state="live")

    registry = ProviderRegistry()
    registry.register(_CaptureCtx())
    await verify_finding(_finding(), registry=registry)
    assert seen == [5.0]


async def test_verify_finding_explicit_ctx_passed_through() -> None:
    seen: list[float] = []

    class _CaptureCtx:
        name = "c"
        entities = frozenset({"GITHUB_PAT"})

        async def verify(self, value: str, *, ctx: VerifyContext) -> VerificationResult:
            seen.append(ctx.timeout_seconds)
            return VerificationResult(state="live")

    registry = ProviderRegistry()
    registry.register(_CaptureCtx())
    await verify_finding(
        _finding(), registry=registry, ctx=VerifyContext(timeout_seconds=2.0)
    )
    assert seen == [2.0]


def test_build_default_registry_with_explicit_providers() -> None:
    stub = _StubVerifier("a", frozenset({"X"}), VerificationResult(state="live"))
    registry = build_default_registry(providers=[stub])
    assert registry.get("a") is stub


def test_build_default_registry_bundled_providers() -> None:
    registry = build_default_registry()
    expected = {"github", "aws", "slack", "stripe", "openai", "generic_bearer"}
    assert expected <= set(registry.names())


def _iter_keys(cache: VerificationCache):
    return list(cache._store)  # type: ignore[attr-defined]


async def test_integration_github_pat_live_via_mocked_transport() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"login": "octocat"})

    transport = httpx.MockTransport(handler)
    registry = ProviderRegistry()
    registry.register(GitHubVerifier())
    finding = _finding(entity="GITHUB_PAT", matched="ghp_" + "a" * 36)
    out = await verify_finding(
        finding,
        registry=registry,
        ctx=VerifyContext(extra={"transport": transport}),
    )
    assert out.verification == "passed"
    assert severity_for("live", out.entity) == "critical"


async def test_cache_expired_entry_triggers_re_probe() -> None:
    registry = ProviderRegistry()
    stub = _StubVerifier(
        "s",
        frozenset({"GITHUB_PAT"}),
        VerificationResult(
            state="live",
            ttl_seconds=1,
            checked_at=datetime.now(UTC) - timedelta(seconds=10),
        ),
    )
    registry.register(stub)
    cache = VerificationCache()
    await verify_finding(_finding(), registry=registry, cache=cache)
    await verify_finding(_finding(), registry=registry, cache=cache)
    assert len(stub.calls) == 2
