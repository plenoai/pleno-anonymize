"""Tests for CredentialBroker, Credential, masking, and resolver chain."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pleno_pii_scanner.credentials import (
    Credential,
    CredentialBroker,
    CredentialError,
    CredentialMisconfiguredError,
    CredentialNotFoundError,
    CredentialResolver,
    _is_secret_key,
)
from pleno_pii_scanner.credentials.broker import _mask_payload


class StaticResolver:
    """Test fixture: returns a fixed Credential for a fixed (kind, name)."""

    def __init__(
        self,
        name: str,
        priority: int,
        match: tuple[str, str] | None,
        cred: Credential | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.name = name
        self.priority = priority
        self._match = match
        self._cred = cred
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, kind: str, name: str) -> Credential | None:
        self.calls.append((kind, name))
        if self._raises is not None:
            raise self._raises
        if self._match is not None and (kind, name) == self._match:
            return self._cred
        return None


class TestIsSecretKey:
    @pytest.mark.parametrize(
        "key",
        [
            "token",
            "Token",
            "TOKEN",
            "secret",
            "client_secret",
            "password",
            "passwd",
            "private_key",
            "credential",
            "session_token",
            "cert",
            "certificate",
            "pem",
            "tls_pem",
        ],
    )
    def test_secret_keys(self, key: str) -> None:
        assert _is_secret_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "app_id",
            "installation_id",
            "tenant_id",
            "role_arn",
            "region",
            "username",
            "access_key_id",
            "key_id",
            "kid",
            "public_key",
            "workspace",
        ],
    )
    def test_non_secret_keys(self, key: str) -> None:
        assert _is_secret_key(key) is False


class TestMaskPayload:
    def test_masks_secret_keys_only(self) -> None:
        payload = {
            "token": "ghp_topsecret",
            "app_id": 12345,
            "private_key": "-----BEGIN-----",
            "region": "us-east-1",
        }
        masked = _mask_payload(payload)
        assert masked == {
            "token": "***",
            "app_id": 12345,
            "private_key": "***",
            "region": "us-east-1",
        }


class TestCredentialMasking:
    def test_repr_hides_token(self) -> None:
        c = Credential(
            kind="github-pat",
            payload={"token": "ghp_topsecret"},
            source="env:PLENO_GITHUB_TOKEN",
        )
        rendered = repr(c)
        assert "ghp_topsecret" not in rendered
        assert "***" in rendered
        assert "github-pat" in rendered
        assert "env:PLENO_GITHUB_TOKEN" in rendered

    def test_str_hides_token(self) -> None:
        c = Credential(kind="github-pat", payload={"token": "ghp_topsecret"})
        assert "ghp_topsecret" not in str(c)

    def test_fstring_repr_hides_token(self) -> None:
        c = Credential(kind="aws-iam", payload={"secret_access_key": "AKIA-secret"})
        out = f"{c!r}"
        assert "AKIA-secret" not in out
        assert "***" in out

    def test_repr_keeps_non_secret_metadata(self) -> None:
        c = Credential(
            kind="github-app",
            payload={
                "app_id": 12345,
                "installation_id": 678,
                "private_key": "-----BEGIN-----",
            },
        )
        out = repr(c)
        assert "12345" in out
        assert "678" in out
        assert "-----BEGIN-----" not in out

    def test_repr_includes_expires_at(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=UTC)
        c = Credential(kind="aws-oidc", payload={"token": "x"}, expires_at=ts)
        assert "2026" in repr(c)

    def test_credential_is_frozen(self) -> None:
        c = Credential(kind="github-pat", payload={"token": "x"})
        with pytest.raises((AttributeError, TypeError)):
            c.kind = "other"  # type: ignore[misc]


class TestCredentialBroker:
    async def test_returns_first_matching_resolver(self) -> None:
        target = Credential(kind="github-pat", payload={"token": "x"})
        r1 = StaticResolver("low", priority=10, match=None)
        r2 = StaticResolver("hit", priority=50, match=("github-pat", "default"), cred=target)
        r3 = StaticResolver("never", priority=5, match=("github-pat", "default"), cred=target)
        broker = CredentialBroker([r1, r2, r3])
        got = await broker.get("github-pat")
        assert got is target
        assert r2.calls == [("github-pat", "default")]
        assert r3.calls == []

    async def test_priority_order_descending(self) -> None:
        target_high = Credential(kind="aws-iam", payload={"token": "high"})
        target_low = Credential(kind="aws-iam", payload={"token": "low"})
        low = StaticResolver("low", priority=1, match=("aws-iam", "default"), cred=target_low)
        high = StaticResolver("high", priority=99, match=("aws-iam", "default"), cred=target_high)
        broker = CredentialBroker([low, high])
        got = await broker.get("aws-iam")
        assert got is target_high
        # Resolver ordering is exposed for the CLI's `credentials test` command.
        assert [r.name for r in broker.resolvers] == ["high", "low"]

    async def test_register_resolver_re_sorts(self) -> None:
        existing = Credential(kind="github-pat", payload={"token": "old"})
        r_old = StaticResolver("old", priority=10, match=("github-pat", "default"), cred=existing)
        broker = CredentialBroker([r_old])
        new_target = Credential(kind="github-pat", payload={"token": "new"})
        r_new = StaticResolver("new", priority=99, match=("github-pat", "default"), cred=new_target)
        broker.register_resolver(r_new)
        got = await broker.get("github-pat")
        assert got is new_target

    async def test_not_found_raises(self) -> None:
        r = StaticResolver("only", priority=10, match=None)
        broker = CredentialBroker([r])
        with pytest.raises(CredentialNotFoundError) as excinfo:
            await broker.get("github-pat", "missing")
        assert "github-pat" in str(excinfo.value)
        assert "missing" in str(excinfo.value)
        assert "only" in str(excinfo.value)

    async def test_not_found_with_no_resolvers(self) -> None:
        broker = CredentialBroker()
        with pytest.raises(CredentialNotFoundError):
            await broker.get("anything")

    async def test_misconfigured_propagates(self) -> None:
        r = StaticResolver(
            "broken",
            priority=10,
            match=None,
            raises=CredentialMisconfiguredError("bad TOML"),
        )
        broker = CredentialBroker([r])
        with pytest.raises(CredentialMisconfiguredError):
            await broker.get("github-pat")

    async def test_default_name_is_default(self) -> None:
        target = Credential(kind="slack-bot", payload={"token": "x"})
        r = StaticResolver("env", priority=10, match=("slack-bot", "default"), cred=target)
        broker = CredentialBroker([r])
        await broker.get("slack-bot")
        assert r.calls == [("slack-bot", "default")]

    async def test_explicit_name_passed_through(self) -> None:
        target = Credential(kind="aws-iam", payload={"token": "x"})
        r = StaticResolver("env", priority=10, match=("aws-iam", "prod"), cred=target)
        broker = CredentialBroker([r])
        await broker.get("aws-iam", "prod")
        assert r.calls == [("aws-iam", "prod")]

    def test_resolver_protocol_runtime_check(self) -> None:
        r = StaticResolver("x", priority=1, match=None)
        assert isinstance(r, CredentialResolver)

    def test_credential_error_hierarchy(self) -> None:
        assert issubclass(CredentialNotFoundError, CredentialError)
        assert issubclass(CredentialMisconfiguredError, CredentialError)

    async def test_get_for_profile_delegates(self) -> None:
        from pleno_pii_scanner.credentials import CredentialProfile

        target = Credential(kind="aws-iam", payload={"access_key_id": "x", "secret_access_key": "y"})
        r = StaticResolver("env", priority=10, match=("aws-iam", "default"), cred=target)
        broker = CredentialBroker([r])
        profile = CredentialProfile(name="p", base="aws-iam:default")
        got = await broker.get_for_profile(profile)
        assert got is target
