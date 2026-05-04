"""Tests for retry_async + RetryConfig."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.scheduler.retry import (
    RetryConfig,
    RetryError,
    collect_retryable,
    merge_configs,
    retry_async,
    retryable_sequence,
)


# Deterministic doubles -----------------------------------------------------


def _no_jitter(_lo: float, _hi: float) -> float:
    return 1.0


def _record_sleeps(buf: list[float]):
    async def _sleep(s: float) -> None:
        buf.append(s)

    return _sleep


class TestRetryConfigValidation:
    def test_max_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            RetryConfig(max_attempts=0)

    def test_initial_backoff_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            RetryConfig(initial_backoff=0)

    def test_max_backoff_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            RetryConfig(max_backoff=0)

    def test_base_must_be_strictly_above_one(self) -> None:
        with pytest.raises(ValueError):
            RetryConfig(base=1.0)
        with pytest.raises(ValueError):
            RetryConfig(base=0.5)

    def test_jitter_must_be_ordered_non_negative(self) -> None:
        with pytest.raises(ValueError):
            RetryConfig(jitter=(0, 1))
        with pytest.raises(ValueError):
            RetryConfig(jitter=(1.5, 0.5))


class TestRetryAsync:
    async def test_returns_value_on_first_success(self) -> None:
        @retry_async(RetryConfig(max_attempts=3))
        async def fn() -> str:
            return "ok"

        assert await fn() == "ok"

    async def test_retries_on_failure_then_succeeds(self) -> None:
        attempts = {"n": 0}
        sleeps: list[float] = []

        @retry_async(
            RetryConfig(max_attempts=3, initial_backoff=1, max_backoff=10, base=2),
            sleep=_record_sleeps(sleeps),
            rand=_no_jitter,
        )
        async def fn() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError(f"flaky #{attempts['n']}")
            return "ok"

        assert await fn() == "ok"
        assert attempts["n"] == 3
        assert sleeps == [1.0, 2.0]  # initial * base**attempt, jitter=1.0

    async def test_exhausts_and_raises_retry_error(self) -> None:
        @retry_async(
            RetryConfig(max_attempts=2, initial_backoff=1, max_backoff=2, base=2),
            sleep=_record_sleeps([]),
            rand=_no_jitter,
        )
        async def fn() -> None:
            raise RuntimeError("always")

        with pytest.raises(RetryError) as exc:
            await fn()
        assert isinstance(exc.value.__cause__, RuntimeError)
        assert "exhausted" in str(exc.value)

    async def test_give_up_on_takes_precedence(self) -> None:
        # ValueError listed in give_up_on must surface immediately even if
        # also in retry_on.
        @retry_async(
            RetryConfig(
                max_attempts=5,
                retry_on=(Exception,),
                give_up_on=(ValueError,),
            ),
            sleep=_record_sleeps([]),
            rand=_no_jitter,
        )
        async def fn() -> None:
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await fn()

    async def test_unlisted_exception_propagates_without_retry(self) -> None:
        sleeps: list[float] = []

        @retry_async(
            RetryConfig(
                max_attempts=5,
                retry_on=(KeyError,),
            ),
            sleep=_record_sleeps(sleeps),
            rand=_no_jitter,
        )
        async def fn() -> None:
            raise RuntimeError("not retryable")

        with pytest.raises(RuntimeError):
            await fn()
        assert sleeps == []

    async def test_max_backoff_caps_growth(self) -> None:
        sleeps: list[float] = []

        @retry_async(
            RetryConfig(
                max_attempts=5,
                initial_backoff=1,
                max_backoff=4,
                base=2,
            ),
            sleep=_record_sleeps(sleeps),
            rand=_no_jitter,
        )
        async def fn() -> None:
            raise RuntimeError("fail")

        with pytest.raises(RetryError):
            await fn()
        # 1, 2, 4, 4 — fourth attempt would have been 8 but capped.
        assert sleeps == [1.0, 2.0, 4.0, 4.0]

    async def test_on_retry_callback_invoked(self) -> None:
        sleeps: list[float] = []
        calls: list[tuple[int, str, float]] = []

        async def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            calls.append((attempt, type(exc).__name__, delay))

        @retry_async(
            RetryConfig(max_attempts=3, initial_backoff=1, base=2),
            on_retry=on_retry,
            sleep=_record_sleeps(sleeps),
            rand=_no_jitter,
        )
        async def fn() -> None:
            raise RuntimeError("x")

        with pytest.raises(RetryError):
            await fn()
        # 2 retries (3 attempts total); on_retry fires after each failure
        # except the last.
        assert calls == [(0, "RuntimeError", 1.0), (1, "RuntimeError", 2.0)]

    async def test_jitter_window_applied(self) -> None:
        sleeps: list[float] = []

        @retry_async(
            RetryConfig(
                max_attempts=3,
                initial_backoff=10,
                max_backoff=100,
                base=2,
                jitter=(0.5, 0.5),
            ),
            sleep=_record_sleeps(sleeps),
            rand=lambda lo, hi: (lo + hi) / 2,
        )
        async def fn() -> None:
            raise RuntimeError("x")

        with pytest.raises(RetryError):
            await fn()
        # 10 * 2**0 * 0.5 = 5; 10 * 2**1 * 0.5 = 10.
        assert sleeps == [5.0, 10.0]


class TestHelpers:
    def test_collect_retryable_dedupes(self) -> None:
        assert collect_retryable(ValueError, ValueError, KeyError) == (
            ValueError,
            KeyError,
        )

    def test_retryable_sequence_materialises(self) -> None:
        seq = [RuntimeError, OSError]
        assert retryable_sequence(seq) == (RuntimeError, OSError)

    def test_merge_configs_overrides_only_specified(self) -> None:
        base = RetryConfig(max_attempts=5, initial_backoff=2)
        merged = merge_configs(base, max_attempts=10)
        assert merged.max_attempts == 10
        assert merged.initial_backoff == 2.0
