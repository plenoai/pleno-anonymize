"""Reservoir sampling for huge S3 buckets.

ADR-0007 §16 cites `n = log(0.05) / log(0.99) ≈ 299` for 95% confidence
that, if at least 1% of a population contains PII, the sample will
include at least one positive. We round to **300** as the default
reservoir size.

Why a separate module: the algorithm is generic (pulls from any iterator)
and lives apart from S3 plumbing so it can be unit-tested in isolation
without touching aioboto3 or moto. A future GCS / Azure Blob connector
can reuse the same logic by importing this module.

Algorithm: Algorithm R (Vitter, 1985). For an unknown-length stream we
keep the first `k` elements verbatim, then for each subsequent element
at index `i` we replace a random reservoir slot with probability `k/i`.
The result is a uniform random sample of size `min(k, total)` regardless
of the eventual stream length, and we never need to materialize the full
stream — critical for buckets with 10⁹+ keys.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

# ADR-0007 §16: 95% confidence at p=1%, ceil(log(0.05)/log(0.99)) = 299;
# round up to 300 for memorability.
DEFAULT_RESERVOIR_SIZE = 300


@dataclass(frozen=True, slots=True)
class SamplingDecision:
    """Result of `should_sample(total_objects, ...)` per bucket.

    `enabled=True` means the discovery loop should drive `reservoir_sample`
    instead of yielding every key. `reason` is logged for operator
    transparency so a sampled bucket never surprises the user — they see
    "bucket too large for full enumeration (>10**6 keys), reservoir n=300".
    """

    enabled: bool
    reservoir_size: int
    reason: str


def should_sample(
    total_objects: int | None,
    *,
    threshold: int = 1_000_000,
    reservoir_size: int = DEFAULT_RESERVOIR_SIZE,
    forced: bool | None = None,
) -> SamplingDecision:
    """Decide whether reservoir sampling should be enabled for a bucket.

    `forced=True` overrides the threshold (operator explicitly asked to
    sample). `forced=False` disables sampling unconditionally (operator
    explicitly asked for a full scan even on huge buckets — they are on
    the hook for the cost). `forced=None` (default) picks based on
    `total_objects`: sample only when the count exceeds `threshold`.
    `total_objects=None` means the operator did not annotate the bucket
    with `estimated_object_count`; we default to **full enumeration**
    rather than silently sampling. The operator opts in by setting
    `estimated_object_count` (so the threshold check fires), or by
    passing `forced=True` for an explicit sample.
    """
    if reservoir_size < 1:
        raise ValueError("reservoir_size must be >= 1")
    if threshold < 1:
        raise ValueError("threshold must be >= 1")
    if forced is True:
        return SamplingDecision(True, reservoir_size, reason="explicit forced=True")
    if forced is False:
        return SamplingDecision(False, reservoir_size, reason="explicit forced=False")
    if total_objects is None:
        return SamplingDecision(
            False,
            reservoir_size,
            reason="bucket size unannotated — full enumeration",
        )
    if total_objects > threshold:
        return SamplingDecision(
            True,
            reservoir_size,
            reason=f"bucket has {total_objects} objects (> {threshold})",
        )
    return SamplingDecision(
        False,
        reservoir_size,
        reason=f"bucket has {total_objects} objects (<= {threshold})",
    )


@dataclass(slots=True)
class ReservoirSampler(Generic[T]):
    """Vitter's Algorithm R reservoir sampler.

    State is mutated as items arrive (`offer`). The `samples` view is a
    snapshot of the current reservoir contents; the order is undefined
    by the algorithm, but the discovery loop in `s3.py` does not rely on
    order — it only needs a uniform sample for downstream PII inspection.

    `seen` counts every offered item so observers can compute the
    sampling fraction and compare against the configured budget.
    """

    capacity: int
    rng: random.Random
    _reservoir: list[T]
    seen: int = 0

    @classmethod
    def make(cls, capacity: int, *, seed: int | None = None) -> "ReservoirSampler[T]":
        """Construct a sampler with `capacity` slots.

        `seed` makes the sampling deterministic — useful for reproducible
        scans (CI fixtures) and idempotent enterprise re-runs. In
        production the operator typically leaves it None to get a fresh
        random.SystemRandom instance.
        """
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        rng = random.Random(seed) if seed is not None else random.SystemRandom()
        return cls(capacity=capacity, rng=rng, _reservoir=[])

    def offer(self, item: T) -> None:
        """Present `item` to the reservoir.

        The first `capacity` items are accepted verbatim; subsequent
        items replace a uniformly chosen slot with probability
        `capacity/seen`. This implements Vitter's Algorithm R exactly.
        """
        self.seen += 1
        if len(self._reservoir) < self.capacity:
            self._reservoir.append(item)
            return
        # randint(0, seen-1) is inclusive on both ends; replace if the
        # roll lands inside the reservoir window.
        j = self.rng.randint(0, self.seen - 1)
        if j < self.capacity:
            self._reservoir[j] = item

    @property
    def samples(self) -> list[T]:
        """Return a defensive copy of the current reservoir.

        Returning a copy stops downstream consumers from accidentally
        mutating the sampler state via list operations.
        """
        return list(self._reservoir)


async def reservoir_sample(
    stream: AsyncIterator[T],
    *,
    capacity: int = DEFAULT_RESERVOIR_SIZE,
    seed: int | None = None,
) -> list[T]:
    """Drain `stream` through a reservoir sampler and return the sample.

    Convenience wrapper around `ReservoirSampler` for the common path
    where the caller wants the final sample as a list. The discovery
    loop in `s3.py` instead constructs the sampler explicitly so it can
    interleave AIMD rate-limit feedback between pages without losing
    the reservoir state.
    """
    sampler: ReservoirSampler[T] = ReservoirSampler.make(capacity, seed=seed)
    async for item in stream:
        sampler.offer(item)
    return sampler.samples


__all__ = [
    "DEFAULT_RESERVOIR_SIZE",
    "ReservoirSampler",
    "SamplingDecision",
    "reservoir_sample",
    "should_sample",
]
