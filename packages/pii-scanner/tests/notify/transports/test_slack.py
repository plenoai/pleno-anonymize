"""Tests for Slack incoming webhook transport via httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from pleno_pii_scanner.notify.base import RetryPolicy
from pleno_pii_scanner.notify.transports.slack import SlackWebhookNotifier
from .._helpers import make_batch, make_finding


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(initial_seconds=0.0, factor=1.0, max_seconds=0.0, jitter=0.0)


def test_slack_requires_webhook_url(monkeypatch):
    monkeypatch.delenv("PLENO_SLACK_WEBHOOK_URL", raising=False)
    with pytest.raises(ValueError):
        SlackWebhookNotifier()


def test_slack_reads_webhook_url_from_env(monkeypatch):
    monkeypatch.setenv("PLENO_SLACK_WEBHOOK_URL", "https://hooks.slack.com/x")
    n = SlackWebhookNotifier()
    assert n._url == "https://hooks.slack.com/x"


async def test_slack_send_empty_batch_short_circuits():
    n = SlackWebhookNotifier(webhook_url="https://hooks.slack.com/x")
    batch = make_batch()
    result = await n.send(batch)
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 0


async def test_slack_send_success_2xx_marks_delivered():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    client = _client(handler)
    n = SlackWebhookNotifier(
        webhook_url="https://hooks.slack.com/x",
        client=client,
        channel_mention_on_critical=True,
    )
    crit = make_finding(verification="passed")
    extra = [make_finding(line=i + 1) for i in range(15)]
    batch = make_batch(crit, *extra)
    result = await n.send(batch)
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 16
    assert result.response_code == 200
    body = captured["body"]
    # @channel mention block must precede header when critical present.
    assert any("<!channel>" in str(b) for b in body["blocks"])
    # Top-N truncation context block emitted.
    assert any(
        b.get("type") == "context" and "more not shown" in b["elements"][0]["text"]
        for b in body["blocks"]
    )
    assert body["attachments"]


async def test_slack_payload_omits_raw_matched_value():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw"] = request.content
        return httpx.Response(200)

    client = _client(handler)
    raw = "AKIAIOSFODNN7EXAMPLE"
    n = SlackWebhookNotifier(webhook_url="https://hooks.slack.com/x", client=client)
    await n.send(make_batch(make_finding(matched=raw)))
    await n.close()
    assert raw.encode() not in captured["raw"]


async def test_slack_retries_on_5xx_then_succeeds():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200)

    client = _client(handler)
    n = SlackWebhookNotifier(
        webhook_url="https://hooks.slack.com/x",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is True
    assert state["n"] == 3


async def test_slack_retry_exhaustion_returns_failure():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(503)

    client = _client(handler)
    n = SlackWebhookNotifier(
        webhook_url="https://hooks.slack.com/x",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert state["n"] == 3
    assert "3 attempts" in (result.error or "")
    assert result.response_code == 503


async def test_slack_retries_on_429():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(429) if state["n"] == 1 else httpx.Response(200)

    client = _client(handler)
    n = SlackWebhookNotifier(
        webhook_url="https://hooks.slack.com/x",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is True


async def test_slack_transport_error_returns_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    n = SlackWebhookNotifier(
        webhook_url="https://hooks.slack.com/x",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert "transport error" in (result.error or "")


async def test_slack_close_does_not_close_external_client():
    captured: dict = {}

    def handler(request):
        return httpx.Response(200)

    client = _client(handler)
    n = SlackWebhookNotifier(webhook_url="https://hooks.slack.com/x", client=client)
    await n.close()
    # External client survived (still usable).
    captured["alive"] = not client.is_closed
    await client.aclose()
    assert captured["alive"]


def test_slack_is_retryable_branches():
    from pleno_pii_scanner.notify.transports.slack import _is_retryable

    assert _is_retryable(httpx.Response(503)) is True
    assert _is_retryable(httpx.Response(429)) is True
    assert _is_retryable(httpx.Response(200)) is False
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable("foo") is False


async def test_slack_owns_client_closes_on_close(monkeypatch):
    monkeypatch.setenv("PLENO_SLACK_WEBHOOK_URL", "https://hooks.slack.com/x")
    n = SlackWebhookNotifier()
    await n.close()
    assert n._client.is_closed
