"""Adaptive token-bucket rate limiter (per-tenant, per-connector-kind).

Each `(connector_kind, tenant_id)` pair gets its own AIMD token bucket.
On 429 / rate-limit signals from the connector the bucket halves its
fill rate; on sustained success the rate climbs back linearly toward the
configured ceiling. This matches the pattern every cloud SDK has
implemented in different shapes (boto3 adaptive mode, Google `RetryPolicy`,
Slack `AsyncRateLimitErrorRetryHandler`) and gives connectors a uniform
back-pressure interface.

ADR-0007 §4.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic


# Floor for the per-bucket fill rate after AIMD shrink. Going to zero
# would deadlock the connector; a small floor keeps the scan crawling
# while the operator inspects the upstream limit.
_MIN_RATE = 0.5  # tokens/sec


class RateLimited(Exception):
    """Raised when the caller asked for tokens it cannot get within timeout.

    Connectors that catch this should yield control back to the scheduler
    rather than tight-spinning; the scheduler may pick up other work and
    retry the rate-limited operation later.
    """


@dataclass(frozen=True, slots=True)
class BucketKey:
    """Identifier used to scope rate limits.

    `connector_kind` aggregates limits across all sources of a given
    backend ("github", "aws-s3"); `tenant_id` is the per-account /
    per-workspace partition the upstream API enforces ("org:plenoai",
    "aws:123456789012", "slack:T01234"). The 5000 req/h GitHub PAT
    budget, for example, is per-token (= per-tenant), not global, so we
    must not cross-pollute counts between two GitHub installations.
    """

    connector_kind: str
    tenant_id: str


@dataclass(slots=True)
class AdaptiveTokenBucket:
    """Single AIMD token bucket.

    `capacity` and `rate` express the upstream's stated long-run budget.
    `current_rate` floats below `rate` after a 429 and recovers linearly.
    `tokens` is the running balance; `acquire()` blocks until at least
    `cost` tokens accumulate, taking the asyncio scheduling cost into
    account so two coroutines waking simultaneously don't both withdraw
    on a stale view.
    """

    capacity: float
    rate: float  # ceiling (tokens/sec)
    current_rate: float = field(init=False)
    tokens: float = field(init=False)
    last_refill: float = field(default_factory=monotonic, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.rate <= 0:
            raise ValueError("capacity and rate must be > 0")
        self.current_rate = self.rate
        self.tokens = self.capacity

    async def acquire(self, cost: float = 1.0, timeout: float | None = None) -> None:
        """Block until `cost` tokens are available.

        Raises `RateLimited` if `timeout` elapses first. `cost > capacity`
        is rejected eagerly — the caller must lower the cost or split the
        request, since the bucket can never accumulate that many tokens.
        """
        if cost > self.capacity:
            raise ValueError(
                f"cost {cost} exceeds bucket capacity {self.capacity}; "
                f"connector should split the request"
            )
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            async with self._lock:
                self._refill_locked()
                if self.tokens >= cost:
                    self.tokens -= cost
                    return
                deficit = cost - self.tokens
                wait = deficit / self.current_rate
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise RateLimited(
                        f"could not acquire {cost} tokens within {timeout}s "
                        f"(current_rate={self.current_rate}/s)"
                    )
                wait = min(wait, remaining)
            await asyncio.sleep(wait)

    def on_throttle_signal(self, factor: float = 0.5) -> None:
        """Halve (or scale by `factor`) the fill rate after upstream 429.

        Called by the connector when it sees a `Retry-After` header,
        `RateLimitExceeded`, `429 Too Many Requests`, or an SDK-specific
        equivalent. The next refill uses the reduced rate; previously
        accumulated tokens are NOT clawed back (the upstream already
        accepted those requests).
        """
        if not 0 < factor < 1:
            raise ValueError("factor must be in (0, 1)")
        self.current_rate = max(_MIN_RATE, self.current_rate * factor)

    def on_success(self, recovery: float = 0.5) -> None:
        """Recover toward the ceiling on sustained success.

        Linear additive increase (`current_rate += recovery`) — slower
        than multiplicative so a flaky upstream doesn't see us spike back
        to full rate after one good response. `recovery` is in tokens/sec
        per call; the scheduler typically calls this once per successful
        batch rather than once per request.
        """
        if recovery <= 0:
            raise ValueError("recovery must be > 0")
        self.current_rate = min(self.rate, self.current_rate + recovery)

    def _refill_locked(self) -> None:
        # Caller holds the lock. Compute elapsed time since last refill,
        # add `current_rate * elapsed` tokens (capped at capacity), and
        # advance the last-refill marker.
        now = monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.current_rate)
            self.last_refill = now


class GlobalRateLimiter:
    """Per-(kind, tenant) bucket registry shared across all connectors.

    The Scheduler owns one of these for the duration of a scan and hands
    the same instance to every connector it spawns. Buckets are created
    on first use and cached; the registry never shrinks (a scan is short
    enough that the memory cost is negligible compared to bucket churn).
    """

    def __init__(
        self, *, default_capacity: float = 100.0, default_rate: float = 10.0
    ) -> None:
        if default_capacity <= 0 or default_rate <= 0:
            raise ValueError("default_capacity and default_rate must be > 0")
        self._default_capacity = default_capacity
        self._default_rate = default_rate
        self._buckets: dict[BucketKey, AdaptiveTokenBucket] = {}
        self._overrides: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    def configure(self, connector_kind: str, *, capacity: float, rate: float) -> None:
        """Pin (capacity, rate) for a kind — applied to future bucket creation.

        Doesn't retroactively replace existing buckets; the Scheduler
        configures all kinds before opening the first connector so this
        is the practical lifetime ordering.
        """
        if capacity <= 0 or rate <= 0:
            raise ValueError("capacity and rate must be > 0")
        self._overrides[connector_kind] = (capacity, rate)

    async def acquire(
        self, key: BucketKey, *, cost: float = 1.0, timeout: float | None = None
    ) -> None:
        bucket = await self._get_or_create(key)
        await bucket.acquire(cost=cost, timeout=timeout)

    async def on_throttle_signal(self, key: BucketKey, *, factor: float = 0.5) -> None:
        bucket = await self._get_or_create(key)
        bucket.on_throttle_signal(factor=factor)

    async def on_success(self, key: BucketKey, *, recovery: float = 0.5) -> None:
        bucket = await self._get_or_create(key)
        bucket.on_success(recovery=recovery)

    async def _get_or_create(self, key: BucketKey) -> AdaptiveTokenBucket:
        bucket = self._buckets.get(key)
        if bucket is not None:
            return bucket
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                return bucket
            cap, rate = self._overrides.get(
                key.connector_kind, (self._default_capacity, self._default_rate)
            )
            bucket = AdaptiveTokenBucket(capacity=cap, rate=rate)
            self._buckets[key] = bucket
            return bucket
