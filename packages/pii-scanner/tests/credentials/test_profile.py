"""Tests for CredentialProfile + assume-role hop chain."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.credentials import (
    AssumeRoleHop,
    Credential,
    CredentialBroker,
    CredentialProfile,
    apply_chain,
    register_hop_plugin,
    registered_hop_providers,
    unregister_hop_plugin,
)


class StaticResolver:
    def __init__(self, kind: str, name: str, cred: Credential) -> None:
        self.name = "static"
        self.priority = 10
        self._match = (kind, name)
        self._cred = cred

    async def resolve(self, kind: str, name: str) -> Credential | None:
        if (kind, name) == self._match:
            return self._cred
        return None


@pytest.fixture(autouse=True)
def _clean_plugins() -> None:
    """Each test starts with a known empty plugin registry."""
    for p in list(registered_hop_providers()):
        unregister_hop_plugin(p)
    yield
    for p in list(registered_hop_providers()):
        unregister_hop_plugin(p)


class TestAssumeRoleHop:
    def test_defaults(self) -> None:
        h = AssumeRoleHop(provider="aws", role_arn_or_id="arn:aws:iam::1:role/x")
        assert h.external_id is None
        assert h.session_name == "pleno-pii-scanner"
        assert h.duration_seconds == 3600

    def test_frozen(self) -> None:
        h = AssumeRoleHop(provider="aws", role_arn_or_id="arn:")
        with pytest.raises((AttributeError, TypeError)):
            h.provider = "gcp"  # type: ignore[misc]


class TestCredentialProfile:
    def test_base_kind_name_default(self) -> None:
        p = CredentialProfile(name="p", base="aws-iam:prod")
        assert p.base_kind_name() == ("aws-iam", "prod")

    def test_base_kind_name_implicit_default(self) -> None:
        p = CredentialProfile(name="p", base="github-pat")
        assert p.base_kind_name() == ("github-pat", "default")

    def test_base_kind_name_empty_base(self) -> None:
        p = CredentialProfile(name="p", base="")
        with pytest.raises(ValueError, match="empty base"):
            p.base_kind_name()

    def test_base_kind_name_missing_kind(self) -> None:
        p = CredentialProfile(name="p", base=":default")
        with pytest.raises(ValueError, match="missing kind"):
            p.base_kind_name()

    def test_chain_default_empty(self) -> None:
        p = CredentialProfile(name="p", base="aws-iam:default")
        assert p.chain == ()

    def test_frozen(self) -> None:
        p = CredentialProfile(name="p", base="aws-iam:default")
        with pytest.raises((AttributeError, TypeError)):
            p.name = "other"  # type: ignore[misc]


class TestApplyChain:
    async def test_no_chain_returns_base(self) -> None:
        base = Credential(
            kind="aws-iam", payload={"access_key_id": "x", "secret_access_key": "y"}
        )
        broker = CredentialBroker([StaticResolver("aws-iam", "default", base)])
        profile = CredentialProfile(name="p", base="aws-iam:default")
        got = await apply_chain(broker, profile)
        assert got is base

    async def test_missing_plugin_raises(self) -> None:
        base = Credential(
            kind="aws-iam", payload={"access_key_id": "x", "secret_access_key": "y"}
        )
        broker = CredentialBroker([StaticResolver("aws-iam", "default", base)])
        profile = CredentialProfile(
            name="p",
            base="aws-iam:default",
            chain=(
                AssumeRoleHop(provider="aws", role_arn_or_id="arn:aws:iam::1:role/x"),
            ),
        )
        with pytest.raises(NotImplementedError, match="pleno-pii-scanner-aws"):
            await apply_chain(broker, profile)

    async def test_plugin_invocation(self) -> None:
        base = Credential(
            kind="aws-iam", payload={"access_key_id": "x", "secret_access_key": "y"}
        )
        broker = CredentialBroker([StaticResolver("aws-iam", "default", base)])
        captured: list[tuple[Credential, AssumeRoleHop]] = []

        async def fake_aws(cred: Credential, hop: AssumeRoleHop) -> Credential:
            captured.append((cred, hop))
            return Credential(
                kind="aws-iam",
                payload={"access_key_id": "STS-id", "secret_access_key": "STS-secret"},
                source=f"sts:{hop.role_arn_or_id}",
            )

        register_hop_plugin("aws", fake_aws)
        hop = AssumeRoleHop(
            provider="aws", role_arn_or_id="arn:aws:iam::1:role/x", external_id="ext"
        )
        profile = CredentialProfile(name="p", base="aws-iam:default", chain=(hop,))
        result = await apply_chain(broker, profile)
        assert result.payload["access_key_id"] == "STS-id"
        assert result.source == "sts:arn:aws:iam::1:role/x"
        assert captured == [(base, hop)]

    async def test_plugin_chain_walks_in_order(self) -> None:
        base = Credential(
            kind="aws-iam",
            payload={"access_key_id": "base", "secret_access_key": "base"},
        )
        broker = CredentialBroker([StaticResolver("aws-iam", "default", base)])

        async def aws(cred: Credential, hop: AssumeRoleHop) -> Credential:
            previous = cred.payload.get("access_key_id")
            return Credential(
                kind="aws-iam",
                payload={
                    "access_key_id": f"{previous}->{hop.role_arn_or_id}",
                    "secret_access_key": "rotated",
                },
            )

        register_hop_plugin("aws", aws)
        profile = CredentialProfile(
            name="p",
            base="aws-iam:default",
            chain=(
                AssumeRoleHop(provider="aws", role_arn_or_id="role-A"),
                AssumeRoleHop(provider="aws", role_arn_or_id="role-B"),
            ),
        )
        result = await apply_chain(broker, profile)
        assert result.payload["access_key_id"] == "base->role-A->role-B"

    async def test_register_hop_plugin_replaces(self) -> None:
        base = Credential(
            kind="aws-iam", payload={"access_key_id": "x", "secret_access_key": "y"}
        )
        broker = CredentialBroker([StaticResolver("aws-iam", "default", base)])

        async def first(cred: Credential, hop: AssumeRoleHop) -> Credential:
            return Credential(
                kind="aws-iam",
                payload={"access_key_id": "first", "secret_access_key": "x"},
            )

        async def second(cred: Credential, hop: AssumeRoleHop) -> Credential:
            return Credential(
                kind="aws-iam",
                payload={"access_key_id": "second", "secret_access_key": "x"},
            )

        register_hop_plugin("aws", first)
        register_hop_plugin("aws", second)
        assert registered_hop_providers() == ("aws",)
        profile = CredentialProfile(
            name="p",
            base="aws-iam:default",
            chain=(AssumeRoleHop(provider="aws", role_arn_or_id="r"),),
        )
        result = await apply_chain(broker, profile)
        assert result.payload["access_key_id"] == "second"

    def test_unregister_hop_plugin_no_op_for_unknown(self) -> None:
        unregister_hop_plugin("nonexistent")
        assert registered_hop_providers() == ()

    async def test_broker_get_for_profile_uses_apply_chain(self) -> None:
        base = Credential(
            kind="aws-iam", payload={"access_key_id": "x", "secret_access_key": "y"}
        )
        broker = CredentialBroker([StaticResolver("aws-iam", "default", base)])

        async def aws(cred: Credential, hop: AssumeRoleHop) -> Credential:
            return Credential(
                kind="aws-iam",
                payload={"access_key_id": "STS", "secret_access_key": "STS"},
            )

        register_hop_plugin("aws", aws)
        profile = CredentialProfile(
            name="p",
            base="aws-iam:default",
            chain=(AssumeRoleHop(provider="aws", role_arn_or_id="r"),),
        )
        result = await broker.get_for_profile(profile)
        assert result.payload["access_key_id"] == "STS"
