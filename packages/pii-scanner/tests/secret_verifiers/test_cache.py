from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pleno_pii_scanner.secret_verifiers.base import VerificationResult
from pleno_pii_scanner.secret_verifiers.cache import VerificationCache


def _result(*, ttl: int = 3600, state: str = "live", at: datetime | None = None) -> VerificationResult:
    return VerificationResult(
        state=state,  # type: ignore[arg-type]
        detail="",
        ttl_seconds=ttl,
        checked_at=at if at is not None else datetime.now(UTC),
    )


def test_put_then_get_returns_value() -> None:
    cache = VerificationCache()
    result = _result()
    cache.put("k", result)
    assert cache.get("k") is result


def test_get_unknown_key_returns_none() -> None:
    cache = VerificationCache()
    assert cache.get("missing") is None


def test_expired_entry_is_evicted_on_read() -> None:
    cache = VerificationCache()
    expired = _result(ttl=10, at=datetime.now(UTC) - timedelta(seconds=20))
    cache.put("k", expired)
    assert cache.get("k") is None
    assert len(cache) == 0


def test_zero_ttl_is_always_expired() -> None:
    cache = VerificationCache()
    cache.put("k", _result(ttl=0))
    assert cache.get("k") is None


def test_invalidate_drops_entry() -> None:
    cache = VerificationCache()
    cache.put("k", _result())
    cache.invalidate("k")
    assert cache.get("k") is None


def test_invalidate_unknown_key_is_noop() -> None:
    cache = VerificationCache()
    cache.invalidate("missing")
    assert len(cache) == 0


def test_clear_drops_all_entries() -> None:
    cache = VerificationCache()
    cache.put("a", _result())
    cache.put("b", _result())
    cache.clear()
    assert len(cache) == 0


def test_lru_eviction_when_max_entries_reached() -> None:
    cache = VerificationCache(max_entries=2)
    cache.put("a", _result())
    cache.put("b", _result())
    cache.put("c", _result())
    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None


def test_get_promotes_entry_to_most_recently_used() -> None:
    cache = VerificationCache(max_entries=2)
    cache.put("a", _result())
    cache.put("b", _result())
    cache.get("a")
    cache.put("c", _result())
    assert cache.get("a") is not None
    assert cache.get("b") is None


def test_put_existing_key_refreshes_recency() -> None:
    cache = VerificationCache(max_entries=2)
    cache.put("a", _result())
    cache.put("b", _result())
    cache.put("a", _result())
    cache.put("c", _result())
    assert cache.get("a") is not None
    assert cache.get("b") is None


def test_max_entries_must_be_positive() -> None:
    with pytest.raises(ValueError):
        VerificationCache(max_entries=0)
