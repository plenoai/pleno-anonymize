"""Liveness verification for detected secrets (ADR-0007 §7).

Public surface kept small so downstream packages can vendor a custom
provider without importing the bundled HTTP-bearing providers. The
default provider set is constructed via build_default_registry().
"""

from .base import (
    LivenessVerifier,
    VerificationResult,
    VerificationState,
    VerifyContext,
    hash_secret,
)
from .cache import VerificationCache
from .registry import (
    CRITICAL_LIVE_ENTITIES,
    HIGH_LIVE_ENTITIES,
    ProviderRegistry,
    build_default_registry,
    severity_for,
    verify_finding,
)

__all__ = [
    "CRITICAL_LIVE_ENTITIES",
    "HIGH_LIVE_ENTITIES",
    "LivenessVerifier",
    "ProviderRegistry",
    "VerificationCache",
    "VerificationResult",
    "VerificationState",
    "VerifyContext",
    "build_default_registry",
    "hash_secret",
    "severity_for",
    "verify_finding",
]
