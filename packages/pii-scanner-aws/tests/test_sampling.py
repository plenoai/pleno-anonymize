"""Unit tests for the reservoir sampling module."""

from __future__ import annotations


import pytest

from pleno_pii_scanner_aws.sampling import (
    DEFAULT_RESERVOIR_SIZE,
    ReservoirSampler,
    SamplingDecision,
    reservoir_sample,
    should_sample,
)


class TestShouldSample:
    def test_default_size_matches_adr(self) -> None:
        # ADR-0007 §16: 95% confidence at p=1% rounds to 300.
        assert DEFAULT_RESERVOIR_SIZE == 300

    def test_below_threshold_disables(self) -> None:
        d = should_sample(500_000)
        assert d == SamplingDecision(False, 300, d.reason)
        assert "<= 1000000" in d.reason

    def test_above_threshold_enables(self) -> None:
        d = should_sample(2_000_000)
        assert d.enabled is True
        assert d.reservoir_size == 300

    def test_unknown_size_disables_default(self) -> None:
        # ADR-0007 §16 default: do not sample silently. Operator opts in
        # via estimated_object_count or forced=True.
        d = should_sample(None)
        assert d.enabled is False
        assert "unannotated" in d.reason

    def test_forced_true_overrides_small_bucket(self) -> None:
        d = should_sample(10, forced=True)
        assert d.enabled is True
        assert "forced=True" in d.reason

    def test_forced_false_overrides_huge_bucket(self) -> None:
        d = should_sample(10**12, forced=False)
        assert d.enabled is False
        assert "forced=False" in d.reason

    def test_invalid_reservoir_size(self) -> None:
        with pytest.raises(ValueError):
            should_sample(100, reservoir_size=0)

    def test_invalid_threshold(self) -> None:
        with pytest.raises(ValueError):
            should_sample(100, threshold=0)


class TestReservoirSampler:
    def test_keeps_all_when_under_capacity(self) -> None:
        s: ReservoirSampler[int] = ReservoirSampler.make(5, seed=0)
        for i in range(3):
            s.offer(i)
        assert sorted(s.samples) == [0, 1, 2]
        assert s.seen == 3

    def test_caps_at_capacity(self) -> None:
        s: ReservoirSampler[int] = ReservoirSampler.make(3, seed=0)
        for i in range(100):
            s.offer(i)
        assert len(s.samples) == 3
        assert s.seen == 100
        # All items must come from the input population.
        assert all(0 <= v < 100 for v in s.samples)

    def test_deterministic_with_seed(self) -> None:
        a: ReservoirSampler[int] = ReservoirSampler.make(5, seed=42)
        b: ReservoirSampler[int] = ReservoirSampler.make(5, seed=42)
        for i in range(50):
            a.offer(i)
            b.offer(i)
        assert a.samples == b.samples

    def test_invalid_capacity(self) -> None:
        with pytest.raises(ValueError):
            ReservoirSampler.make(0)

    def test_samples_returns_copy(self) -> None:
        s: ReservoirSampler[int] = ReservoirSampler.make(3, seed=1)
        for i in range(3):
            s.offer(i)
        view = s.samples
        view.append(999)
        assert 999 not in s.samples

    def test_default_construction_uses_systemrandom(self) -> None:
        # No seed → SystemRandom path; just verify it does not raise and
        # produces a different state than a seeded sampler with high
        # probability.
        s: ReservoirSampler[int] = ReservoirSampler.make(3)
        for i in range(20):
            s.offer(i)
        assert len(s.samples) == 3

    def test_uniform_distribution(self) -> None:
        # Sanity: over many trials, every input has a roughly equal
        # chance of ending up in a 1-slot reservoir. Loose tolerance to
        # keep the test fast.
        counts = [0] * 5
        for trial in range(2000):
            s: ReservoirSampler[int] = ReservoirSampler.make(1, seed=trial)
            for i in range(5):
                s.offer(i)
            counts[s.samples[0]] += 1
        for c in counts:
            assert 300 < c < 500, counts


async def _async_iter(items):
    for x in items:
        yield x


class TestReservoirSampleHelper:
    async def test_drains_async_iterator(self) -> None:
        out = await reservoir_sample(_async_iter(range(10)), capacity=4, seed=7)
        assert len(out) == 4
        assert all(0 <= v < 10 for v in out)

    async def test_returns_all_when_under_capacity(self) -> None:
        out = await reservoir_sample(_async_iter([1, 2, 3]), capacity=10, seed=1)
        assert sorted(out) == [1, 2, 3]
