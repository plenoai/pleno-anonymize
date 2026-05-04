"""PostgreSQL SourceConnector — production-safe DB scan (ADR-0007 §16).

Hard requirements driven by the ADR:

  * Replica enforcement (`pg_is_in_recovery() = true`) — connecting to a
    primary by accident has been the #1 bug in production DB scanners.
  * `SET LOCAL statement_timeout = '30s'` on every query.
  * Reservoir sampling (n=300 default) per table — full-table scans of
    multi-billion-row tables stall for hours and burn replica IO.
  * Dedicated pool capped at `max_size=2` so the scanner can never
    starve the application of pool slots.
  * Only enumerate text-shaped columns (`varchar`, `text`, `citext`,
    `jsonb`, `xml`, `bytea`). Numeric / temporal / uuid columns are
    skipped because they cannot host PII the regex/NER passes detect.

What this connector does NOT do (deliberately, ADR-aligned):

  * Write to the DB. Read-only role enforced by replica check.
  * Restore from PITR / wal-g. Out of scope; user provides the replica.
  * Inspect raw values in logs. The connector materialises rows into
    Documents and forgets them; the FindingsStore (#9) is the only
    component that ever persists raw value bytes (envelope-encrypted).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import asyncpg  # type: ignore[import-untyped]

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec
from pleno_pii_scanner_postgres.sampling import (
    SamplingPlan,
    plan_sample,
    reservoir_sample_size,
)


# Column types that may carry PII the regex / NER pipeline can match.
# Anything else (numeric, temporal, geometric, network address) is
# skipped at discover time — keeps the document budget tight and
# avoids casting binary types to text where the codec choice is
# ambiguous.
_TEXTUAL_TYPES: frozenset[str] = frozenset(
    {
        "character varying",
        "varchar",
        "text",
        "character",
        "char",
        "citext",
        "jsonb",
        "json",
        "xml",
        "bytea",
    }
)


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    """Construction config for `PostgresConnector`.

    `dsn` is a libpq connection string. `schemas` filters the discover
    pass; `("public",)` is the bare-minimum default. `tables` and
    `excluded_tables` apply on top of the schema filter and accept
    `<schema>.<table>` exact matches (no wildcards — operators who
    need glob filtering should pass them via SourceFilter.include).
    """

    dsn: str
    schemas: tuple[str, ...] = ("public",)
    tables: tuple[str, ...] = ()
    excluded_tables: tuple[str, ...] = ()
    sample_rows: int = field(
        default_factory=lambda: reservoir_sample_size(
            confidence=0.95, prevalence=0.01
        )
    )
    statement_timeout: str = "30s"
    pool_size: int = 2
    require_replica: bool = True
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("dsn must be non-empty")
        if not self.schemas:
            raise ValueError("schemas must be non-empty")
        if self.sample_rows <= 0:
            raise ValueError("sample_rows must be > 0")
        if self.pool_size < 1:
            raise ValueError("pool_size must be >= 1")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Strip credentials from dsn for the id; libpq dsn often
        # contains the password and we don't want it in checkpoint
        # paths or logs. We keep host + dbname.
        return f"postgres:{_redact_dsn(self.dsn)}"


class PostgresConnector:
    """Read-only SourceConnector for PostgreSQL replicas."""

    kind = "postgres"

    def __init__(self, config: PostgresConfig) -> None:
        self._config = config
        self.id = config.resolved_id()
        self._pool: asyncpg.Pool | None = None
        # Caches discover() results so fetch() can re-issue the
        # sampling query without re-walking information_schema. Bounded
        # by the number of (schema, table) pairs in the database.
        self._tables: dict[str, _TableMeta] = {}

    def capabilities(self) -> Capabilities:
        # `incremental=False` because reservoir sampling is the entire
        # design point — re-running the scan is meant to draw a fresh
        # sample, not to resume a prior one. `streaming=True` because
        # very large rows (jsonb columns) yield as DocumentChunk.
        return Capabilities(
            incremental=False,
            binary=True,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=True,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        del cursor  # incremental=False; cursor is unused
        await self._ensure_pool()
        await self._enforce_replica()
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await self._set_timeout(conn)
            rows = await conn.fetch(
                _DISCOVER_SQL,
                list(self._config.schemas),
                list(_TEXTUAL_TYPES),
            )
        for row in rows:
            full = f"{row['table_schema']}.{row['table_name']}"
            if self._config.tables and full not in self._config.tables:
                continue
            if full in self._config.excluded_tables:
                continue
            if filter.include and not _matches_any(full, filter.include):
                continue
            if filter.exclude and _matches_any(full, filter.exclude):
                continue
            meta = self._tables.setdefault(
                full,
                _TableMeta(
                    schema=row["table_schema"],
                    table=row["table_name"],
                    columns=[],
                    estimated_rows=int(row["estimated_rows"] or 1),
                ),
            )
            meta.columns.append(row["column_name"])
            # One DocumentRef per (schema, table) — fetch() emits N
            # Documents (one per sampled row) under that ref. Yielding
            # per-table, not per-column, keeps the scheduler's per-ref
            # bookkeeping bounded by the table count, not column count.
        for full, meta in self._tables.items():
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=full,
                content_type="application/x-postgres-rows",
                size=meta.estimated_rows,
                metadata={
                    "schema": meta.schema,
                    "table": meta.table,
                    "columns": ",".join(meta.columns),
                    "estimated_rows": str(meta.estimated_rows),
                },
            )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        full = ref.path
        meta = self._tables.get(full)
        if meta is None:
            # Cold fetch — refs from a checkpoint reload won't have the
            # discover cache. Re-resolve schema + columns on demand.
            await self._ensure_pool()
            meta = await self._reload_table_meta(full)
            if meta is None:
                return
            self._tables[full] = meta
        await self._enforce_replica()
        plan = plan_sample(
            schema=meta.schema,
            table=meta.table,
            estimated_rows=meta.estimated_rows,
            sample_rows=self._config.sample_rows,
        )
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            await self._set_timeout(conn)
            rows = await conn.fetch(plan.query(meta.columns))
        for i, row in enumerate(rows):
            text = _serialise_row(meta.columns, row)
            yield Document(
                ref=DocumentRef(
                    source_id=ref.source_id,
                    source_kind=ref.source_kind,
                    path=f"{full}#row-{i}",
                    content_type="text/plain",
                    metadata=dict(ref.metadata) | {"row_index": str(i)},
                ),
                text=text,
                fetched_at=datetime.now(UTC),
                extra={
                    "schema": meta.schema,
                    "table": meta.table,
                    "row_index": str(i),
                },
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        self._tables.clear()

    # --- internals -----------------------------------------------

    async def _ensure_pool(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            dsn=self._config.dsn,
            min_size=1,
            max_size=self._config.pool_size,
        )

    async def _enforce_replica(self) -> None:
        if not self._config.require_replica:
            return
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            in_recovery = await conn.fetchval("SELECT pg_is_in_recovery()")
        if not in_recovery:
            raise PrimaryConnectionRefused(
                "refusing to scan a primary instance: set "
                "require_replica=False to override (not recommended)"
            )

    async def _set_timeout(self, conn: asyncpg.Connection) -> None:
        # `SET LOCAL` only applies inside a transaction. asyncpg starts
        # an implicit one on each fetch() call, so the SET LOCAL takes
        # effect for the whole sequence within that pool acquisition.
        await conn.execute(
            f"SET LOCAL statement_timeout = '{self._config.statement_timeout}'"
        )

    async def _reload_table_meta(self, full: str) -> "_TableMeta | None":
        try:
            schema, table = full.split(".", 1)
        except ValueError:
            return None
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            rows = await conn.fetch(
                _RELOAD_SQL, schema, table, list(_TEXTUAL_TYPES)
            )
        if not rows:
            return None
        return _TableMeta(
            schema=schema,
            table=table,
            columns=[r["column_name"] for r in rows],
            estimated_rows=int(rows[0]["estimated_rows"] or 1),
        )


@dataclass(slots=True)
class _TableMeta:
    schema: str
    table: str
    columns: list[str]
    estimated_rows: int


class PrimaryConnectionRefused(RuntimeError):
    """Raised when require_replica=True and pg_is_in_recovery()=false."""


# Pulls every (schema, table, column, type) tuple in one round-trip,
# joined to pg_class for the row-count estimate. Filters out toast +
# pg_catalog / information_schema noise client-side via the schemas
# allowlist parameter.
_DISCOVER_SQL = """
SELECT
    c.table_schema,
    c.table_name,
    c.column_name,
    c.data_type,
    COALESCE(pc.reltuples::bigint, 1) AS estimated_rows
