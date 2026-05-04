"""OS keyring credential resolver (macOS Keychain / Secret Service / Windows Cred Manager).

The `keyring` library is an optional extra so the core wheel does not
pull a GUI/dbus dependency on every install. When the library is
absent at import time the resolver downgrades to a no-op (all
`resolve()` calls return None). When present, secrets are fetched from
the platform keyring under the service name ``pleno-pii-scanner`` with
the username ``<kind>:<name>``.

Stored value is a JSON document whose keys become Credential.payload
fields. A bare string is also accepted and treated as ``{"token": ...}``
because that is how 90% of single-token providers (GitHub PAT, Slack
bot token) are stored in practice.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from pleno_pii_scanner.credentials.broker import (
    Credential,
    CredentialMisconfiguredError,
)

# Service name in OS keyring. Singular per process so all kind/name
# combinations live in one bucket the user can audit with
# `security dump-keychain` / `secret-tool` / `cmdkey`.
SERVICE_NAME = "pleno-pii-scanner"


class _KeyringBackend(Protocol):
    """Minimal subset of the `keyring` library API we depend on.

    Declared as Protocol so a test fixture can inject a fake without
    importing keyring, and so we have a single place that documents
    exactly which methods the resolver touches.
    """

    def get_password(self, service: str, username: str) -> str | None: ...


def _try_import_keyring() -> _KeyringBackend | None:
    # Wrapped so unit tests can monkeypatch this single function to
    # simulate "library not installed" / "library throws on import"
    # without touching sys.modules globally.
    try:
        import keyring as _keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    return _keyring


class KeyringCredentialResolver:
    """OS keyring CredentialResolver.

    Constructed eagerly so the no-op state is visible to callers via
    `available` (CLI `credentials test` reports it). The backend is
    captured at construction so tests can inject a fake; production
    code typically lets the constructor pick up the real library.
    """

    name = "keyring"

    def __init__(
        self,
        *,
        priority: int = 60,
        backend: _KeyringBackend | None = None,
        backend_loader: Callable[[], _KeyringBackend | None] = _try_import_keyring,
    ) -> None:
        self.priority = priority
        self._backend: _KeyringBackend | None = (
            backend if backend is not None else backend_loader()
        )

    @property
    def available(self) -> bool:
        """True if the keyring library is importable and ready to query."""
        return self._backend is not None

    async def resolve(self, kind: str, name: str) -> Credential | None:
        if self._backend is None:
            return None
        username = f"{kind}:{name}"
        try:
            raw = self._backend.get_password(SERVICE_NAME, username)
        except Exception as exc:
            # Common keyring exceptions are wrapped under
            # `keyring.errors.KeyringError`. We catch broadly so a
            # locked keychain (macOS prompt cancelled, dbus down)
            # surfaces as misconfigured rather than crashing the scan.
            raise CredentialMisconfiguredError(
                f"keyring backend failed for {username!r}: {exc}"
            ) from exc
        if raw is None:
            return None
        payload = self._parse(raw, username)
        return Credential(
            kind=kind,
            payload=payload,
            source=f"keyring:{SERVICE_NAME}/{username}",
        )

    def _parse(self, raw: str, username: str) -> dict[str, object]:
        stripped = raw.strip()
        # Bare token shortcut: anything that does not look like a JSON
        # container (object or array) is treated as the single secret
        # value. Avoids forcing users to write {"token": "ghp_xxx"} for
        # the common case while still parsing arrays so we can reject
        # them loudly (a JSON array root is operator error).
        if not stripped.startswith(("{", "[")):
            return {"token": stripped}
        try:
            obj: Any = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise CredentialMisconfiguredError(
                f"keyring entry {username!r} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise CredentialMisconfiguredError(
                f"keyring entry {username!r} JSON root must be an object, "
                f"got {type(obj).__name__}"
            )
        return obj
