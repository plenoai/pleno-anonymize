"""Tests for the generic webhook transport including HMAC signing."""

from __future__ import annotations

import json

import httpx

from pleno_pii_scanner.notify.base import RetryPolicy
from pleno_pii_scanner.notify.transports.webhook import (
    SIGNATURE_HEADER,
    WebhookNotifier,
    verify_hmac,
)
from .._helpers import make_batch, make_finding


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(initial_seconds=0.0, factor=1.0, max_seconds=0.0, jitter=0.0)


async def test_webhook_empty_batch_short_circuits():
    n = WebhookNotifier(url="https://example.com/hook")
    result = await n.send(make_batch())
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 0


async def test_webhook_post_includes_findings_metadata_and_no_raw_value():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = dict(request.headers)
        return httpx.Response(200)

    client = _client(handler)
    n = WebhookNotifier(
        url="https://example.com/hook",
        client=client,
        headers={"X-Tenant": "acme"},
    )
    raw = "AKIAIOSFODNN7EXAMPLE"
    f = make_finding(matched=raw)
    result = await n.send(make_batch(f, source_kind="dir"))
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 1
    assert captured["headers"]["x-tenant"] == "acme"
    assert raw.encode() not in captured["body"]
    body = json.loads(captured["body"])
    assert body["metadata"] == {"source_kind": "dir"}
    assert body["findings"][0]["fingerprint"] == f.fingerprint()


async def test_webhook_signs_body_with_hmac():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["sig"] = request.headers.get(SIGNATURE_HEADER.lower())
        return httpx.Response(200)

    secret = "shh-very-secret"
    client = _client(handler)
    n = WebhookNotifier(url="https://example.com/hook", secret=secret, client=client)
    await n.send(make_batch(make_finding()))
    await n.close()
    assert captured["sig"] is not None
    assert verify_hmac(captured["body"], secret, captured["sig"]) is True


async def test_webhook_unsigned_request_omits_signature_header():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["sig"] = request.headers.get(SIGNATURE_HEADER.lower())
        return httpx.Response(200)

    client = _client(handler)
    n = WebhookNotifier(url="https://example.com/hook", client=client)
    await n.send(make_batch(make_finding()))
    await n.close()
    assert captured["sig"] is None


async def test_verify_hmac_rejects_tampered_payload():
    secret = "k"
    body = b'{"x":1}'

    def handler(request):
        return httpx.Response(200)

    client = _client(handler)
    n = WebhookNotifier(url="https://example.com/hook", secret=secret, client=client)
    captured: dict = {}

    def cap_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["sig"] = request.headers.get(SIGNATURE_HEADER.lower())
        return httpx.Response(200)

    client2 = _client(cap_handler)
    n2 = WebhookNotifier(url="https://example.com/hook", secret=secret, client=client2)
    await n2.send(make_batch(make_finding()))
    await n2.close()
    await n.close()
    assert verify_hmac(captured["body"] + b"x", secret, captured["sig"]) is False


async def test_webhook_retry_on_5xx_then_success():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(202)

    client = _client(handler)
    n = WebhookNotifier(
        url="https://example.com/hook", client=client, retry_policy=_no_jitter()
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is True
    assert result.response_code == 202
    assert state["n"] == 3


async def test_webhook_retry_exhaustion():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(handler)
    n = WebhookNotifier(
        url="https://example.com/hook", client=client, retry_policy=_no_jitter()
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert "3 attempts" in (result.error or "")


async def test_webhook_4xx_not_retried_and_marked_failed():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(400)

    client = _client(handler)
    n = WebhookNotifier(
        url="https://example.com/hook", client=client, retry_policy=_no_jitter()
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert state["n"] == 1
    assert result.response_code == 400


async def test_webhook_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("rst")

    client = _client(handler)
    n = WebhookNotifier(
        url="https://example.com/hook", client=client, retry_policy=_no_jitter()
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert "transport error" in (result.error or "")


def test_webhook_is_retryable_branches():
    from pleno_pii_scanner.notify.transports.webhook import _is_retryable

    assert _is_retryable(httpx.Response(500)) is True
    assert _is_retryable(httpx.Response(429)) is True
    assert _is_retryable(httpx.Response(204)) is False
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable(123) is False


async def test_webhook_owns_client_closes():
    n = WebhookNotifier(url="https://example.com/hook")
    await n.close()
    assert n._client.is_closed
