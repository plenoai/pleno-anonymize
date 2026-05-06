"""Tests for FileCredentialResolver (TOML)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pleno_pii_scanner.credentials import (
    CredentialMisconfiguredError,
    FileCredentialResolver,
)
from pleno_pii_scanner.credentials.resolvers.file import default_credentials_path


class TestDefaultCredentialsPath:
    def test_xdg_config_home_honored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert default_credentials_path() == tmp_path / "pleno" / "credentials.toml"

    def test_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert (
            default_credentials_path()
            == tmp_path / ".config" / "pleno" / "credentials.toml"
        )


class TestFileCredentialResolver:
    async def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        r = FileCredentialResolver(path=tmp_path / "nonexistent.toml")
        assert await r.resolve("github-pat", "default") is None

    async def test_simple_github_pat(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text(
            '[github.default]\nkind = "github-pat"\ntoken = "ghp_xxx"\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        cred = await r.resolve("github-pat", "default")
        assert cred is not None
        assert cred.kind == "github-pat"
        assert cred.payload == {"token": "ghp_xxx"}
        assert cred.source.startswith("file:")
        assert "github.default" in cred.source
        # Secret never appears in repr.
        assert "ghp_xxx" not in repr(cred)

    async def test_aws_iam_with_extra_fields(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text(
            "[aws.prod]\n"
            'kind = "aws-iam"\n'
            'access_key_id = "AKIA..."\n'
            'secret_access_key = "wJa..."\n'
            'region = "us-east-1"\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        cred = await r.resolve("aws-iam", "prod")
        assert cred is not None
        assert cred.payload["access_key_id"] == "AKIA..."
        assert cred.payload["secret_access_key"] == "wJa..."
        assert cred.payload["region"] == "us-east-1"

    async def test_private_key_path_expansion(self, tmp_path: Path) -> None:
        pem_path = tmp_path / "github-app.pem"
        pem_path.write_text(
            "-----BEGIN PRIVATE KEY-----\nDATA\n-----END-----\n", encoding="utf-8"
        )
        f = tmp_path / "creds.toml"
        f.write_text(
            f"[github.work]\n"
            f'kind = "github-app"\n'
            f"app_id = 12345\n"
            f"installation_id = 678\n"
            f'private_key_path = "{pem_path}"\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        cred = await r.resolve("github-app", "work")
        assert cred is not None
        assert cred.payload["app_id"] == 12345
        assert cred.payload["installation_id"] == 678
        assert "BEGIN PRIVATE KEY" in str(cred.payload["private_key"])
        assert "private_key_path" not in cred.payload

    async def test_private_key_path_unreadable(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text(
            "[github.work]\n"
            'kind = "github-app"\n'
            "app_id = 1\n"
            "installation_id = 1\n"
            'private_key_path = "/nonexistent/path/key.pem"\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        with pytest.raises(CredentialMisconfiguredError, match="private_key_path"):
            await r.resolve("github-app", "work")

    async def test_broken_toml_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text("this is not = = valid toml [[[", encoding="utf-8")
        r = FileCredentialResolver(path=f)
        with pytest.raises(CredentialMisconfiguredError, match="not valid TOML"):
            await r.resolve("github-pat", "default")

    async def test_missing_kind_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text('[github.default]\ntoken = "x"\n', encoding="utf-8")
        r = FileCredentialResolver(path=f)
        with pytest.raises(
            CredentialMisconfiguredError, match="missing required `kind`"
        ):
            await r.resolve("github-pat", "default")

    async def test_kind_must_be_string(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text('[github.default]\nkind = 123\ntoken = "x"\n', encoding="utf-8")
        r = FileCredentialResolver(path=f)
        with pytest.raises(CredentialMisconfiguredError, match="kind must be a string"):
            await r.resolve("github-pat", "default")

    async def test_wrong_kind_keeps_searching(self, tmp_path: Path) -> None:
        # A name that exists under family but with a different declared kind
        # must not match — the broker would otherwise see github-app where
        # github-pat was requested.
        f = tmp_path / "creds.toml"
        f.write_text(
            '[github.default]\nkind = "github-app"\napp_id = 1\ninstallation_id = 1\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        assert await r.resolve("github-pat", "default") is None

    async def test_missing_section_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text(
            '[aws.prod]\nkind = "aws-iam"\naccess_key_id = "x"\nsecret_access_key = "y"\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        assert await r.resolve("github-pat", "default") is None

    async def test_missing_name_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text(
            '[github.work]\nkind = "github-pat"\ntoken = "x"\n', encoding="utf-8"
        )
        r = FileCredentialResolver(path=f)
        assert await r.resolve("github-pat", "missing-name") is None

    async def test_section_must_be_table(self, tmp_path: Path) -> None:
        # Top-level scalar where a table is expected: silently skipped
        # because the user might have unrelated TOML mixed in. (We surface
        # broken TOML loudly, but well-formed-but-unrelated keys are fine.)
        f = tmp_path / "creds.toml"
        f.write_text('github = "not a table"\n', encoding="utf-8")
        r = FileCredentialResolver(path=f)
        assert await r.resolve("github-pat", "default") is None

    async def test_entry_must_be_table(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text('[github]\ndefault = "not a table"\n', encoding="utf-8")
        r = FileCredentialResolver(path=f)
        assert await r.resolve("github-pat", "default") is None

    async def test_sops_marker_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text(
            '[sops]\nlastmodified = "2026-01-01"\n[github.default]\nkind = "github-pat"\ntoken = "x"\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        with pytest.raises(NotImplementedError, match="SOPS"):
            await r.resolve("github-pat", "default")

    async def test_encrypted_marker_raises(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text("encrypted = true\n", encoding="utf-8")
        r = FileCredentialResolver(path=f)
        with pytest.raises(NotImplementedError, match="SOPS"):
            await r.resolve("github-pat", "default")

    async def test_default_path_used_when_explicit_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Set XDG so default path lands inside tmp_path; create credential there.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        creds = tmp_path / "pleno" / "credentials.toml"
        creds.parent.mkdir(parents=True)
        creds.write_text(
            '[github.default]\nkind = "github-pat"\ntoken = "x"\n', encoding="utf-8"
        )
        r = FileCredentialResolver()
        cred = await r.resolve("github-pat", "default")
        assert cred is not None

    async def test_load_caches(self, tmp_path: Path) -> None:
        f = tmp_path / "creds.toml"
        f.write_text(
            '[github.default]\nkind = "github-pat"\ntoken = "x"\n', encoding="utf-8"
        )
        r = FileCredentialResolver(path=f)
        await r.resolve("github-pat", "default")
        # Mutate file: cached resolver should still return the original.
        f.write_text(
            '[github.default]\nkind = "github-pat"\ntoken = "rotated"\n',
            encoding="utf-8",
        )
        cred = await r.resolve("github-pat", "default")
        assert cred is not None
        assert cred.payload["token"] == "x"

    async def test_exact_kind_lookup_used_when_family_misses(
        self, tmp_path: Path
    ) -> None:
        # When the TOML uses [github-pat.default] form rather than the
        # family form, exact-kind lookup must still find it.
        f = tmp_path / "creds.toml"
        f.write_text(
            '[github-pat.default]\nkind = "github-pat"\ntoken = "exact"\n',
            encoding="utf-8",
        )
        r = FileCredentialResolver(path=f)
        cred = await r.resolve("github-pat", "default")
        assert cred is not None
        assert cred.payload["token"] == "exact"

    def test_priority_default(self) -> None:
        r = FileCredentialResolver()
        assert r.priority == 100

    def test_priority_override(self) -> None:
        r = FileCredentialResolver(priority=42)
        assert r.priority == 42

    def test_name(self) -> None:
        r = FileCredentialResolver()
        assert r.name == "file"
