"""Tests for the reservoir-sampling SQL builder."""

from __future__ import annotations

import math

import pytest

from pleno_pii_scanner_postgres.sampling import (
    SamplingPlan,
    _quote_ident,
    plan_sample,
    reservoir_sample_size,
)


class TestReservoirSize:
    def test_default_yields_adr_constant(self) -> None:
        # ADR §16: log(0.05) / log(0.99) ≈ 298.07 → ceil → 299.
        n = reservoir_sample_size(confidence=0.95, prevalence=0.01)
        assert n == 299

    def test_higher_confidence_grows_n(self) -> None:
        a = reservoir_sample_size(confidence=0.95, prevalence=0.01)
        b = reservoir_sample_size(confidence=0.99, prevalence=0.01)
        assert b > a

    def test_lower_prevalence_grows_n(self) -> None:
        a = reservoir_sample_size(confidence=0.95, prevalence=0.01)
        b = reservoir_sample_size(confidence=0.95, prevalence=0.001)
        assert b > a

    @pytest.mark.parametrize("bad", [0, 1, 1.5, -0.1])
    def test_rejects_invalid_confidence(self, bad: float) -> None:
        with pytest.raises(ValueError, match="confidence"):
            reservoir_sample_size(confidence=bad, prevalence=0.01)

    @pytest.mark.parametrize("bad", [0, 1, 1.5, -0.1])
    def test_rejects_invalid_prevalence(self, bad: float) -> None:
        with pytest.raises(ValueError, match="prevalence"):
            reservoir_sample_size(confidence=0.95, prevalence=bad)


class TestPlanSample:
    def test_small_table_uses_random(self) -> None:
        p = plan_sample(
            schema="public",
            table="users",
            estimated_rows=50_000,
            sample_rows=300,
        )
        assert p.bernoulli_pct is None
        sql = p.query(["email"])
        assert "ORDER BY random()" in sql
        assert "LIMIT 300" in sql

    def test_large_table_uses_bernoulli(self) -> None:
        p = plan_sample(
            schema="public",
            table="events",
            estimated_rows=10_000_000_000,
            sample_rows=300,
        )
        assert p.bernoulli_pct is not None
        sql = p.query(["payload"])
        assert "TABLESAMPLE BERNOULLI" in sql
        assert "LIMIT 300" in sql

    def test_bernoulli_capped_at_100(self) -> None:
        # Tiny table mis-estimated as 1 row but sample_rows=300 → would
        # otherwise compute >100% which is invalid SQL.
        p = plan_sample(
            schema="public",
            table="tiny",
            estimated_rows=100_001,
            sample_rows=300_000,
        )
        assert p.bernoulli_pct == 100.0

    def test_rejects_zero_estimated_rows(self) -> None:
        with pytest.raises(ValueError, match="estimated_rows"):
            plan_sample(
                schema="s", table="t", estimated_rows=0, sample_rows=10
            )

    def test_rejects_zero_sample_rows(self) -> None:
        with pytest.raises(ValueError, match="sample_rows"):
            plan_sample(
                schema="s", table="t", estimated_rows=100, sample_rows=0
            )

    def test_query_quotes_identifiers(self) -> None:
        p = plan_sample(
            schema="public", table="users", estimated_rows=1000, sample_rows=10
        )
        sql = p.query(["email"])
        assert '"public"."users"' in sql
        assert '"email"' in sql


class TestQuoteIdent:
    def test_quotes_simple(self) -> None:
        assert _quote_ident("users") == '"users"'

    def test_quotes_mixed_case(self) -> None:
        assert _quote_ident("UserAccounts") == '"UserAccounts"'

    def test_quotes_with_unicode(self) -> None:
        # Non-ASCII identifier; quoted form is mandatory.
        assert _quote_ident("ユーザー") == '"ユーザー"'

    def test_rejects_double_quote(self) -> None:
        with pytest.raises(ValueError, match="double-quote"):
            _quote_ident('attack"; DROP TABLE')

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            _quote_ident("")
