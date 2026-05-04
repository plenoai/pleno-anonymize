"""Tests for the auth-mode classifier and header builder."""

from __future__ import annotations

import pytest

from pleno_pii_scanner_gitlab.auth import (
    GitlabAuthMode,
    InvalidGitlabAuthError,
    header_for,
    parse_auth_mode,
)


class TestParseAuthMode:
    def test_pat(self) -> None:
        assert parse_auth_mode("pat") is GitlabAuthMode.PAT

    def test_oauth(self) -> None:
        assert parse_auth_mode("oauth") is GitlabAuthMode.OAUTH

    def test_project(self) -> None:
        assert parse_auth_mode("project") is GitlabAuthMode.PROJECT

    def test_unknown_raises_invalid(self) -> None:
        with pytest.raises(InvalidGitlabAuthError, match="unsupported"):
            parse_auth_mode("bearer")

    def test_empty_raises(self) -> None:
        with pytest.raises(InvalidGitlabAuthError, match="unsupported"):
            parse_auth_mode("")

    def test_typo_raises(self) -> None:
        # `pats` is the most likely typo; we want a loud error not a fallback.
        with pytest.raises(InvalidGitlabAuthError):
            parse_auth_mode("pats")

    def test_invalid_subclass_of_value_error(self) -> None:
        # Registry factory paths catch ValueError; the subclassing
        # contract has to hold for that to keep working.
        assert issubclass(InvalidGitlabAuthError, ValueError)


class TestHeaderFor:
    def test_pat_uses_private_token(self) -> None:
        name, value = header_for(GitlabAuthMode.PAT, "glpat-x")
        assert name == "PRIVATE-TOKEN"
        assert value == "glpat-x"

    def test_project_uses_private_token_too(self) -> None:
        # Same wire format as PAT — scope is server-enforced.
        name, value = header_for(GitlabAuthMode.PROJECT, "glpat-proj")
        assert name == "PRIVATE-TOKEN"
        assert value == "glpat-proj"

    def test_oauth_uses_bearer(self) -> None:
        name, value = header_for(GitlabAuthMode.OAUTH, "abc")
        assert name == "Authorization"
        assert value == "Bearer abc"
