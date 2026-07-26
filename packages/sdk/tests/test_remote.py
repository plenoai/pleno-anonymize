"""RemoteEngine — verified with a real urllib stub (no network)."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from pleno_anonymize import PlenoAnonymize, RemoteEngine
from pleno_anonymize._remote import PlenoAnonymizeError


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _ok(payload: object) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


def test_factory_returns_remote_engine_when_base_url_set() -> None:
    engine = PlenoAnonymize(base_url="https://pleno-anonymize.fly.dev")
    assert isinstance(engine, RemoteEngine)


def test_remote_analyze_posts_payload_and_parses_findings() -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["auth"] = req.headers.get("Authorization")
        return _ok(
            [
                {
                    "entity_type": "EMAIL_ADDRESS",
                    "start": 0,
                    "end": 11,
                    "score": 1.0,
                    "text": "x@y.example",
                },
            ]
        )

    engine = RemoteEngine(base_url="https://example.test/", api_key="tok")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        findings = engine.analyze("x@y.example", language="en")

    assert captured["url"] == "https://example.test/api/analyze"
    assert captured["method"] == "POST"
    assert captured["body"] == {"text": "x@y.example", "language": "en"}
    assert captured["auth"] == "Bearer tok"
    assert len(findings) == 1
    assert findings[0].entity_type == "EMAIL_ADDRESS"
    assert findings[0].text == "x@y.example"


def test_remote_redact_returns_text() -> None:
    def fake_urlopen(req, timeout):
        return _ok({"text": "<EMAIL_ADDRESS>", "items": []})

    engine = RemoteEngine(base_url="https://example.test")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = engine.redact("a@b.example", language="en")
    assert result.text == "<EMAIL_ADDRESS>"


def test_remote_http_error_becomes_pleno_error() -> None:
    import urllib.error

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=422,
            msg="Unprocessable",
            hdrs=None,
            fp=io.BytesIO(json.dumps({"detail": "bad"}).encode("utf-8")),
        )

    engine = RemoteEngine(base_url="https://example.test")
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        pytest.raises(PlenoAnonymizeError) as exc,
    ):
        engine.analyze("x")
    assert exc.value.status == 422
    assert exc.value.body == {"detail": "bad"}


def test_remote_analyze_missing_field_raises_pleno_error() -> None:
    def fake_urlopen(req, timeout):
        return _ok([{"entity_type": "EMAIL_ADDRESS", "start": 0, "end": 5}])

    engine = RemoteEngine(base_url="https://example.test")
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        pytest.raises(PlenoAnonymizeError) as exc,
    ):
        engine.analyze("hello")
    assert "score" in str(exc.value)


def test_remote_analyze_invalid_field_type_raises_pleno_error() -> None:
    def fake_urlopen(req, timeout):
        return _ok(
            [
                {
                    "entity_type": "EMAIL_ADDRESS",
                    "start": "not-an-int",
                    "end": 5,
                    "score": 1.0,
                }
            ]
        )

    engine = RemoteEngine(base_url="https://example.test")
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        pytest.raises(PlenoAnonymizeError),
    ):
        engine.analyze("hello")


def test_remote_analyze_non_object_item_raises_pleno_error() -> None:
    def fake_urlopen(req, timeout):
        return _ok(["not-a-dict"])

    engine = RemoteEngine(base_url="https://example.test")
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        pytest.raises(PlenoAnonymizeError),
    ):
        engine.analyze("hello")


def test_remote_redact_missing_text_raises_pleno_error() -> None:
    def fake_urlopen(req, timeout):
        return _ok({"items": []})

    engine = RemoteEngine(base_url="https://example.test")
    with (
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
        pytest.raises(PlenoAnonymizeError),
    ):
        engine.redact("a@b.example")


def test_remote_trailing_slash_normalized() -> None:
    seen: list[str] = []

    def fake_urlopen(req, timeout):
        seen.append(req.full_url)
        return _ok([])

    engine = RemoteEngine(base_url="https://example.test///")
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        engine.analyze("hi")
    assert seen == ["https://example.test/api/analyze"]
