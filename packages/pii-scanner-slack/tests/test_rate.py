"""Tests for the SlackApiError → RateLimited bridge."""

from __future__ import annotations

import pytest
from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from slack_sdk.errors import SlackApiError

from pleno_pii_scanner_slack import _rate

from .conftest import FakeResponse


def _make_429(retry_after: str | None = "12") -> SlackApiError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    response = FakeResponse({"ok": False, "error": "ratelimited"}, status_code=429, headers=headers)
    return SlackApiError("rate", response)


class TestRaiseFromSlackApiError:
    def test_translates_429(self) -> None:
        with pytest.raises(RateLimited, match="Retry-After=12"):
            _rate.raise_from_slack_api_error(_make_429("12"))

    def test_translates_ratelimited_string(self) -> None:
        # Older Slack APIs return 200 + body error="ratelimited" instead
        # of 429. Both must surface as RateLimited.
        response = FakeResponse(
            {"ok": False, "error": "ratelimited"}, status_code=200
        )
        exc = SlackApiError("rate", response)
        with pytest.raises(RateLimited):
            _rate.raise_from_slack_api_error(exc)

    def test_no_retry_after_header(self) -> None:
        with pytest.raises(RateLimited, match="no Retry-After"):
            _rate.raise_from_slack_api_error(_make_429(None))

    def test_invalid_retry_after_treated_as_missing(self) -> None:
        with pytest.raises(RateLimited, match="no Retry-After"):
            _rate.raise_from_slack_api_error(_make_429("not-a-number"))

    def test_non_rate_limit_does_nothing(self) -> None:
        response = FakeResponse(
            {"ok": False, "error": "channel_not_found"}, status_code=200
        )
        exc = SlackApiError("missing", response)
        # Must return without raising — caller handles non-rate-limit errors.
        _rate.raise_from_slack_api_error(exc)

    def test_no_response_attribute(self) -> None:
        exc = SlackApiError("orphan", None)
        # Defensive: no response means no signal to translate.
        _rate.raise_from_slack_api_error(exc)

    def test_response_without_get_attribute(self) -> None:
        # An exotic SDK shape where `response` is a bare object without
        # the Mapping-like `.get`. Must not crash; falls through.
        class BareResponse:
            status_code = 500

        exc = SlackApiError("weird", BareResponse())
        _rate.raise_from_slack_api_error(exc)

    def test_response_with_non_string_error(self) -> None:
        # `error` could be None in a malformed response — exercise the
        # `isinstance(candidate, str)` branch where it's False.
        response = FakeResponse({"ok": False, "error": None}, status_code=200)
        exc = SlackApiError("nope", response)
        _rate.raise_from_slack_api_error(exc)


class TestTranslateSlackErrors:
    async def test_passes_through_success(self) -> None:
        async with _rate.translate_slack_errors():
            value = 1 + 1
        assert value == 2

    async def test_translates_429_in_block(self) -> None:
        with pytest.raises(RateLimited):
            async with _rate.translate_slack_errors():
                raise _make_429("5")

    async def test_re_raises_non_rate_limit(self) -> None:
        response = FakeResponse(
            {"ok": False, "error": "not_in_channel"}, status_code=200
        )
        exc = SlackApiError("nope", response)
        with pytest.raises(SlackApiError):
            async with _rate.translate_slack_errors():
                raise exc
