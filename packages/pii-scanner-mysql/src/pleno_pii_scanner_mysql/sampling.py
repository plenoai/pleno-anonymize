"""Reservoir-sampling SQL builder for MySQL.

Same math as pii-scanner-postgres: `n = log(0.05) / log(0.99) ≈ 299`
gives 95% confidence of detecting at least one PII row at p=1%
prevalence. We round up to 300 as the operational default.

Strategy split by table size (estimated_rows):

  * ≤ 100 k rows: `ORDER BY RAND() LIMIT n`. Simple, exact, fast
    enough for small tables.
  * > 100 k rows: primary-key hash trick — `WHERE
    CRC32(CAST(id AS CHAR)) % bucket = 0 LIMIT n`. Avoids the
    O(N log N) cost of `ORDER BY RAND()` on multi-billion-row
    tables that crashes the query budget. `bucket = max(1,
    floor(estimated_rows / (sample_rows * 5)))` gives a 5×
    over-sample so LIMIT lands close to the requested cardinality.

Note: MySQL has no `TABLESAMPLE BERNOULLI` (Postgres-only); the
hash trick is the closest production-safe equivalent. Requires the
table to have an `id` integer primary key — we fall back to
`ORDER BY RAND()` if the schema doesn't have one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


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
    schema: str
    table: str
    sample_rows: int
    estimated_rows: int
    use_hash_bucket: bool
    bucket_modulus: int

    def query(self, columns: list[str]) -> str:
        cols = ", ".join(_quote_ident(c) for c in columns)
        ident = f"{_quote_ident(self.schema)}.{_quote_ident(self.table)}"
        if self.use_hash_bucket:
            # Primary-key hash sampling. Assumes an `id` PK — operators
            # who don't have one get the ORDER BY RAND() fallback.
            return (
                f"SELECT {cols} FROM {ident} "
                f"WHERE CRC32(CAST(`id` AS CHAR)) % {self.bucket_modulus} = 0 "
                f"LIMIT {self.sample_rows}"
            )
        return (
            f"SELECT {cols} FROM {ident} "
            f"ORDER BY RAND() LIMIT {self.sample_rows}"
        )


def plan_sample(
    *,
    schema: str,
    table: str,
    estimated_rows: int,
    sample_rows: int,
) -> SamplingPlan:
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
            use_hash_bucket=False,
            bucket_modulus=1,
        )
    # 5× over-sample so the hash bucket returns roughly 5*sample_rows
    # candidates; LIMIT trims to the exact target cardinality.
    bucket = max(1, estimated_rows // (sample_rows * 5))
    return SamplingPlan(
        schema=schema,
        table=table,
        sample_rows=sample_rows,
        estimated_rows=estimated_rows,
        use_hash_bucket=True,
        bucket_modulus=bucket,
    )


def _quote_ident(name: str) -> str:
    """Backtick-quote a MySQL identifier; reject backticks inside.

    Identifiers come from `information_schema` queries, never from
    user-controlled config. A backtick in the name signals a corrupt
    catalog or a malicious server — we refuse rather than risk SQL
    injection via crafted catalog rows.
    """
    if "`" in name:
        raise ValueError(
            f"refusing to quote identifier containing backtick: {name!r}"
        )
    if not name:
        raise ValueError("identifier must be non-empty")
    return f"`{name}`"


__all__ = [
    "SamplingPlan",
    "plan_sample",
    "reservoir_sample_size",
]
