"""Tests for `tokens.classify_token`."""

from __future__ import annotations

import pytest

from pleno_pii_scanner_slack.tokens import (
    InvalidSlackTokenError,
    SlackTokenType,
    classify_token,
)


class TestClassifyToken:
    def test_xoxb_is_bot(self) -> None:
        assert classify_token("xoxb-1-2-3-deadbeef") is SlackTokenType.BOT

    def test_xoxp_is_user(self) -> None:
        assert classify_token("xoxp-1-2-3-deadbeef") is SlackTokenType.USER

    def test_xoxa_is_org(self) -> None:
        assert classify_token("xoxa-1234abcd") is SlackTokenType.ORG

    def test_xoxa2_is_org(self) -> None:
        # Slack ships both `xoxa-` and `xoxa2-` for org-wide tokens.
        assert classify_token("xoxa2-1-1-deadbeef") is SlackTokenType.ORG

    def test_xoxs_is_rejected(self) -> None:
        with pytest.raises(InvalidSlackTokenError, match="xoxa-"):
            classify_token("xoxs-legacy-token")

    def test_xoxe_is_rejected(self) -> None:
        with pytest.raises(InvalidSlackTokenError):
            classify_token("xoxe-refresh-token")

    def test_empty_is_rejected(self) -> None:
        with pytest.raises(InvalidSlackTokenError, match="empty"):
            classify_token("")

    def test_unknown_prefix_is_rejected(self) -> None:
        with pytest.raises(InvalidSlackTokenError, match="unsupported"):
            classify_token("Bearer abcdef")

    def test_error_does_not_echo_token(self) -> None:
        # The token must never appear in the error message — secret hygiene.
        secret = "xoxs-very-secret-do-not-leak"
        try:
            classify_token(secret)
        except InvalidSlackTokenError as exc:
            assert "very-secret" not in str(exc)
            return
        raise AssertionError("expected InvalidSlackTokenError")
