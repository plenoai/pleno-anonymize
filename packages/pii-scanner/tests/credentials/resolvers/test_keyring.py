"""Tests for KeyringCredentialResolver, including no-op fallback."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.credentials import (
    CredentialMisconfiguredError,
    KeyringCredentialResolver,
)
from pleno_pii_scanner.credentials.resolvers.keyring import SERVICE_NAME


class FakeBackend:
    def __init__(self, store: dict[tuple[str, str], str] | None = None, raises: Exception | None = None) -> None:
        self.store = store or {}
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def get_password(self, service: str, username: str) -> str | None:
        self.calls.append((service, username))
        if self.raises is not None:
            raise self.raises
        return self.store.get((service, username))


class TestKeyringResolverNoOp:
    async def test_no_keyring_lib_returns_none(self) -> None:
        r = KeyringCredentialResolver(backend_loader=lambda: None)
        assert r.available is False
        assert await r.resolve("github-pat", "default") is None

    async def test_explicit_backend_overrides_loader(self) -> None:
        backend = FakeBackend(store={(SERVICE_NAME, "github-pat:default"): "ghp_xxx"})
        r = KeyringCredentialResolver(backend=backend, backend_loader=lambda: None)
        assert r.available is True
        cred = await r.resolve("github-pat", "default")
        assert cred is not None


class TestKeyringResolverWithBackend:
    async def test_bare_token(self) -> None:
        backend = FakeBackend(store={(SERVICE_NAME, "github-pat:default"): "ghp_secret"})
        r = KeyringCredentialResolver(backend=backend)
        cred = await r.resolve("github-pat", "default")
        assert cred is not None
        assert cred.kind == "github-pat"
        assert cred.payload == {"token": "ghp_secret"}
        assert cred.source == f"keyring:{SERVICE_NAME}/github-pat:default"
        assert "ghp_secret" not in repr(cred)

    async def test_json_payload(self) -> None:
        backend = FakeBackend(
            store={
                (SERVICE_NAME, "aws-iam:prod"): '{"access_key_id":"AKIA","secret_access_key":"wJa","region":"us-east-1"}'
            }
        )
        r = KeyringCredentialResolver(backend=backend)
        cred = await r.resolve("aws-iam", "prod")
        assert cred is not None
        assert cred.payload["access_key_id"] == "AKIA"
        assert cred.payload["secret_access_key"] == "wJa"
        assert cred.payload["region"] == "us-east-1"
        assert "wJa" not in repr(cred)

    async def test_missing_entry_returns_none(self) -> None:
        backend = FakeBackend(store={})
        r = KeyringCredentialResolver(backend=backend)
        assert await r.resolve("github-pat", "default") is None

    async def test_invalid_json_raises(self) -> None:
        backend = FakeBackend(store={(SERVICE_NAME, "x:default"): "{not valid json"})
        r = KeyringCredentialResolver(backend=backend)
        with pytest.raises(CredentialMisconfiguredError, match="not valid JSON"):
            await r.resolve("x", "default")

    async def test_json_root_must_be_object(self) -> None:
        backend = FakeBackend(store={(SERVICE_NAME, "x:default"): "[1,2,3]"})
        r = KeyringCredentialResolver(backend=backend)
        with pytest.raises(CredentialMisconfiguredError, match="JSON root must be an object"):
            await r.resolve("x", "default")

    async def test_backend_exception_wraps(self) -> None:
        backend = FakeBackend(raises=RuntimeError("keychain locked"))
        r = KeyringCredentialResolver(backend=backend)
        with pytest.raises(CredentialMisconfiguredError, match="keychain locked"):
            await r.resolve("github-pat", "default")

    async def test_whitespace_around_bare_token(self) -> None:
        backend = FakeBackend(store={(SERVICE_NAME, "github-pat:default"): "  ghp_xxx  \n"})
        r = KeyringCredentialResolver(backend=backend)
        cred = await r.resolve("github-pat", "default")
        assert cred is not None
        assert cred.payload == {"token": "ghp_xxx"}

    async def test_username_format(self) -> None:
        backend = FakeBackend(store={(SERVICE_NAME, "aws-iam:prod"): "x"})
        r = KeyringCredentialResolver(backend=backend)
        await r.resolve("aws-iam", "prod")
        assert backend.calls == [(SERVICE_NAME, "aws-iam:prod")]

    def test_priority_default(self) -> None:
        r = KeyringCredentialResolver(backend_loader=lambda: None)
        assert r.priority == 60

    def test_priority_override(self) -> None:
        r = KeyringCredentialResolver(priority=10, backend_loader=lambda: None)
        assert r.priority == 10

    def test_name(self) -> None:
        r = KeyringCredentialResolver(backend_loader=lambda: None)
        assert r.name == "keyring"


class TestKeyringLibraryImport:
    """Real-library import path: covers the try/except in _try_import_keyring."""

    def test_real_loader_no_crash(self) -> None:
        # Whether the lib is installed or not, the loader must return
        # cleanly (Backend or None). Verifies the ImportError branch
        # does not propagate.
        from pleno_pii_scanner.credentials.resolvers.keyring import _try_import_keyring

        result = _try_import_keyring()
        assert result is None or hasattr(result, "get_password")

    def test_loader_returns_module_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inject a fake `keyring` module so the import-success branch
        # is exercised even when the real library is not installed.
        import sys
        import types

        fake = types.ModuleType("keyring")
        fake.get_password = lambda service, username: None  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "keyring", fake)
        from pleno_pii_scanner.credentials.resolvers.keyring import _try_import_keyring

        result = _try_import_keyring()
        assert result is fake
