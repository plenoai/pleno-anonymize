"""Provider registry + verify_finding integration.

Decouples Finding dispatch from concrete provider classes so the
scheduler can register custom providers (BYOD) at startup without
patching this module. The default registry is built lazily — providers
import HTTP clients, and tests that exercise only the cache should not
pay that cost.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator
from typing import Final

from ..models import Finding
from .base import (
    LivenessVerifier,
    VerificationResult,
    VerifyContext,
    hash_secret,
)
from .cache import VerificationCache

CRITICAL_LIVE_ENTITIES: Final[frozenset[str]] = frozenset(
    {
        "AWS_ACCESS_KEY",
        "AWS_SECRET_KEY",
        "GITHUB_PAT",
        "GITHUB_APP_TOKEN",
        "STRIPE_LIVE_KEY",
        "OPENAI_API_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_USER_TOKEN",
    }
)
HIGH_LIVE_ENTITIES: Final[frozenset[str]] = frozenset(
    {
        "STRIPE_RESTRICTED_KEY",
        "STRIPE_TEST_KEY",
        "GENERIC_BEARER_TOKEN",
    }
)

# error state is cached briefly to absorb transient network blips
# without re-hammering the upstream API on every finding in a batch.
ERROR_TTL_SECONDS: Final[int] = 60


def severity_for(state: str, entity: str, baseline: str = "medium") -> str:
    """Map a verification state + entity to an effective severity.

    Public so the scheduler can surface the same severity in
    notifications without re-running verification. baseline is the
    Finding's pre-verification severity (kept for revoked/error/etc.).
    """
    if state == "live":
        if entity in CRITICAL_LIVE_ENTITIES:
            return "critical"
        if entity in HIGH_LIVE_ENTITIES:
            return "high"
        return baseline
    return baseline


class ProviderRegistry:
    """Registry of LivenessVerifier instances keyed by provider name.

    A second index by entity speeds up verify_finding dispatch — the
    scanner can produce thousands of findings per scan and per-finding
    iteration over all providers would dominate wall time.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, LivenessVerifier] = {}
        self._by_entity: dict[str, LivenessVerifier] = {}

    def register(self, provider: LivenessVerifier) -> None:
        if provider.name in self._by_name:
            raise ValueError(f"provider already registered: {provider.name!r}")
        for entity in provider.entities:
            if entity in self._by_entity:
                raise ValueError(
                    f"entity {entity!r} already handled by "
                    f"{self._by_entity[entity].name!r}"
                )
        self._by_name[provider.name] = provider
        for entity in provider.entities:
            self._by_entity[entity] = provider

    def unregister(self, name: str) -> None:
        provider = self._by_name.pop(name, None)
        if provider is None:
            return
        for entity in provider.entities:
            # Defensive pop: another provider might have overwritten
            # the entity index in a buggy custom registration. We only
            # delete entries that still point at the removed provider.
            if self._by_entity.get(entity) is provider:
                del self._by_entity[entity]

    def get(self, name: str) -> LivenessVerifier | None:
        return self._by_name.get(name)

    def for_entity(self, entity: str) -> LivenessVerifier | None:
        return self._by_entity.get(entity)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def __iter__(self) -> Iterator[LivenessVerifier]:
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)


def build_default_registry(
    providers: Iterable[LivenessVerifier] | None = None,
) -> ProviderRegistry:
    """Construct a registry with the bundled providers.

    Lazy import keeps import-time cost off callers that only need the
    Protocol / cache (e.g. unit tests, notifier formatting).
    """
    registry = ProviderRegistry()
    if providers is None:
        from .providers.aws import AwsVerifier
        from .providers.generic_bearer import GenericBearerVerifier
        from .providers.github import GitHubVerifier
        from .providers.openai import OpenAiVerifier
        from .providers.slack import SlackVerifier
        from .providers.stripe import StripeVerifier

        providers = (
            GitHubVerifier(),
            AwsVerifier(),
            SlackVerifier(),
            StripeVerifier(),
            OpenAiVerifier(),
            GenericBearerVerifier(),
        )
    for provider in providers:
        registry.register(provider)
    return registry


async def verify_finding(
    finding: Finding,
    *,
    registry: ProviderRegistry,
    cache: VerificationCache | None = None,
    ctx: VerifyContext | None = None,
) -> Finding:
    """Verify a single Finding via its provider.

    Returns a new Finding with verification updated. severity bump
    information is surfaced via severity_for(); we do not mutate
    Finding.severity because the dataclass has no such field today —
    callers (Scheduler #7, Notifier #9) read severity_for(...) when
    they need it. State -> verification mapping:
      live           -> "passed"
      revoked        -> "failed"
      unknown / rate_limited / error -> unchanged
    """
    provider = registry.for_entity(finding.entity)
    if provider is None:
        return finding
    effective_ctx = ctx if ctx is not None else VerifyContext()
    key = hash_secret(finding.matched)
    result: VerificationResult | None = None
    if cache is not None:
        result = cache.get(key)
    if result is None:
        result = await provider.verify(finding.matched, ctx=effective_ctx)
        if cache is not None and result.state != "rate_limited":
            cache.put(key, _persisted(result))
    return _apply(finding, result)


def _persisted(result: VerificationResult) -> VerificationResult:
    """Shrink TTL for transient states so retries happen sooner.

    rate_limited is filtered earlier (we never cache it). error is
    cached for ERROR_TTL_SECONDS to absorb scan-batch storms without
    pinning a wrong verdict.
    """
    if result.state == "error" and result.ttl_seconds > ERROR_TTL_SECONDS:
        return dataclasses.replace(result, ttl_seconds=ERROR_TTL_SECONDS)
    return result


def _apply(finding: Finding, result: VerificationResult) -> Finding:
    if result.state == "live":
        return dataclasses.replace(finding, verification="passed")
    if result.state == "revoked":
        return dataclasses.replace(finding, verification="failed")
    return finding
