"""Tests for EnvCredentialResolver."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.credentials import (
    CredentialMisconfiguredError,
    EnvCredentialResolver,
)


@pytest.fixture(autouse=True)
def _clear_pleno_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every PLENO_* env var so tests start from a clean slate."""
    import os

    for k in list(os.environ):
        if k.startswith("PLENO_"):
            monkeypatch.delenv(k, raising=False)


class TestEnvCredentialResolver:
    async def test_unknown_kind_returns_none(self) -> None:
        r = EnvCredentialResolver()
        assert await r.resolve("unknown-kind", "default") is None

    async def test_github_pat_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_GITHUB_PAT_TOKEN", "ghp_xxx")
        r = EnvCredentialResolver()
        cred = await r.resolve("github-pat", "default")
        assert cred is not None
        assert cred.kind == "github-pat"
        assert cred.payload == {"token": "ghp_xxx"}
        assert "ghp_xxx" not in repr(cred)

    async def test_github_pat_legacy_token_form(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Backwards-compat: PLENO_<KIND>_TOKEN with no _NAME_ segment.
        monkeypatch.setenv("PLENO_GITHUB_PAT_TOKEN", "ghp_legacy")
        r = EnvCredentialResolver()
        cred = await r.resolve("github-pat", "default")
        assert cred is not None
        assert cred.payload == {"token": "ghp_legacy"}

    async def test_github_pat_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_GITHUB_PAT_WORK_TOKEN", "ghp_work")
        r = EnvCredentialResolver()
        cred = await r.resolve("github-pat", "work")
        assert cred is not None
        assert cred.payload == {"token": "ghp_work"}

    async def test_aws_iam_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_AWS_IAM_PROD_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setenv("PLENO_AWS_IAM_PROD_SECRET_ACCESS_KEY", "wJa")
        monkeypatch.setenv("PLENO_AWS_IAM_PROD_SESSION_TOKEN", "FwoG")
        monkeypatch.setenv("PLENO_AWS_IAM_PROD_REGION", "us-west-2")
        r = EnvCredentialResolver()
        cred = await r.resolve("aws-iam", "prod")
        assert cred is not None
        assert cred.payload["access_key_id"] == "AKIA"
        assert cred.payload["secret_access_key"] == "wJa"
        assert cred.payload["session_token"] == "FwoG"
        assert cred.payload["region"] == "us-west-2"

    async def test_aws_iam_partial_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Missing secret half: resolver yields None so the chain falls
        # through to the next resolver (file / keyring) instead of
        # synthesizing a half-credential.
        monkeypatch.setenv("PLENO_AWS_IAM_DEFAULT_ACCESS_KEY_ID", "AKIA")
        r = EnvCredentialResolver()
        assert await r.resolve("aws-iam", "default") is None

    async def test_empty_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_GITHUB_PAT_TOKEN", "")
        r = EnvCredentialResolver()
        with pytest.raises(CredentialMisconfiguredError, match="empty"):
            await r.resolve("github-pat", "default")

    async def test_hyphen_in_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_AWS_IAM_MY_TENANT_ACCESS_KEY_ID", "AKIA")
        monkeypatch.setenv("PLENO_AWS_IAM_MY_TENANT_SECRET_ACCESS_KEY", "wJa")
        r = EnvCredentialResolver()
        cred = await r.resolve("aws-iam", "my-tenant")
        assert cred is not None

    async def test_github_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_GITHUB_APP_APP_ID", "12345")
        monkeypatch.setenv("PLENO_GITHUB_APP_INSTALLATION_ID", "678")
        monkeypatch.setenv("PLENO_GITHUB_APP_PRIVATE_KEY", "-----BEGIN-----")
        monkeypatch.setenv("PLENO_GITHUB_APP_BASE_URL", "https://ghe.example/api/v3")
        r = EnvCredentialResolver()
        cred = await r.resolve("github-app", "default")
        assert cred is not None
        assert cred.payload["app_id"] == "12345"
        assert cred.payload["installation_id"] == "678"
        assert cred.payload["base_url"] == "https://ghe.example/api/v3"
        assert "BEGIN" not in repr(cred)

    async def test_aws_oidc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # name="default" compresses to PLENO_<KIND>_<FIELD> (no _DEFAULT_).
        monkeypatch.setenv("PLENO_AWS_OIDC_ROLE_ARN", "arn:aws:iam::1:role/x")
        monkeypatch.setenv("PLENO_AWS_OIDC_EXTERNAL_ID", "ext-id")
        r = EnvCredentialResolver()
        cred = await r.resolve("aws-oidc", "default")
        assert cred is not None
        assert cred.payload["role_arn"] == "arn:aws:iam::1:role/x"
        assert cred.payload["external_id"] == "ext-id"

    async def test_slack_bot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_SLACK_BOT_TOKEN", "xoxb-secret")
        r = EnvCredentialResolver()
        cred = await r.resolve("slack-bot", "default")
        assert cred is not None
        assert cred.payload == {"token": "xoxb-secret"}
        assert "xoxb-secret" not in repr(cred)

    async def test_gcp_sa_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_GCP_SA_KEY_CLIENT_EMAIL", "scanner@proj.iam.gserviceaccount.com")
        monkeypatch.setenv("PLENO_GCP_SA_KEY_PRIVATE_KEY", "-----BEGIN-----")
        r = EnvCredentialResolver()
        cred = await r.resolve("gcp-sa-key", "default")
        assert cred is not None
        assert cred.payload["client_email"].endswith(".gserviceaccount.com")

    async def test_azure_sp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_AZURE_SP_TENANT_ID", "tid")
        monkeypatch.setenv("PLENO_AZURE_SP_CLIENT_ID", "cid")
        monkeypatch.setenv("PLENO_AZURE_SP_CLIENT_SECRET", "csecret")
        r = EnvCredentialResolver()
        cred = await r.resolve("azure-sp", "default")
        assert cred is not None
        assert cred.payload["tenant_id"] == "tid"
        assert "csecret" not in repr(cred)

    async def test_bitbucket_app_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PLENO_BITBUCKET_APP_PASSWORD_USERNAME", "alice")
        monkeypatch.setenv("PLENO_BITBUCKET_APP_PASSWORD_APP_PASSWORD", "secret")
        r = EnvCredentialResolver()
        cred = await r.resolve("bitbucket-app-password", "default")
        assert cred is not None
        assert cred.payload["username"] == "alice"
        assert "secret" not in repr(cred)

    async def test_no_env_returns_none(self) -> None:
        r = EnvCredentialResolver()
        assert await r.resolve("github-pat", "default") is None

    def test_priority_default(self) -> None:
        r = EnvCredentialResolver()
        assert r.priority == 80

    def test_priority_override(self) -> None:
        r = EnvCredentialResolver(priority=99)
        assert r.priority == 99

    def test_name(self) -> None:
        r = EnvCredentialResolver()
        assert r.name == "env"
