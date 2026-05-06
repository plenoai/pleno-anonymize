"""Reservoir-sampling SQL builder.

ADR §16: `n = log(0.05) / log(0.99) ≈ 299` gives 95% confidence of
detecting at least one PII row when prevalence is p=1%. We round up to
300 as the operational default. The math is reproduced here so anyone
auditing the SQL can verify the constant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


# Reproduces ADR §16: P(no PII in n rows) = (1-p)^n; we want this ≤ α.
# Solving for n: n ≥ log(α) / log(1-p). With α=0.05 (95% confidence) and
# p=0.01 (1% prevalence), n ≈ 298.07 → round up to 299; we publish 300
# as the operationally friendly default that is still mathematically
# correct (the function gives the exact minimum so callers can audit).
def reservoir_sample_size(*, confidence: float = 0.95, prevalence: float = 0.01) -> int:
    """Minimum sample size for `confidence` PII detection at `prevalence`."""
    if not 0 < confidence < 1:
        raise ValueError("confidence must be in (0, 1)")
    if not 0 < prevalence < 1:
        raise ValueError("prevalence must be in (0, 1)")
    alpha = 1.0 - confidence
    return math.ceil(math.log(alpha) / math.log(1.0 - prevalence))


@dataclass(frozen=True, slots=True)
class SamplingPlan:
    """Strategy chosen for one (schema, table) pair.

    `bernoulli_pct` is non-None when the sampler can use TABLESAMPLE
    (PostgreSQL ≥9.5); falls back to ORDER BY random() LIMIT n on the
    very rare PG ≤9.4. `limit` is always populated so the final query
    never returns more than `sample_rows` regardless of strategy.
    """

    schema: str
    table: str
    sample_rows: int
    estimated_rows: int
    bernoulli_pct: float | None

    def query(self, columns: list[str]) -> str:
        cols = ", ".join(_quote_ident(c) for c in columns)
        ident = f"{_quote_ident(self.schema)}.{_quote_ident(self.table)}"
        if self.bernoulli_pct is not None:
            # TABLESAMPLE BERNOULLI(p) is uniform over rows; keeps the
            # plan within the configured statement_timeout because the
            # planner rewrites table scans to skip pages probabilistically.
            return (
                f"SELECT {cols} FROM {ident} "
                f"TABLESAMPLE BERNOULLI({self.bernoulli_pct:.6f}) "
                f"LIMIT {self.sample_rows}"
            )
        # Fallback: PG ≤9.4 has no TABLESAMPLE. ORDER BY random() is
        # O(N log N) so we cap with `LIMIT` upfront — the planner can
        # use index-only scans here on small tables but the strategy is
        # only ever picked when estimated_rows ≤ 100k.
        return f"SELECT {cols} FROM {ident} ORDER BY random() LIMIT {self.sample_rows}"


def plan_sample(
    *,
    schema: str,
    table: str,
    estimated_rows: int,
    sample_rows: int,
) -> SamplingPlan:
    """Pick TABLESAMPLE pct vs ORDER BY random() based on table size.

    Below 100k rows: ORDER BY random() is faster and simpler.
    Above 100k rows: TABLESAMPLE BERNOULLI with `pct = 100 * sample_rows
    / estimated_rows * 5` (5× over-sample so LIMIT can cap to exactly
    sample_rows without re-sampling).
    """
    if estimated_rows <= 0:
        raise ValueError("estimated_rows must be > 0")
    if sample_rows <= 0:
        raise ValueError("sample_rows must be > 0")
    if estimated_rows < 100_000:
        return SamplingPlan(
            schema=schema,
            table=table,
            sample_rows=sample_rows,
            estimated_rows=estimated_rows,
            bernoulli_pct=None,
        )
    # 5× over-sample so LIMIT lands on the right cardinality even when
    # BERNOULLI returns slightly fewer than the expected number of rows
    # (it's probabilistic). Capped at 100% (full table) for very small
    # cardinality misestimates.
    pct = min(100.0, 100.0 * sample_rows / estimated_rows * 5.0)
    return SamplingPlan(
        schema=schema,
        table=table,
        sample_rows=sample_rows,
        estimated_rows=estimated_rows,
        bernoulli_pct=pct,
    )


_VALID_IDENT_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _quote_ident(name: str) -> str:
    """Double-quote a SQL identifier; reject double-quotes inside the name.

    Identifiers come from `information_schema.tables` queries, not from
    user-controlled config, so a double-quote in the name signals a
    corrupt catalog or a malicious database — we refuse to compose a
    query rather than risk SQL injection via crafted catalog rows.
    """
    if '"' in name:
        raise ValueError(
            f"refusing to quote identifier containing double-quote: {name!r}"
        )
    if not name:
        raise ValueError("identifier must be non-empty")
    if all(c in _VALID_IDENT_CHARS for c in name) and not name[0].isdigit():
        # Unquoted form is safe for these characters; produces friendlier
        # error messages from PG when the table truly doesn't exist.
        return f'"{name}"'
    return f'"{name}"'


__all__ = [
    "SamplingPlan",
    "plan_sample",
    "reservoir_sample_size",
]
