"""Jev filtering policy, wire contract, and real Presidio/SDK integration."""

import copy
import json
from unittest.mock import MagicMock

import pytest
from pleno_anonymize import PlenoAnonymize
from pleno_anonymize.presidio import JevContextFilter
from presidio_analyzer import Pattern, PatternRecognizer, RecognizerResult


def answer(choice="false_positive", confidence=0.99):
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": {
            key: 0.98 if key == choice else 0.01
            for key in ("entity", "false_positive", "uncertain")
        },
    }


def wire(monkeypatch, answers, status=200):
    connection = MagicMock()
    connection.getresponse.return_value.status = status
    connection.getresponse.return_value.read.return_value = json.dumps(
        {"model": "jev-test-pinned", "answers": answers}
    ).encode()
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr("pleno_anonymize.presidio.http.client.HTTPSConnection", factory)
    return connection, factory


def test_context_unicode_duplicates_and_audit(monkeypatch):
    text = "前文。May は人名。後文。May は月名。終わり。"
    starts = [text.index("May"), text.rindex("May")]
    results = [RecognizerResult("PERSON", start, start + 3, 0.7) for start in starts]
    results.insert(1, copy.deepcopy(results[0]))
    connection, factory = wire(
        monkeypatch, {"candidate_0": answer("entity"), "candidate_1": answer()}
    )
    audit = JevContextFilter(api_key="test", context_chars=4).evaluate(text, results)
    factory.assert_called_once_with("api.typesafe.ai", timeout=10.0)
    args, kwargs = connection.request.call_args
    assert args == ("POST", "/v1/systemone")
    assert kwargs["headers"]["Authorization"] == "Bearer test"
    payload = json.loads(kwargs["body"])
    assert len(payload["questions"]) == 2
    assert payload["state"]["candidate_0"] == {
        "entity_type": "PERSON",
        "before": "前文。",
        "span": "May",
        "after": " は人名",
    }
    assert payload["state"]["candidate_1"]["after"] == " は月名"
    assert [r.recognition_metadata["jev"]["keep"] for r in audit] == [True, True, False]
    assert all(r.score == 0.7 for r in audit)
    assert all("jev" not in (r.recognition_metadata or {}) for r in results)
    assert audit[0].recognition_metadata["jev"]["model"] == "jev-test-pinned"
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "decision,keep",
    [
        (answer(), False),
        (answer(confidence=0.9), False),
        (answer(confidence=0.89), True),
        (answer("entity"), True),
        (answer("uncertain"), True),
    ],
)
def test_confidence_policy(monkeypatch, decision, keep):
    wire(monkeypatch, {"candidate_0": decision})
    result = JevContextFilter(api_key="test").evaluate(
        "May", [RecognizerResult("PERSON", 0, 3, 1)]
    )[0]
    assert result.recognition_metadata["jev"]["keep"] is keep


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {**answer(), "type": "noul"},
        {**answer(), "confidence": float("nan")},
        {**answer(), "confidence": True},
        {**answer(), "confidence": "0.99"},
        {**answer(), "confidence": 2},
        {**answer(), "confidence": 10**500},
        {**answer(), "choice": []},
        {**answer(), "probabilities": {"false_positive": 1}},
        {
            **answer(),
            "probabilities": {"entity": 0.9, "false_positive": 0.05, "uncertain": 0.05},
        },
        {
            **answer(),
            "probabilities": {"entity": 0, "false_positive": 0.9, "uncertain": 0.2},
        },
    ],
)
def test_invalid_answer_retains_detection(monkeypatch, bad):
    wire(monkeypatch, {"candidate_0": bad})
    result = JevContextFilter(api_key="test").evaluate(
        "May", [RecognizerResult("PERSON", 0, 3, 1)]
    )[0]
    assert result.recognition_metadata["jev"] == {"keep": True, "status": "unavailable"}


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "bad_json",
        "deep_json",
        "bad_envelope",
        "oversized",
        "redirect",
        "unauthorized",
        "rate_limit",
        "server_error",
    ],
)
def test_transport_failures_do_not_unmask_or_log_secrets(monkeypatch, caplog, failure):
    connection, _ = wire(monkeypatch, {"candidate_0": answer()})
    response = connection.getresponse.return_value
    if failure == "timeout":
        connection.request.side_effect = TimeoutError("SECRET-CONTENT")
    elif failure in {"bad_json", "deep_json", "bad_envelope", "oversized"}:
        response.read.return_value = {
            "bad_json": b"SECRET-CONTENT",
            "deep_json": b"[" * 2000 + b"0" + b"]" * 2000,
            "bad_envelope": b"[]",
            "oversized": b"x" * 1_048_577,
        }[failure]
    else:
        response.status = {
            "redirect": 302,
            "unauthorized": 401,
            "rate_limit": 429,
            "server_error": 503,
        }[failure]
    result = JevContextFilter(api_key="SECRET-KEY").evaluate(
        "May", [RecognizerResult("PERSON", 0, 3, 1)]
    )[0]
    assert result.recognition_metadata["jev"]["keep"]
    assert "SECRET" not in caplog.text
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def test_batching_empty_and_long_spans(monkeypatch):
    connection, _ = wire(monkeypatch, {f"candidate_{i}": answer() for i in range(8)})
    enhancer = JevContextFilter(api_key="test")
    assert enhancer.evaluate("", []) == []
    assert not connection.request.called
    results = [RecognizerResult("PERSON", i, i + 1, 1) for i in range(9)]
    audited = enhancer.evaluate("abcdefghi", results)
    assert len(audited) == 9 and connection.request.call_count == 2
    assert all(not r.recognition_metadata["jev"]["keep"] for r in audited)
    result = enhancer.evaluate("a" * 2049, [RecognizerResult("PERSON", 0, 2049, 1)])[0]
    assert result.recognition_metadata["jev"] == {
        "keep": True,
        "status": "span_too_long",
    }
    assert connection.request.call_count == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_confidence": float("nan")},
        {"min_confidence": 0.5},
        {"min_confidence": True},
        {"min_confidence": 1.1},
        {"context_chars": -1},
        {"context_chars": 1025},
        {"context_chars": True},
        {"timeout": 0},
        {"timeout": float("inf")},
        {"model": ""},
    ],
)
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        JevContextFilter(api_key="test", **kwargs)


