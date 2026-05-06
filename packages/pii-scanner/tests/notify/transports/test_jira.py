"""Tests for Jira transport including dedup-by-fingerprint behaviour."""

from __future__ import annotations

import base64
import json

import httpx

from pleno_pii_scanner.notify.base import RetryPolicy
from pleno_pii_scanner.notify.transports.jira import JiraNotifier
from pleno_pii_scanner.notify._adf import build_issue_adf, comment_adf
from .._helpers import make_batch, make_finding


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(initial_seconds=0.0, factor=1.0, max_seconds=0.0, jitter=0.0)


async def test_jira_empty_batch_short_circuits():
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="bot@acme.example",
        api_token="t",
        project_key="SEC",
    )
    result = await n.send(make_batch())
    await n.close()
    assert result.delivered is True


async def test_jira_creates_new_issue_when_no_match():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/issue"):
            return httpx.Response(201, json={"key": "SEC-1"})
        return httpx.Response(404)

    client = _client(handler)
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="bot@acme.example",
        api_token="tok",
        project_key="SEC",
        client=client,
        retry_policy=_no_jitter(),
    )
    f = make_finding()
    result = await n.send(make_batch(f))
    await n.close()
    assert result.delivered is True
    assert result.delivered_count == 1
    assert any(r.url.path.endswith("/issue") and r.method == "POST" for r in seen)
    create_req = next(r for r in seen if r.url.path.endswith("/issue"))
    body = json.loads(create_req.content)
    assert body["fields"]["project"]["key"] == "SEC"
    assert f.fingerprint() in body["fields"]["summary"]
    expected_token = base64.b64encode(b"bot@acme.example:tok").decode()
    assert create_req.headers["authorization"] == f"Basic {expected_token}"


async def test_jira_dedup_adds_comment_when_existing_open_issue():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200, json={"issues": [{"key": "SEC-42", "fields": {"summary": "x"}}]}
            )
        if "/issue/SEC-42/comment" in request.url.path:
            return httpx.Response(201, json={"id": "10001"})
        if request.url.path.endswith("/issue") and request.method == "POST":
            raise AssertionError("must not create duplicate issue")
        return httpx.Response(404)

    client = _client(handler)
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="bot@acme.example",
        api_token="t",
        project_key="SEC",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is True
    assert any("/comment" in r.url.path for r in seen)


async def test_jira_search_failure_returns_unhealthy_result():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(403)
        return httpx.Response(404)

    client = _client(handler)
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="b@e",
        api_token="t",
        project_key="SEC",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False


async def test_jira_create_failure_returns_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/issue") and request.method == "POST":
            return httpx.Response(400, json={"errors": {"summary": "bad"}})
        return httpx.Response(404)

    client = _client(handler)
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="b@e",
        api_token="t",
        project_key="SEC",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False


async def test_jira_comment_failure_returns_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200, json={"issues": [{"key": "SEC-9", "fields": {"summary": "x"}}]}
            )
        if "/comment" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(404)

    client = _client(handler)
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="b@e",
        api_token="t",
        project_key="SEC",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False


async def test_jira_transport_error_returns_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("rst")

    client = _client(handler)
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="b@e",
        api_token="t",
        project_key="SEC",
        client=client,
        retry_policy=_no_jitter(),
    )
    result = await n.send(make_batch(make_finding()))
    await n.close()
    assert result.delivered is False
    assert "transport error" in (result.error or "")


async def test_jira_owns_client_closes():
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="b@e",
        api_token="t",
        project_key="SEC",
    )
    await n.close()
    assert n._client.is_closed


async def test_jira_payload_does_not_contain_raw_match():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"issues": []})
        if request.url.path.endswith("/issue") and request.method == "POST":
            seen["body"] = request.content
            return httpx.Response(201, json={"key": "SEC-1"})
        return httpx.Response(404)

    client = _client(handler)
    raw = "AKIAIOSFODNN7EXAMPLE"
    n = JiraNotifier(
        base_url="https://acme.atlassian.net",
        email="b@e",
        api_token="t",
        project_key="SEC",
        client=client,
        retry_policy=_no_jitter(),
    )
    await n.send(make_batch(make_finding(matched=raw)))
    await n.close()
    assert raw.encode() not in seen["body"]


def test_adf_builders_smoke():
    f = make_finding()
    doc = build_issue_adf(
        scan_id="s",
        findings=[f],
        severity_summary={"low": 1},
        metadata={"k": "v"},
    )
    assert doc["type"] == "doc"
    assert doc["content"][0]["type"] == "heading"
    assert any(node["type"] == "table" for node in doc["content"])

    cdoc = comment_adf("hi")
    assert cdoc["type"] == "doc"
    assert cdoc["content"][0]["content"][0]["text"] == "hi"


def test_adf_empty_metadata_renders_dash():
    f = make_finding()
    doc = build_issue_adf(
        scan_id="s",
        findings=[f],
        severity_summary={"low": 1},
        metadata={},
    )
    paragraphs = [node for node in doc["content"] if node["type"] == "paragraph"]
    texts = [p["content"][0]["text"] for p in paragraphs]
    assert any("Metadata: -" in t for t in texts)


def test_jira_is_retryable_unknown_value_returns_false():
    from pleno_pii_scanner.notify.transports.jira import _is_retryable

    assert _is_retryable("string") is False
    assert _is_retryable(None) is False


def test_adf_empty_severity_summary_renders_no_findings():
    doc = build_issue_adf(
        scan_id="s",
        findings=[],
        severity_summary={},
        metadata={},
    )
    paragraphs = [node for node in doc["content"] if node["type"] == "paragraph"]
    texts = [p["content"][0]["text"] for p in paragraphs]
    assert any("no findings" in t for t in texts)