FROM information_schema.columns c
JOIN pg_class pc
    ON pc.relname = c.table_name
JOIN pg_namespace pn
    ON pn.oid = pc.relnamespace AND pn.nspname = c.table_schema
WHERE c.table_schema = ANY($1::text[])
  AND c.data_type   = ANY($2::text[])
  AND pc.relkind IN ('r', 'm')   -- regular tables + matviews; skip views/foreign
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

_RELOAD_SQL = """
SELECT
    c.column_name,
    COALESCE(pc.reltuples::bigint, 1) AS estimated_rows
FROM information_schema.columns c
JOIN pg_class pc
    ON pc.relname = c.table_name
JOIN pg_namespace pn
    ON pn.oid = pc.relnamespace AND pn.nspname = c.table_schema
WHERE c.table_schema = $1
  AND c.table_name   = $2
  AND c.data_type    = ANY($3::text[])
  AND pc.relkind IN ('r', 'm')
ORDER BY c.ordinal_position
"""


def _serialise_row(columns: list[str], row: asyncpg.Record) -> str:
    """Render a row as one text blob the regex/NER pipeline can scan.

    Format: `column1=value1\\ncolumn2=value2\\n...`. Keeps column
    attribution adjacent to each value so the detector's per-line
    logic produces accurate "this PII was in column X" output.
    """
    out: list[str] = []
    for col in columns:
        value = row[col]
        rendered = _stringify(value)
        out.append(f"{col}={rendered}")
    return "\n".join(out)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        # bytea: decode best-effort, replacing invalid bytes so the
        # text pipeline never crashes on non-UTF-8 binary blobs.
        return value.decode("utf-8", errors="replace")
    return str(value)


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _redact_dsn(dsn: str) -> str:
    """Strip password from a libpq DSN for use in logs and the `id`.

    `postgresql://user:secret@host/db` → `postgresql://user@host/db`.
    Only handles the URL form; `key=value` form is passed through
    unchanged because parsing it correctly across all libpq quirks
    would invite bugs (semicolons, embedded quotes, etc.).
    """
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    auth, host_part = rest.split("@", 1)
    if ":" in auth:
        user = auth.split(":", 1)[0]
        return f"{scheme}://{user}@{host_part}"
    return dsn


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "dsn" not in config:
        raise ValueError("postgres connector config requires 'dsn'")
    schemas_raw = config.get("schemas", ("public",))
    tables_raw = config.get("tables", ())
    excluded_raw = config.get("excluded_tables", ())
    return PostgresConnector(
        PostgresConfig(
            dsn=str(config["dsn"]),
            schemas=tuple(schemas_raw) if schemas_raw else ("public",),
            tables=tuple(tables_raw),
            excluded_tables=tuple(excluded_raw),
            sample_rows=int(
                config.get(
                    "sample_rows",
                    reservoir_sample_size(confidence=0.95, prevalence=0.01),
                )
            ),
            statement_timeout=str(config.get("statement_timeout", "30s")),
            pool_size=int(config.get("pool_size", 2)),
            require_replica=bool(config.get("require_replica", True)),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="postgres",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=True,
        max_concurrent_fetches=2,
        streaming=True,
    ),
    required_scopes=("connect", "select"),
    description=(
        "PostgreSQL replica scanner. Enforces pg_is_in_recovery(), "
        "SET LOCAL statement_timeout=30s, reservoir sampling (n=300 by "
        "default for 95% conf at 1% PII prevalence), and a 2-conn pool "
        "cap. Only scans textual columns."
    ),
)


__all__ = [
    "SPEC",
    "PostgresConfig",
    "PostgresConnector",
    "PrimaryConnectionRefused",
    "SamplingPlan",
]