def test_missing_key_and_invalid_offsets(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        JevContextFilter()
    enhancer = JevContextFilter(api_key="test")
    with pytest.raises(ValueError, match="offsets"):
        enhancer.evaluate("May", [RecognizerResult("PERSON", -1, 3, 1)])


def test_sdk_analyze_and_redact_share_native_presidio_filter(monkeypatch):
    monkeypatch.delenv("PLENO_ANONYMIZE_BASE_URL", raising=False)
    monkeypatch.setattr("pleno_anonymize._local.is_installed", lambda _: False)
    monkeypatch.setattr("pleno_anonymize._local.ensure", lambda *a, **k: None)
    connection, _ = wire(monkeypatch, {"candidate_0": answer()})
    enhancer = JevContextFilter(api_key="test")
    engine = PlenoAnonymize(
        languages=("en",), auto_download=False, context_aware_enhancer=enhancer
    )
    analyzer = engine._get_analyzer()
    assert analyzer.context_aware_enhancer is enhancer
    analyzer.nlp_engine.nlp["en"].add_pipe("attribute_ruler").add(
        patterns=[[{"LOWER": "met"}]], attrs={"LEMMA": "met"}
    )
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="PERSON",
            supported_language="en",
            patterns=[Pattern("month_or_name", r"\bMay\b", 0.3)],
            context=["met"],
        )
    )
    text = "We met May yesterday."
    assert engine.analyze(text, language="en", entities=["PERSON"]) == []
    assert engine.redact(text, language="en", entities=["PERSON"]).text == text
    connection.getresponse.return_value.read.return_value = json.dumps(
        {"model": "jev-test-pinned", "answers": {"candidate_0": answer("entity")}}
    ).encode()
    findings = engine.analyze(text, language="en", entities=["PERSON"])
    assert len(findings) == 1 and findings[0].score == pytest.approx(0.65)
    assert (
        engine.redact(text, language="en", entities=["PERSON"]).text
        == "We met <PERSON> yesterday."
    )
    connection.request.side_effect = TimeoutError()
    assert (
        engine.redact(text, language="en", entities=["PERSON"]).text
        == "We met <PERSON> yesterday."
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"base_url": "https://example.test"}, {"engine": "openai-privacy-filter"}],
)
def test_unsupported_engines_reject_silent_noop(kwargs):
    with pytest.raises(ValueError, match="local builtin"):
        PlenoAnonymize(
            context_aware_enhancer=JevContextFilter(api_key="test"), **kwargs
        )
