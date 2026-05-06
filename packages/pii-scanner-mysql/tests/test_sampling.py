"""Tests for MySQL reservoir-sampling planner."""

from __future__ import annotations

import pytest

from pleno_pii_scanner_mysql.sampling import (
    plan_sample,
    reservoir_sample_size,
)


class TestReservoirSampleSize:
    def test_default_95pct_1pct(self) -> None:
        # Reproduces the ADR §16 reference value.
        assert reservoir_sample_size(confidence=0.95, prevalence=0.01) == 299

    def test_higher_confidence_larger_sample(self) -> None:
        a = reservoir_sample_size(confidence=0.95, prevalence=0.01)
        b = reservoir_sample_size(confidence=0.99, prevalence=0.01)
        assert b > a

    def test_higher_prevalence_smaller_sample(self) -> None:
        a = reservoir_sample_size(confidence=0.95, prevalence=0.01)
        b = reservoir_sample_size(confidence=0.95, prevalence=0.10)
        assert b < a

    def test_invalid_confidence(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            reservoir_sample_size(confidence=0.0, prevalence=0.01)
        with pytest.raises(ValueError, match="confidence"):
            reservoir_sample_size(confidence=1.0, prevalence=0.01)

    def test_invalid_prevalence(self) -> None:
        with pytest.raises(ValueError, match="prevalence"):
            reservoir_sample_size(confidence=0.95, prevalence=0.0)
        with pytest.raises(ValueError, match="prevalence"):
            reservoir_sample_size(confidence=0.95, prevalence=1.0)


class TestPlanSample:
    def test_small_table_uses_order_by_rand(self) -> None:
        p = plan_sample(
            schema="app", table="users", estimated_rows=5_000, sample_rows=300
        )
        assert not p.use_hash_bucket
        sql = p.query(["id", "email"])
        assert "ORDER BY RAND()" in sql
        assert "LIMIT 300" in sql

    def test_large_table_uses_hash_bucket(self) -> None:
        p = plan_sample(
            schema="app",
            table="events",
            estimated_rows=10_000_000,
            sample_rows=300,
        )
        assert p.use_hash_bucket
        # bucket = 10_000_000 / (300 * 5) ≈ 6666
        assert p.bucket_modulus == 6666
        sql = p.query(["id", "payload"])
        assert "CRC32" in sql
        assert "% 6666 = 0" in sql

    def test_bucket_at_least_one(self) -> None:
        p = plan_sample(
            schema="app",
            table="tiny",
            estimated_rows=200_000,
            sample_rows=100_000,
        )
        # 200_000 // 500_000 == 0 → clamp to 1
        assert p.bucket_modulus >= 1

    def test_invalid_estimated_rows(self) -> None:
        with pytest.raises(ValueError, match="estimated_rows"):
            plan_sample(schema="a", table="b", estimated_rows=0, sample_rows=10)

    def test_invalid_sample_rows(self) -> None:
        with pytest.raises(ValueError, match="sample_rows"):
            plan_sample(schema="a", table="b", estimated_rows=10, sample_rows=0)


class TestQuoteIdent:
    def test_backtick_in_name_rejected(self) -> None:
        from pleno_pii_scanner_mysql.sampling import _quote_ident

        with pytest.raises(ValueError, match="backtick"):
            _quote_ident("evil`name")

    def test_empty_rejected(self) -> None:
        from pleno_pii_scanner_mysql.sampling import _quote_ident

        with pytest.raises(ValueError, match="non-empty"):
            _quote_ident("")

    def test_normal_name(self) -> None:
        from pleno_pii_scanner_mysql.sampling import _quote_ident

        assert _quote_ident("users") == "`users`"
