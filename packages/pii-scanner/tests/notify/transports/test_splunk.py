"""Tests for Splunk HEC transport — chunking, retry, headers."""

from __future__ import annotations

import json

import httpx

from pleno_pii_scanner.notify.base import RetryPolicy
from pleno_pii_scanner.notify.transports.splunk import (
    DEFAULT_SOURCETYPE,
    SplunkHECNotifier,
)
from .._helpers import make_batch, make_finding


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(initial_seconds=0.0, factor=1.0, max_seconds=0.0, jitter=0.0)


async def test_splunk_empty_batch_short_circuits():
    n = SplunkHECNotifier(url="https://hec.example.com:8088", token="t")
    result = await n.send(make_batch())
    await n.close()
    assert result.delivered is True


async def test_splunk_single_request_for_small_batch():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"text": "ok"})

    client = _client(handler)
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="abc",
        host="scanner-1",
        index="pleno",
        client=client,
        retry_policy=_no_jitter(),
    )
    await n.send(make_batch(make_finding(), make_finding(line=2)))
    await n.close()
    assert len(captured) == 1
    req = captured[0]
    assert req.headers["authorization"] == "Splunk abc"
    assert req.url.path == "/services/collector/event"
    body = req.content
    lines = body.split(b"\n")
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert payload["sourcetype"] == DEFAULT_SOURCETYPE
    assert payload["host"] == "scanner-1"
    assert payload["index"] == "pleno"


async def test_splunk_chunks_by_event_count():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content.split(b"\n"))
        return httpx.Response(200)

    client = _client(handler)
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="t",
        client=client,
        max_events_per_request=1000,
        retry_policy=_no_jitter(),
    )
    findings = [make_finding(line=i) for i in range(1500)]
    result = await n.send(make_batch(*findings))
    await n.close()
    assert result.delivered is True
    assert len(captured) == 2
    assert len(captured[0]) == 1000
    assert len(captured[1]) == 500


async def test_splunk_chunks_by_byte_size():
    captured: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(len(request.content))
        return httpx.Response(200)

    client = _client(handler)
    # Force tiny byte cap so two findings split.
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="t",
        client=client,
        max_bytes_per_request=200,
        retry_policy=_no_jitter(),
    )
    await n.send(make_batch(make_finding(line=1), make_finding(line=2)))
    await n.close()
    assert len(captured) == 2


async def test_splunk_retries_on_5xx_then_succeeds():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(503) if state["n"] < 3 else httpx.Response(200)

    client = _client(handler)
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="t",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is True
    assert state["n"] == 3


async def test_splunk_retry_exhaustion():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(handler)
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="t",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert result.response_code == 503


async def test_splunk_4xx_returns_failure_without_retry():
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(403)

    client = _client(handler)
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="t",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert state["n"] == 1


async def test_splunk_transport_error_returns_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    client = _client(handler)
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="t",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert "transport error" in (result.error or "")


async def test_splunk_payload_omits_raw_value():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        return httpx.Response(200)

    client = _client(handler)
    raw = "AKIAIOSFODNN7EXAMPLE"
    n = SplunkHECNotifier(
        url="https://hec.example.com:8088",
        token="t",
        client=client,
        retry_policy=_no_jitter(),
    )
    await n.send(make_batch(make_finding(matched=raw)))
    await n.close()
    assert raw.encode() not in captured["body"]


def test_splunk_is_retryable_branches():
    from pleno_pii_scanner.notify.transports.splunk import _is_retryable

    assert _is_retryable(httpx.Response(503)) is True
    assert _is_retryable(httpx.Response(429)) is True
    assert _is_retryable(httpx.Response(202)) is False
    assert _is_retryable(httpx.ConnectError("x")) is True
    assert _is_retryable(object()) is False


async def test_splunk_owns_client_closes():
    n = SplunkHECNotifier(url="https://hec.example.com:8088", token="t")
    await n.close()
    assert n._client.is_closed
