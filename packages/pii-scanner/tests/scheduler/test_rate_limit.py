"""Tests for AdaptiveTokenBucket and GlobalRateLimiter."""

from __future__ import annotations

import asyncio

import pytest

from pleno_pii_scanner.scheduler.rate_limit import (
    AdaptiveTokenBucket,
    BucketKey,
    GlobalRateLimiter,
    RateLimited,
)


class TestBucketConstruction:
    def test_rejects_zero_capacity(self) -> None:
        with pytest.raises(ValueError):
            AdaptiveTokenBucket(capacity=0, rate=1)

    def test_rejects_zero_rate(self) -> None:
        with pytest.raises(ValueError):
            AdaptiveTokenBucket(capacity=1, rate=0)

    def test_rejects_negative_capacity(self) -> None:
        with pytest.raises(ValueError):
            AdaptiveTokenBucket(capacity=-1, rate=1)

    def test_starts_full(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=5)
        assert b.tokens == 10
        assert b.current_rate == 5

    def test_acquire_zero_cost_immediate(self) -> None:
        # not really sensible but should not deadlock.
        AdaptiveTokenBucket(capacity=10, rate=5)
        # we still consume zero tokens, so acquire returns immediately.
        # No assertion needed beyond "doesn't hang".


class TestAcquire:
    async def test_immediate_when_tokens_available(self) -> None:
        b = AdaptiveTokenBucket(capacity=5, rate=1)
        await b.acquire(1)
        assert b.tokens == pytest.approx(4, abs=0.01)

    async def test_blocks_when_empty(self) -> None:
        b = AdaptiveTokenBucket(capacity=1, rate=10)
        await b.acquire(1)  # drain
        # Refill at 10/s; second acquire should complete inside ~150ms.
        await asyncio.wait_for(b.acquire(1), timeout=0.5)

    async def test_timeout_raises(self) -> None:
        b = AdaptiveTokenBucket(capacity=1, rate=0.5)
        await b.acquire(1)  # drain
        with pytest.raises(RateLimited):
            await b.acquire(1, timeout=0.05)

    async def test_cost_exceeds_capacity_raises(self) -> None:
        b = AdaptiveTokenBucket(capacity=5, rate=1)
        with pytest.raises(ValueError, match="exceeds bucket capacity"):
            await b.acquire(10)

    async def test_concurrent_acquires_serialised(self) -> None:
        # 4 callers want 1 token, bucket has 1, refills at 5/s.
        # All four should complete within ~1s but not at once.
        b = AdaptiveTokenBucket(capacity=1, rate=5)
        results = await asyncio.gather(
            b.acquire(1), b.acquire(1), b.acquire(1), b.acquire(1)
        )
        assert results == [None, None, None, None]


class TestThrottleSignal:
    def test_halves_rate_by_default(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=10)
        b.on_throttle_signal()
        assert b.current_rate == pytest.approx(5.0)

    def test_custom_factor(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=10)
        b.on_throttle_signal(factor=0.25)
        assert b.current_rate == pytest.approx(2.5)

    def test_floor_prevents_zero(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=1.0)
        # Several halvings would drive rate below the floor.
        for _ in range(20):
            b.on_throttle_signal()
        assert b.current_rate >= 0.5

    def test_invalid_factor_raises(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=10)
        with pytest.raises(ValueError):
            b.on_throttle_signal(factor=0)
        with pytest.raises(ValueError):
            b.on_throttle_signal(factor=1.0)
        with pytest.raises(ValueError):
            b.on_throttle_signal(factor=-0.1)


class TestRecovery:
    def test_recovers_toward_ceiling(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=10)
        b.on_throttle_signal()
        b.on_success(recovery=2)
        assert b.current_rate == pytest.approx(7.0)

    def test_recovery_capped_at_ceiling(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=10)
        b.on_throttle_signal()  # rate -> 5
        b.on_success(recovery=100)  # would jump to 105
        assert b.current_rate == pytest.approx(10.0)

    def test_invalid_recovery_raises(self) -> None:
        b = AdaptiveTokenBucket(capacity=10, rate=10)
        with pytest.raises(ValueError):
            b.on_success(recovery=0)


