"""CredentialBroker — priority-ordered credential resolution.

The Scheduler (#7) and every SourceConnector (#1) request credentials by
`(kind, name)` and never touch resolvers directly. The broker walks a
priority-ordered list of CredentialResolver implementations and returns
the first hit. This keeps connector code free of "is this env or
keyring or instance metadata" branching, and lets enterprise operators
swap in Vault / 1Password / SecretsManager resolvers as plugins without
forking the connectors.

See ADR-0007 §3.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

# Keys whose values must be redacted in repr / str. Any Mapping key that
# matches one of these substrings (case-insensitive) is rendered as
# "***". Numeric / non-secret metadata (app_id, role_arn, region) keeps
# its real value to stay debug-useful.
_SECRET_KEY_HINTS: tuple[str, ...] = (
    "token",
    "secret",
    "key",
    "password",
    "passwd",
    "private",
    "credential",
    "session",
    "cert",
    "pem",
)

# Allow-list overrides: substring match would otherwise hide these
# operationally-important non-secret fields. e.g. "access_key_id" contains
# "key" but the id itself is logged in CloudTrail anyway, and "key_id"
# alone (KMS key id, app key id) is metadata not a secret. We still mask
# the paired *_secret_* counterpart.
_NON_SECRET_KEY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "access_key_id",
        "key_id",
        "kid",
        "public_key",
    }
)


def _is_secret_key(key: str) -> bool:
    """Return True if `key` should be masked in repr / str output.

    Match is case-insensitive substring against `_SECRET_KEY_HINTS`,
    minus the explicit `_NON_SECRET_KEY_ALLOWLIST` entries which are
    public identifiers even when they happen to contain "key".
    """
    lowered = key.lower()
    if lowered in _NON_SECRET_KEY_ALLOWLIST:
        return False
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _mask_payload(payload: Mapping[str, object]) -> dict[str, object]:
    # Render each entry: secret keys -> "***", others -> repr of value.
    # Done up front so __repr__ never accidentally inlines the raw secret.
    return {k: ("***" if _is_secret_key(k) else v) for k, v in payload.items()}


class CredentialError(Exception):
    """Base class for credential broker errors."""


class CredentialNotFoundError(CredentialError):
    """No resolver returned a Credential for the requested (kind, name)."""


class CredentialMisconfiguredError(CredentialError):
    """A resolver source (file / env / keyring) is malformed.

    Distinct from NotFound: the user *intended* to provide a credential
    but the source is unparseable (broken TOML, env without value,
    keyring entry with wrong shape). Surfaces loudly so misconfiguration
    is not silently treated as missing credential.
    """


@dataclass(frozen=True, slots=True)
class Credential:
    """In-memory bundle for a single credential.

    `payload` carries provider-specific fields (token, access_key_id +
    secret_access_key, app_id + private_key, role_arn + external_id,
    ...). Secret-like keys are masked in repr/str via `_is_secret_key`
    so the value never lands in logs by accident. `expires_at` lets the
    Scheduler proactively refresh short-lived STS / OIDC creds before
    they fail mid-scan; `refresh_callback` produces a new Credential
    when invoked (used by AssumeRole hop chains and OIDC plugins).
    """

    kind: str
    payload: Mapping[str, object]
    expires_at: datetime | None = None
    source: str = ""
    refresh_callback: Callable[[], Awaitable["Credential"]] | None = None

    def __repr__(self) -> str:
        masked = _mask_payload(self.payload)
        return (
            f"Credential(kind={self.kind!r}, source={self.source!r}, "
            f"payload={masked!r}, expires_at={self.expires_at!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


@runtime_checkable
class CredentialResolver(Protocol):
    """Lookup interface implemented by file / env / keyring / cloud / OIDC.

    Implementations are pure-async so a network-bound resolver
    (Vault, IMDSv2, OIDC token exchange) can be added without changing
    the broker contract. `priority` is consulted at registration; higher
    wins. `name` is used in CredentialNotFoundError messages so the user
    can see which resolver order was attempted.
    """

    name: str
    priority: int

    async def resolve(self, kind: str, name: str) -> Credential | None:
        """Return a Credential for `(kind, name)` or None if absent."""
        ...


@dataclass(slots=True)
class CredentialBroker:
    """Priority-ordered credential resolution facade.

    Resolvers passed to the constructor are sorted once by descending
    `priority`. `register_resolver` keeps that invariant; broker is
    expected to be configured at process startup and rarely mutated, so
    sort-on-register is acceptable.
    """

    _resolvers: list[CredentialResolver] = field(default_factory=list)

    def __init__(self, resolvers: Sequence[CredentialResolver] = ()) -> None:
        self._resolvers = []
        for r in resolvers:
            self.register_resolver(r)

    def register_resolver(self, r: CredentialResolver) -> None:
        """Add a resolver and re-sort the chain by descending priority."""
        self._resolvers.append(r)
        # Stable sort so ties retain registration order (caller-controlled).
        self._resolvers.sort(key=lambda x: x.priority, reverse=True)

    @property
    def resolvers(self) -> tuple[CredentialResolver, ...]:
        """Snapshot of the resolver chain in resolution order."""
        return tuple(self._resolvers)

    async def get(self, kind: str, name: str = "default") -> Credential:
        """Resolve `(kind, name)` against the configured chain.

        Raises CredentialNotFoundError if every resolver returns None.
        Resolver-raised CredentialMisconfiguredError propagates so a
        broken file is not silently downgraded to "missing".
        """
        attempted: list[str] = []
        for r in self._resolvers:
            attempted.append(r.name)
            cred = await r.resolve(kind, name)
            if cred is not None:
                return cred
        raise CredentialNotFoundError(
            f"no credential for kind={kind!r} name={name!r}; "
            f"tried resolvers={attempted}"
        )

    async def get_for_profile(self, profile: "CredentialProfile") -> Credential:
        """Resolve the base of `profile`, then apply the assume-role chain.

        Imported lazily to avoid the broker module depending on profile
        at import time (profile depends on broker for plugin hooks).
        """
        from pleno_pii_scanner.credentials.profile import apply_chain

        return await apply_chain(self, profile)
