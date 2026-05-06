"""Tests for notify.base — severity mapping, excerpt masking, retry helper."""

from __future__ import annotations


import pytest

from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
    Notifier,
    RetryPolicy,
    SEVERITY_OTEL_NUMBER,
    excerpt,
    retry_call,
    severity_for,
)
from ._helpers import make_batch, make_finding


# ---------------- severity_for ----------------


def test_severity_for_passes_verification_promotes_to_critical():
    f = make_finding(verification="passed", score=0.1)
    assert severity_for(f) == "critical"


def test_severity_for_failed_verification_demotes_to_info():
    f = make_finding(verification="failed", score=0.99)
    assert severity_for(f) == "info"


def test_severity_for_high_score_unverified_is_high():
    f = make_finding(score=0.95)
    assert severity_for(f) == "high"


def test_severity_for_medium_score_is_medium():
    f = make_finding(score=0.7)
    assert severity_for(f) == "medium"


def test_severity_for_low_score_is_low():
    f = make_finding(score=0.3)
    assert severity_for(f) == "low"


def test_severity_otel_number_table_covers_known_levels():
    assert SEVERITY_OTEL_NUMBER["critical"] == 21
    assert SEVERITY_OTEL_NUMBER["high"] == 17


# ---------------- excerpt ----------------


def test_excerpt_short_value_fully_masked():
    f = make_finding(matched="ab")
    assert excerpt(f) == "**"


def test_excerpt_long_value_keeps_edges():
    f = make_finding(matched="ABCDEFGHIJKLMNOP")
    out = excerpt(f)
    assert out.startswith("AB")
    assert out.endswith("OP")
    assert "*" in out
    assert "CDEFGH" not in out


def test_excerpt_truncates_when_above_max_len():
    f = make_finding(matched="X" * 200)
    out = excerpt(f, max_len=20)
    assert len(out) == 20
    assert out.endswith("…")


def test_excerpt_never_contains_raw_value():
    raw = "AKIAIOSFODNN7EXAMPLE"
    f = make_finding(matched=raw)
    out = excerpt(f)
    assert raw not in out


# ---------------- NotificationBatch.filtered ----------------


def test_batch_filtered_returns_subset_with_recomputed_summary():
    a = make_finding(entity="EMAIL", score=0.95)
    b = make_finding(entity="PHONE", score=0.3)
    batch = make_batch(a, b)
    sub = batch.filtered(lambda f: f.entity == "EMAIL")
    assert sub.findings == (a,)
    assert sub.severity_summary == {"high": 1}
    assert sub.scan_id == batch.scan_id


def test_batch_filtered_empty_keeps_metadata():
    a = make_finding()
    batch = make_batch(a, source_kind="dir")
    sub = batch.filtered(lambda f: False)
    assert sub.findings == ()
    assert sub.severity_summary == {}
    assert sub.metadata == {"source_kind": "dir"}


# ---------------- RetryPolicy ----------------


def test_retry_policy_delay_grows_then_caps():
    p = RetryPolicy(initial_seconds=1.0, factor=2.0, max_seconds=4.0, jitter=0.0)
    assert p.delay(1) == pytest.approx(1.0)
    assert p.delay(2) == pytest.approx(2.0)
    assert p.delay(3) == pytest.approx(4.0)
    assert p.delay(10) == pytest.approx(4.0)


def test_retry_policy_jitter_within_bounds():
    p = RetryPolicy(initial_seconds=2.0, factor=1.0, max_seconds=10.0, jitter=0.5)
    for _ in range(50):
        d = p.delay(1)
        assert 1.0 <= d <= 3.0


# ---------------- retry_call ----------------


async def test_retry_call_returns_immediately_on_success():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        return "ok"

    result, attempts = await retry_call(
        op, policy=RetryPolicy(max_attempts=3), is_retryable=lambda _: False
    )
    assert result == "ok"
    assert attempts == 1
    assert calls == 1


async def test_retry_call_retries_until_success():
    state = {"n": 0}
    sleeps: list[float] = []

    async def op():
        state["n"] += 1
        return "fail" if state["n"] < 3 else "ok"

    async def fake_sleep(d):
        sleeps.append(d)

    result, attempts = await retry_call(
        op,
        policy=RetryPolicy(max_attempts=5, initial_seconds=0.01, jitter=0.0),
        is_retryable=lambda v: v == "fail",
        sleep=fake_sleep,
    )
    assert result == "ok"
    assert attempts == 3
    assert len(sleeps) == 2


async def test_retry_call_exhausts_returns_last_result():
    state = {"n": 0}

    async def op():
        state["n"] += 1
        return "fail"

    async def fake_sleep(_):
        return None

    result, attempts = await retry_call(
        op,
        policy=RetryPolicy(max_attempts=3, initial_seconds=0.01, jitter=0.0),
        is_retryable=lambda v: v == "fail",
        sleep=fake_sleep,
    )
    assert result == "fail"
    assert attempts == 3
    assert state["n"] == 3


async def test_retry_call_raises_non_retryable_exception_immediately():
    async def op():
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        await retry_call(
            op,
            policy=RetryPolicy(max_attempts=3),
            is_retryable=lambda v: False,
        )


async def test_retry_call_retries_then_raises_when_all_fail():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        raise ValueError("boom")

    async def fake_sleep(_):
        return None

    with pytest.raises(ValueError):
        await retry_call(
            op,
            policy=RetryPolicy(max_attempts=3, initial_seconds=0.01, jitter=0.0),
            is_retryable=lambda v: isinstance(v, ValueError),
            sleep=fake_sleep,
        )
    assert calls == 3


# ---------------- Notifier protocol ----------------


async def test_notifier_protocol_runtime_check():
    class _Stub:
        name = "stub"

        async def send(self, batch):
            return NotificationResult(
                transport="stub", delivered=True, delivered_count=0
            )

        async def close(self):
            pass

    assert isinstance(_Stub(), Notifier)


def test_notification_result_construction():
    r = NotificationResult(transport="x", delivered=True, delivered_count=2)
    assert r.error is None
    assert r.response_code is None


def test_notification_batch_default_metadata_empty():
    b = NotificationBatch(scan_id="s", findings=(), severity_summary={})
    assert b.metadata == {}


def test_severity_for_returns_low_when_score_below_thresholds():
    f = make_finding(score=0.0)
    assert severity_for(f) == "low"
