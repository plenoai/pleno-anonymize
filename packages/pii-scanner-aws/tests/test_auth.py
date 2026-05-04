"""Unit tests for the AWS auth + assume-role chain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pleno_pii_scanner.credentials.profile import AssumeRoleHop, CredentialProfile

from pleno_pii_scanner_aws.auth import (
    AccountSpec,
    AwsBaseIdentity,
    AwsCredentials,
    AwsSessionFactory,
    StubHopRunner,
)


class TestAwsBaseIdentity:
    def test_default_construction(self) -> None:
        b = AwsBaseIdentity()
        assert b.region == "us-east-1"
        assert b.access_key_id is None

    def test_explicit_keys_pair(self) -> None:
        b = AwsBaseIdentity(access_key_id="AKIA", secret_access_key="secret")
        assert b.access_key_id == "AKIA"

    def test_only_one_mode_allowed(self) -> None:
        with pytest.raises(ValueError, match="at most one"):
            AwsBaseIdentity(
                access_key_id="A",
                secret_access_key="B",
                profile_name="dev",
            )

    def test_keys_must_be_paired(self) -> None:
        with pytest.raises(ValueError, match="provided together"):
            AwsBaseIdentity(access_key_id="A")
        with pytest.raises(ValueError, match="provided together"):
            AwsBaseIdentity(secret_access_key="B")

    def test_web_identity_must_be_paired(self) -> None:
        with pytest.raises(ValueError, match="provided together"):
            AwsBaseIdentity(web_identity_token_file="/tmp/t")
        with pytest.raises(ValueError, match="provided together"):
            AwsBaseIdentity(role_arn="arn:aws:iam::123:role/R")

    def test_web_identity_pair_valid(self) -> None:
        b = AwsBaseIdentity(
            web_identity_token_file="/tmp/jwt", role_arn="arn:aws:iam::1:role/R"
        )
        assert b.role_arn.endswith(":role/R")


class TestAwsCredentials:
    def test_no_expiry_never_expires(self) -> None:
        c = AwsCredentials(access_key_id="A", secret_access_key="S")
        assert c.is_expired() is False
        assert c.is_expired(datetime.now(UTC) + timedelta(days=365)) is False

    def test_expired_within_safety_margin(self) -> None:
        soon = datetime.now(UTC) + timedelta(seconds=5)
        c = AwsCredentials(access_key_id="A", secret_access_key="S", expires_at=soon)
        assert c.is_expired() is True

    def test_not_expired_with_headroom(self) -> None:
        later = datetime.now(UTC) + timedelta(minutes=30)
        c = AwsCredentials(access_key_id="A", secret_access_key="S", expires_at=later)
        assert c.is_expired() is False

    def test_to_session_kwargs_omits_token(self) -> None:
        c = AwsCredentials(access_key_id="A", secret_access_key="S", region="us-west-2")
        kw = c.to_session_kwargs()
        assert kw["aws_access_key_id"] == "A"
        assert kw["aws_secret_access_key"] == "S"
        assert kw["region_name"] == "us-west-2"
        assert "aws_session_token" not in kw

    def test_to_session_kwargs_includes_token(self) -> None:
        c = AwsCredentials(
            access_key_id="A", secret_access_key="S", session_token="tok"
        )
        assert c.to_session_kwargs()["aws_session_token"] == "tok"


class TestAccountSpec:
    def test_resolved_label_default(self) -> None:
        a = AccountSpec(account_id="123456789012")
        assert a.resolved_label() == "aws:123456789012"

    def test_resolved_label_explicit(self) -> None:
        a = AccountSpec(account_id="1", label="prod-us")
        assert a.resolved_label() == "prod-us"


def _seed_creds(name: str = "seed") -> AwsCredentials:
    return AwsCredentials(
        access_key_id=f"AK_{name}", secret_access_key=f"SK_{name}", region="us-east-1"
    )


def _hop_creds(name: str, exp_minutes: int = 60) -> AwsCredentials:
    return AwsCredentials(
        access_key_id=f"AK_{name}",
        secret_access_key=f"SK_{name}",
        session_token=f"ST_{name}",
        expires_at=datetime.now(UTC) + timedelta(minutes=exp_minutes),
        region="us-east-1",
    )


class TestAwsSessionFactory:
    async def test_no_chain_returns_seed(self) -> None:
        runner = StubHopRunner(seed=_seed_creds("base"))
        f = AwsSessionFactory(base=AwsBaseIdentity(), _hop_runner=runner)
        creds = await f.credentials_for(AccountSpec(account_id="111111111111"))
        assert creds.access_key_id == "AK_base"
        assert runner.calls == []

    async def test_account_chain_walks_hops(self) -> None:
        runner = StubHopRunner(
            seed=_seed_creds(),
            hops=[_hop_creds("transit"), _hop_creds("target")],
        )
        f = AwsSessionFactory(base=AwsBaseIdentity(), _hop_runner=runner)
        account = AccountSpec(
            account_id="222",
            chain=(
                AssumeRoleHop(provider="aws", role_arn_or_id="arn:transit"),
                AssumeRoleHop(provider="aws", role_arn_or_id="arn:target"),
            ),
        )
        creds = await f.credentials_for(account)
        assert creds.access_key_id == "AK_target"
        assert [h.role_arn_or_id for h, _ in runner.calls] == [
            "arn:transit",
            "arn:target",
        ]

    async def test_profile_chain_then_account_chain(self) -> None:
        # Profile-level chain runs first; account-level chain layers on top.
        runner = StubHopRunner(
            seed=_seed_creds(),
            hops=[_hop_creds("master"), _hop_creds("member")],
        )
        profile = CredentialProfile(
            name="org",
            base="aws:default",
            chain=(AssumeRoleHop(provider="aws", role_arn_or_id="arn:org-master"),),
        )
        f = AwsSessionFactory(
            base=AwsBaseIdentity(), profile=profile, _hop_runner=runner
        )
        account = AccountSpec(
            account_id="333",
            chain=(
                AssumeRoleHop(
                    provider="aws",
                    role_arn_or_id="arn:aws:iam::333:role/OrganizationAccountAccessRole",
                ),
            ),
        )
        creds = await f.credentials_for(account)
        assert creds.access_key_id == "AK_member"
        assert [h.role_arn_or_id for h, _ in runner.calls] == [
            "arn:org-master",
            "arn:aws:iam::333:role/OrganizationAccountAccessRole",
        ]

    async def test_credentials_cached_until_expiry(self) -> None:
        runner = StubHopRunner(
            seed=_seed_creds(),
            hops=[_hop_creds("first", exp_minutes=60)],
        )
        f = AwsSessionFactory(base=AwsBaseIdentity(), _hop_runner=runner)
        account = AccountSpec(
            account_id="444",
            chain=(AssumeRoleHop(provider="aws", role_arn_or_id="arn:r"),),
        )
        a = await f.credentials_for(account)
        b = await f.credentials_for(account)
        assert a is b  # cached
        assert len(runner.calls) == 1

    async def test_re_assume_when_expired(self) -> None:
        # First call returns near-expired creds → cache miss next time.
        runner = StubHopRunner(
            seed=_seed_creds(),
            hops=[
                AwsCredentials(
                    access_key_id="AK1",
                    secret_access_key="SK1",
                    session_token="t1",
                    expires_at=datetime.now(UTC) + timedelta(seconds=1),
                ),
                _hop_creds("fresh"),
            ],
        )
        f = AwsSessionFactory(base=AwsBaseIdentity(), _hop_runner=runner)
        account = AccountSpec(
            account_id="555",
            chain=(AssumeRoleHop(provider="aws", role_arn_or_id="arn:r"),),
        )
        a = await f.credentials_for(account)
        assert a.access_key_id == "AK1"
        b = await f.credentials_for(account)
        assert b.access_key_id == "AK_fresh"
        assert len(runner.calls) == 2

    async def test_stub_runner_under_baked_raises(self) -> None:
        runner = StubHopRunner(seed=_seed_creds())  # no hops
        f = AwsSessionFactory(base=AwsBaseIdentity(), _hop_runner=runner)
        account = AccountSpec(
            account_id="666",
            chain=(AssumeRoleHop(provider="aws", role_arn_or_id="arn:r"),),
        )
        with pytest.raises(AssertionError, match="under-baked"):
            await f.credentials_for(account)

    def test_base_session_explicit_keys(self) -> None:
        f = AwsSessionFactory(
            base=AwsBaseIdentity(
                access_key_id="A",
                secret_access_key="B",
                session_token="T",
                region="ap-northeast-1",
            )
        )
        s = f.base_session()
        # We do not depend on aioboto3 internals beyond construction not raising.
        assert s is not None

    def test_base_session_profile_name(self) -> None:
        # Construct with an unresolvable profile — aioboto3 builds the
        # session lazily but raises ProfileNotFound when credentials are
        # actually flushed. Either behaviour is acceptable for our
        # construction-only check; we just want the kwargs path covered.
        from botocore.exceptions import ProfileNotFound

        f = AwsSessionFactory(
            base=AwsBaseIdentity(profile_name="__nonexistent_profile__", region="us-west-2")
        )
        try:
            session = f.base_session()
        except ProfileNotFound:
            return
        assert session is not None

    def test_base_session_default_chain(self) -> None:
        f = AwsSessionFactory(base=AwsBaseIdentity())
        assert f.base_session() is not None


class TestRealHopRunnerGuards:
    async def test_rejects_non_aws_provider(self) -> None:
        from pleno_pii_scanner_aws.auth import _RealHopRunner

        r = _RealHopRunner()
        prev = AwsCredentials(access_key_id="A", secret_access_key="B")
        with pytest.raises(ValueError, match="provider='aws'"):
            await r.assume(
                prev,
                AssumeRoleHop(provider="gcp", role_arn_or_id="x"),
                "us-east-1",
            )


class TestRealHopRunnerStart:
    """Cover `_RealHopRunner.start` by stubbing the botocore session.

    Avoids running the actual aioboto3 default credential chain (which
    would touch the filesystem / env / IMDSv2 in CI).
    """

    async def test_start_returns_frozen_credentials(self, monkeypatch) -> None:
        from pleno_pii_scanner_aws import auth as auth_mod

        class FrozenStub:
            access_key = "AK"
            secret_key = "SK"
            token = "TOK"

        class CredsStub:
            def get_frozen_credentials(self):
                return FrozenStub()

        class SessionStub:
            class _session:  # noqa: N801 — attribute name from real aioboto3
                @staticmethod
                def get_credentials():
                    return CredsStub()

        runner = auth_mod._RealHopRunner()
        out = await runner.start(SessionStub(), "ap-northeast-1")
        assert out.access_key_id == "AK"
        assert out.secret_access_key == "SK"
        assert out.session_token == "TOK"
        assert out.region == "ap-northeast-1"


class TestRealHopRunnerAssume:
    """Cover `_RealHopRunner.assume` by replacing aioboto3.Session.

    The fake yields a fake STS client whose `assume_role` returns a
    pre-baked Credentials dict — exercises the parsing branches
    (datetime + ISO string) without touching AWS.
    """

    async def test_assume_with_datetime_expiry(self, monkeypatch) -> None:
        from pleno_pii_scanner_aws import auth as auth_mod

        recorded: dict[str, dict] = {}

        class StsClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def assume_role(self, **kwargs):
                recorded["params"] = kwargs
                return {
                    "Credentials": {
                        "AccessKeyId": "AK_h",
                        "SecretAccessKey": "SK_h",
                        "SessionToken": "ST_h",
                        "Expiration": datetime(2030, 1, 1, tzinfo=UTC),
                    }
                }

        class FakeSession:
            def __init__(self, **kwargs):
                recorded["session_kwargs"] = kwargs

            def client(self, name, region_name=None):
                recorded["client_name"] = name
                recorded["client_region"] = region_name
                return StsClient()

        monkeypatch.setattr(auth_mod.aioboto3, "Session", FakeSession)

        runner = auth_mod._RealHopRunner()
        prev = AwsCredentials(
            access_key_id="P", secret_access_key="P", session_token="t", region="us-east-1"
        )
        hop = AssumeRoleHop(
            provider="aws",
            role_arn_or_id="arn:aws:iam::333:role/X",
            external_id="ext-1",
            session_name="sess",
            duration_seconds=900,
        )
        out = await runner.assume(prev, hop, "us-west-2")
        assert out.access_key_id == "AK_h"
        assert out.session_token == "ST_h"
        assert out.region == "us-west-2"
        assert recorded["client_name"] == "sts"
        assert recorded["client_region"] == "us-west-2"
        assert recorded["params"]["RoleArn"].endswith(":role/X")
        assert recorded["params"]["ExternalId"] == "ext-1"
        assert recorded["params"]["DurationSeconds"] == 900

    async def test_assume_iso_string_expiry(self, monkeypatch) -> None:
        from pleno_pii_scanner_aws import auth as auth_mod

        class StsClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def assume_role(self, **kwargs):  # noqa: ARG002
                return {
                    "Credentials": {
                        "AccessKeyId": "AK",
                        "SecretAccessKey": "SK",
                        "SessionToken": "ST",
                        # Naive ISO string — branch under test.
                        "Expiration": "2030-06-30T12:00:00",
                    }
                }

        class FakeSession:
            def __init__(self, **kwargs):
                pass

            def client(self, name, region_name=None):  # noqa: ARG002
                return StsClient()

        monkeypatch.setattr(auth_mod.aioboto3, "Session", FakeSession)

        runner = auth_mod._RealHopRunner()
        out = await runner.assume(
            AwsCredentials(access_key_id="A", secret_access_key="B"),
            AssumeRoleHop(provider="aws", role_arn_or_id="arn:r"),
            "us-east-1",
        )
        assert out.expires_at is not None
        assert out.expires_at.tzinfo is UTC
