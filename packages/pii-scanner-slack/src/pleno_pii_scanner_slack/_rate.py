"""Slack 429 → RateLimited bridge.

Slack returns rate-limit signals in two shapes that we normalize into a
single `RateLimited` raise so the scheduler's AIMD bucket can throttle:

  * `SlackApiError` whose `response.status_code == 429` (Web API tier-1..4)
  * `SlackApiError` with `response["error"] == "ratelimited"` (legacy)

(httpx `429` from the file CDN download is translated separately at the
connector boundary because it doesn't carry a SlackApiError.)

The `Retry-After` header (when present) is attached to the RateLimited
exception so the scheduler can pick a smarter sleep than the default
AIMD wait. We never sleep here — that would block the connector and
defeat the whole point of surfacing the signal upward.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from slack_sdk.errors import SlackApiError


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Parse the `Retry-After` header to a float seconds value.

    Slack always returns integer seconds; the HTTP spec also allows an
    HTTP-date but Slack never sends that form. Returns None when the
    header is absent or unparseable so the caller can include a clear
    "no Retry-After" message in the surfaced exception.
    """
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_message(prefix: str, retry_after: float | None) -> str:
    if retry_after is None:
        return f"{prefix} (no Retry-After header)"
    return f"{prefix} (Retry-After={retry_after}s)"


def raise_from_slack_api_error(exc: SlackApiError) -> None:
    """Translate a SlackApiError into RateLimited if it represents a 429.

    Returns silently for non-rate-limit errors so callers can chain this
    inside an `except SlackApiError` block before re-raising.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return
    status = getattr(response, "status_code", None)
    error_str: str | None = None
    if hasattr(response, "get"):
        candidate = response.get("error")
        if isinstance(candidate, str):
            error_str = candidate
    if status == 429 or error_str == "ratelimited":
        headers = getattr(response, "headers", {}) or {}
        retry_after = _retry_after_seconds(headers)
        raise RateLimited(_format_message("slack rate limited", retry_after)) from exc


@contextlib.asynccontextmanager
async def translate_slack_errors() -> AsyncIterator[None]:
    """Async context manager that converts SDK 429s to RateLimited.

    Use around any Slack SDK call that may produce rate-limit pressure
    so the scheduler sees a uniform exception type. Non-rate-limit
    SlackApiError is re-raised unchanged for the caller to handle (e.g.
    `not_in_channel` for an archived private channel).
    """
    try:
        yield
    except SlackApiError as exc:
        raise_from_slack_api_error(exc)
        raise


__all__ = [
    "raise_from_slack_api_error",
    "translate_slack_errors",
]
