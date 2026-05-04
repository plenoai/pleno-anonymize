"""Liveness verification primitives.

Defines the LivenessVerifier Protocol that every provider implements,
the per-call VerifyContext (timeout / proxy / CA), and the
VerificationResult dataclass returned to the caller. See ADR-0007 §7.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

VerificationState = Literal["live", "revoked", "unknown", "rate_limited", "error"]


_EMPTY_METADATA: Mapping[str, object] = MappingProxyType({})


def hash_secret(value: str) -> str:
    """Stable cache key for a raw secret.

    Using sha256 truncated to 32 hex chars (128 bits) keeps the cache
    file/log safe to share without leaking the underlying token. Hex
    rather than base64 for grep-friendliness in audit logs.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class VerifyContext:
    """Per-call hints for a liveness probe.

    timeout_seconds is enforced by the provider's HTTP client; providers
    must not silently extend it. proxy_url and ca_bundle are advisory —
    a provider may ignore them if it has no network call. extra is a
    free-form bag for provider-specific hints (e.g. AWS secret key
    co-located with an access key id).
    """

    timeout_seconds: float = 5.0
    proxy_url: str | None = None
    ca_bundle: Path | None = None
    extra: Mapping[str, object] = field(default_factory=lambda: _EMPTY_METADATA)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of a single liveness probe.

    state is the only field callers must branch on. detail is a short
    human-readable summary suitable for notifier output, with raw
    secrets redacted. metadata is structured provider data (login id,
    arn, scopes, ...) — also secret-free. ttl_seconds controls cache
    retention; providers shorten it for transient states.
    """

    state: VerificationState
    detail: str = ""
    metadata: Mapping[str, object] = field(default_factory=lambda: _EMPTY_METADATA)
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ttl_seconds: int = 3600


@runtime_checkable
class LivenessVerifier(Protocol):
    """Protocol every provider implements.

    name is the registry key (e.g. "github"). entities is the set of
    Finding.entity values the provider can verify — a registry uses it
    to dispatch findings. verify must be coroutine-safe and must not
    raise on transport / auth failures; it should map them to an
    "error" or "rate_limited" state instead.
    """

    name: str
    entities: frozenset[str]

    async def verify(
        self, value: str, *, ctx: VerifyContext
    ) -> VerificationResult: ...