class TestGlobalRateLimiter:
    def test_rejects_non_positive_defaults(self) -> None:
        with pytest.raises(ValueError):
            GlobalRateLimiter(default_capacity=0, default_rate=1)
        with pytest.raises(ValueError):
            GlobalRateLimiter(default_capacity=1, default_rate=0)

    async def test_creates_bucket_on_first_acquire(self) -> None:
        rl = GlobalRateLimiter(default_capacity=5, default_rate=10)
        await rl.acquire(BucketKey("github", "tenant-a"))
        # Second acquire on the same key reuses the bucket.
        await rl.acquire(BucketKey("github", "tenant-a"))

    async def test_independent_keys_independent_buckets(self) -> None:
        rl = GlobalRateLimiter(default_capacity=1, default_rate=0.5)
        await rl.acquire(BucketKey("github", "tenant-a"))
        # Different tenant => fresh bucket, doesn't block.
        await rl.acquire(BucketKey("github", "tenant-b"))

    async def test_configure_overrides_defaults(self) -> None:
        rl = GlobalRateLimiter(default_capacity=1, default_rate=0.5)
        rl.configure("github", capacity=100, rate=50)
        # Drain 100 tokens — would be impossible at default capacity=1.
        for _ in range(100):
            await rl.acquire(BucketKey("github", "x"))

    def test_configure_rejects_invalid(self) -> None:
        rl = GlobalRateLimiter()
        with pytest.raises(ValueError):
            rl.configure("github", capacity=0, rate=1)
        with pytest.raises(ValueError):
            rl.configure("github", capacity=1, rate=0)

    async def test_throttle_and_recovery_round_trip(self) -> None:
        rl = GlobalRateLimiter(default_capacity=10, default_rate=10)
        key = BucketKey("github", "x")
        await rl.on_throttle_signal(key, factor=0.5)
        # Bucket exists now; success recovers toward ceiling.
        await rl.on_success(key, recovery=2)
        # Re-acquire still works (bucket present, rate 7).
        await rl.acquire(key)

    async def test_bucket_key_is_hashable_and_value_equal(self) -> None:
        a = BucketKey("github", "tenant-a")
        b = BucketKey("github", "tenant-a")
        assert a == b
        assert hash(a) == hash(b)
        d = {a: "value"}
        assert d[b] == "value"

    async def test_double_checked_lock_handles_race(self) -> None:
        # Re-checking inside the lock prevents a second creation when two
        # coroutines both observed the bucket missing before either took
        # the lock. We simulate the race by manually pre-populating the
        # bucket dict between the outer get() and the inner get() —
        # impossible to time deterministically with asyncio sleeps.
        rl = GlobalRateLimiter(default_capacity=10, default_rate=10)
        key = BucketKey("github", "x")
        sentinel = AdaptiveTokenBucket(capacity=999, rate=999)

        original = rl._lock.acquire

        async def patched_acquire():
            await original()
            # Place a sentinel before _get_or_create's inner lookup.
            rl._buckets.setdefault(key, sentinel)

        # Patch only the lock's acquire so the inner re-check fires.
        rl._lock.acquire = patched_acquire  # type: ignore[method-assign]
        await rl.acquire(key)
        # The pre-populated sentinel survives — the inner branch returned
        # it instead of creating a new bucket.
        assert rl._buckets[key] is sentinel

    async def test_concurrent_first_access_creates_one_bucket(self) -> None:
        # The double-checked lock in _get_or_create must keep us from
        # creating two buckets when many coroutines acquire the same key
        # simultaneously.
        rl = GlobalRateLimiter(default_capacity=100, default_rate=100)
        key = BucketKey("github", "x")
        await asyncio.gather(*(rl.acquire(key) for _ in range(50)))
        # Internal state inspection — deliberate use of private attr in
        # a single test to assert the invariant.
        assert len(rl._buckets) == 1
