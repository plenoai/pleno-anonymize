"""Process-local TTL + LRU cache for VerificationResult.

The cache key is sha256(secret)[:32] (see base.hash_secret) so the raw
token never lives in cache state. TTL is taken from the result itself
to let providers shorten retention for transient states (error: 60s,
rate_limited: not cached at all by the integration layer).
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta

from .base import VerificationResult


class VerificationCache:
    """Bounded TTL cache with LRU eviction.

    Not threadsafe across asyncio tasks running on different threads;
    the verifier integration funnels access through a single event
    loop, so no lock is needed. Adding a lock would mask cross-thread
    misuse, which is more dangerous than the lock itself is helpful.
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._store: OrderedDict[str, VerificationResult] = OrderedDict()

    def __len__(self) -> int:
        return len(self._store)

    def get(self, value_hash: str) -> VerificationResult | None:
        result = self._store.get(value_hash)
        if result is None:
            return None
        if self._is_expired(result):
            # Expired entries are evicted on read so a long-idle cache
            # eventually drains without a separate sweeper task.
            del self._store[value_hash]
            return None
        self._store.move_to_end(value_hash)
        return result

    def put(self, value_hash: str, result: VerificationResult) -> None:
        if value_hash in self._store:
            self._store.move_to_end(value_hash)
        self._store[value_hash] = result
        while len(self._store) > self._max_entries:
            self._store.popitem(last=False)

    def invalidate(self, value_hash: str) -> None:
        self._store.pop(value_hash, None)

    def clear(self) -> None:
        self._store.clear()

    @staticmethod
    def _is_expired(result: VerificationResult) -> bool:
        if result.ttl_seconds <= 0:
            return True
        deadline = result.checked_at + timedelta(seconds=result.ttl_seconds)
        return datetime.now(UTC) >= deadline
