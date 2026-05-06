"""CredentialProfile + assume-role hop chain.

A profile names a base identity (resolved by CredentialBroker) and an
ordered list of cross-account / cross-tenant `assume_role` hops. The
broker resolves the base, then walks each hop through the registered
plugin (AWS STS, GCP Service Account impersonation, Azure on-behalf-of).
The plugins themselves live in per-cloud wheels so the core does not
import boto3 / google-auth / azure-identity. This file only contains
the registration plumbing and chain walker.

See ADR-0007 §3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pleno_pii_scanner.credentials.broker import (
    Credential,
    CredentialBroker,
)

# Hop applier signature: receives the previous-hop Credential and the
# AssumeRoleHop spec, returns the new Credential. Implementations are
# async so STS / impersonation HTTP calls do not block the event loop.
HopApplier = Callable[[Credential, "AssumeRoleHop"], Awaitable[Credential]]

_HOP_PLUGINS: dict[str, HopApplier] = {}


@dataclass(frozen=True, slots=True)
class AssumeRoleHop:
    """One step in a multi-account assume-role chain.

    `provider` selects the plugin ("aws", "gcp", "azure"). `role_arn_or_id`
    is the target principal (AWS ARN, GCP service account email, Azure
    principal id). `external_id` defends against the AWS confused-deputy
    pattern; required when crossing AWS account boundaries to a role
    that requires it. `session_name` shows up in CloudTrail / audit
    logs. `duration_seconds` bounds short-lived credential lifetime.
    """

    provider: str
    role_arn_or_id: str
    external_id: str | None = None
    session_name: str = "pleno-pii-scanner"
    duration_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class CredentialProfile:
    """Named credential acquisition recipe.

    `base` is a `kind:name` string consumed by CredentialBroker.get
    (e.g. "aws:default", "github:work"). `chain` is applied in order
    after the base resolves, so the final Credential carries the
    permissions of the deepest role.
    """

    name: str
    base: str
    chain: tuple[AssumeRoleHop, ...] = ()

    def base_kind_name(self) -> tuple[str, str]:
        """Split `base` into `(kind, name)`; defaults name to "default".

        Raises ValueError if `base` is empty or the kind is empty after
        splitting — empty base would silently resolve nothing.
        """
        if not self.base:
            raise ValueError(f"profile {self.name!r} has empty base")
        kind, _, name = self.base.partition(":")
        if not kind:
            raise ValueError(f"profile {self.name!r} base {self.base!r} missing kind")
        return kind, name or "default"


def register_hop_plugin(provider: str, applier: HopApplier) -> None:
    """Register an AssumeRole applier for `provider`.

    Called by per-cloud wheels at import time (typically through their
    entry-point loader). Re-registration replaces the existing applier
    so a test fixture can stub plugins without unregister boilerplate.
    """
    _HOP_PLUGINS[provider] = applier


def unregister_hop_plugin(provider: str) -> None:
    """Drop the applier for `provider`. Mostly for test cleanup."""
    _HOP_PLUGINS.pop(provider, None)


def registered_hop_providers() -> tuple[str, ...]:
    """Snapshot of provider names currently wired up."""
    return tuple(sorted(_HOP_PLUGINS))


async def apply_chain(
    broker: CredentialBroker, profile: CredentialProfile
) -> Credential:
    """Resolve `profile.base`, then walk every hop through its plugin.

    Raises NotImplementedError when a hop's provider has no registered
    applier — explicit signal to install the corresponding wheel
    (`pleno-pii-scanner-aws` etc.). The unmodified base Credential is
    returned when `chain` is empty.
    """
    kind, name = profile.base_kind_name()
    cred = await broker.get(kind, name)
    for hop in profile.chain:
        applier = _HOP_PLUGINS.get(hop.provider)
        if applier is None:
            raise NotImplementedError(
                f"{hop.provider} assume_role plugin not loaded; "
                f"install pleno-pii-scanner-{hop.provider}"
            )
        cred = await applier(cred, hop)
    return cred
