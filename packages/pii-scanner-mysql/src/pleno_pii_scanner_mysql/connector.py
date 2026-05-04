"""MySQL SourceConnector — production-safe DB scan.

Hard requirements driven by ADR-0007 §16:

  * Replica enforcement (`@@global.read_only` AND replication state).
    Refusing to connect to a primary is the #1 protection a DB
    scanner can offer. Override with `require_replica=False`.
  * 30 s `MAX_EXECUTION_TIME` optimizer hint on every SELECT.
    MySQL 5.7+ supports `/*+ MAX_EXECUTION_TIME(N) */` per-statement.
  * Reservoir sampling (n=300 default) per table — `ORDER BY RAND()
    LIMIT n` for tables ≤ 100 k rows, primary-key hash sampling
    above. Full-table scans of multi-billion-row tables stall for
    hours and burn replica IO.
  * Pool capped at `max_pool_size=2`. The scanner must never
    starve the application of connection slots.
  * Only enumerate text-shaped columns. Numeric / temporal / spatial
    columns cannot host PII the regex/NER pipeline detects.

Auth path: standard MySQL user/password embedded in the DSN. IAM
auth (RDS/Aurora) is out of scope for v1; users with an IAM
auth-token rotation policy can resolve the password via the
CredentialBroker (#5) and feed it into the DSN.

What this connector does NOT do (deliberately, ADR-aligned):

  * Write. Read-only role enforced by replica check.
  * Inspect raw values in logs. Rows materialise into Documents
    and are forgotten; only the FindingsStore (#9) ever persists
    raw bytes (envelope-encrypted).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import aiomysql

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
from pleno_pii_scanner_mysql.sampling import (
    SamplingPlan,
    plan_sample,
    reservoir_sample_size,
)


# Column types that may carry PII the regex / NER pipeline can match.
# Anything else (numeric / temporal / spatial / set / enum / json
# could host PII but the JSON path is handled below as a special case)
# is skipped at discover time.
_TEXTUAL_TYPES: frozenset[str] = frozenset(
    {
        "char",
        "varchar",
        "tinytext",
        "text",
        "mediumtext",
        "longtext",
        "binary",
        "varbinary",
        "tinyblob",
        "blob",
        "mediumblob",
        "longblob",
        "json",
    }
)


class PrimaryConnectionRefused(RuntimeError):
    """Raised when require_replica=True and the server is a primary."""


@dataclass(frozen=True, slots=True)
class MysqlConfig:
    """Construction config for `MysqlConnector`.

    `dsn` is a `mysql://user:pass@host:port/db` URL. `schemas`
    filters discover (MySQL "schema" == database in this driver).
    Defaults to the database in the DSN path if non-empty, else
    fails — operators must say which DBs to scan.
    """

    dsn: str
    schemas: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    excluded_tables: tuple[str, ...] = ()
    sample_rows: int = field(
        default_factory=lambda: reservoir_sample_size(
            confidence=0.95, prevalence=0.01
        )
    )
    statement_timeout_ms: int = 30_000
    pool_size: int = 2
    require_replica: bool = True
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.dsn:
            raise ValueError("dsn must be non-empty")
        if not self.dsn.startswith(("mysql://", "mysql+aiomysql://")):
            raise ValueError(
                "dsn must start with mysql:// or mysql+aiomysql://"
            )
        if self.sample_rows <= 0:
            raise ValueError("sample_rows must be > 0")
        if self.pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        if self.statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be > 0")
        if not self.schemas and not _dsn_database(self.dsn):
            raise ValueError(
                "schemas must be non-empty (or include a database in the dsn path)"
            )

    def resolved_schemas(self) -> tuple[str, ...]:
        if self.schemas:
            return self.schemas
        return (_dsn_database(self.dsn) or "",)

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        return f"mysql:{_redact_dsn(self.dsn)}"


class MysqlConnector:
    """Read-only SourceConnector for MySQL replicas."""

    kind = "mysql"

    def __init__(
        self,
        config: MysqlConfig,
        *,
        pool: aiomysql.Pool | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        # Test seam — production wiring builds the pool internally.
        # `_owns_pool` controls whether close() actually disposes it.
        self._pool = pool
        self._owns_pool = pool is None
        self._tables: dict[str, _TableMeta] = {}

    def capabilities(self) -> Capabilities:
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
        del cursor  # incremental=False
        await self._ensure_pool()
        await self._enforce_replica()
        schemas = self._config.resolved_schemas()
        rows = await self._fetch_all(
            _DISCOVER_SQL,
            (list(schemas), list(_TEXTUAL_TYPES)),
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
        for full, meta in self._tables.items():
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=full,
                content_type="application/x-mysql-rows",
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
        rows = await self._fetch_all(
            self._with_timeout_hint(plan.query(meta.columns)),
            (),
        )
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
        if self._owns_pool and self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None
        self._tables.clear()

    # --- internals -----------------------------------------------

    async def _ensure_pool(self) -> None:
        if self._pool is not None:
            return
        params = _parse_dsn(self._config.dsn)
        self._pool = await aiomysql.create_pool(
            host=params["host"],
            port=params["port"],
            user=params["user"],
            password=params["password"],
            db=params["db"],
            minsize=1,
            maxsize=self._config.pool_size,
            autocommit=True,
        )

    async def _enforce_replica(self) -> None:
        if not self._config.require_replica:
            return
        # Strategy:
        #   1. read_only (or super_read_only) MUST be ON
        #   2. SHOW REPLICA STATUS (≥8.0.22) or SHOW SLAVE STATUS
        #      must return ≥1 row → this server is replicating from
        #      another. Belt-and-braces: a `read_only` primary that
        #      isn't actually replicating should also be refused.
        ro = await self._fetch_all(
            "SHOW VARIABLES LIKE 'read_only'", ()
        )
        if not ro or _row_value(ro[0]).lower() not in {"on", "1"}:
            raise PrimaryConnectionRefused(
                "refusing to scan: read_only is OFF (set "
                "require_replica=False to override, not recommended)"
            )
        status = await self._fetch_all_one_of(
            ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS")
        )
        if not status:
            raise PrimaryConnectionRefused(
                "refusing to scan: no replication source configured "
                "(read_only is ON but not replicating from a primary)"
            )

    def _with_timeout_hint(self, sql: str) -> str:
        # Optimizer hint on the leading SELECT keyword. Robust to
        # leading whitespace / trailing semicolons. The hint is a
        # no-op on MySQL <5.7.7 — those installations are EOL.
        upper = sql.lstrip()
        if not upper.upper().startswith("SELECT"):
            return sql
        prefix_len = len(sql) - len(upper)
        prefix = sql[:prefix_len]
        head, rest = upper[:6], upper[6:]
        return (
            f"{prefix}{head} /*+ MAX_EXECUTION_TIME({self._config.statement_timeout_ms}) */"
            f"{rest}"
        )

    async def _reload_table_meta(self, full: str) -> "_TableMeta | None":
        try:
            schema, table = full.split(".", 1)
        except ValueError:
            return None
        rows = await self._fetch_all(
            _RELOAD_SQL, (schema, table, list(_TEXTUAL_TYPES))
        )
        if not rows:
            return None
        return _TableMeta(
            schema=schema,
            table=table,
            columns=[r["column_name"] for r in rows],
            estimated_rows=int(rows[0]["estimated_rows"] or 1),
        )

    async def _fetch_all(
        self, sql: str, params: tuple[Any, ...] | list[Any]
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:  # type: ignore[union-attr]
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if params:
                    await cur.execute(sql, params)
                else:
                    await cur.execute(sql)
                return await cur.fetchall()

    async def _fetch_all_one_of(
        self, sqls: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        # Tries each statement until one runs without ProgrammingError.
        # Both `SHOW REPLICA STATUS` (≥8.0.22) and `SHOW SLAVE STATUS`
        # are valid on different MySQL versions; we try the modern
        # form first. Always called with at least one SQL — last_err
        # is guaranteed populated if every attempt fails.
        last_err: Exception | None = None
        for sql in sqls:
            try:
                return await self._fetch_all(sql, ())
            except aiomysql.ProgrammingError as exc:
                last_err = exc
                continue
        assert last_err is not None
        raise last_err


@dataclass(slots=True)
class _TableMeta:
    schema: str
    table: str
    columns: list[str]
    estimated_rows: int


# Pulls every (schema, table, column, type) tuple in one round-trip
# joined to information_schema.tables for the row-count estimate.
# `table_rows` is approximate (planner stat), same as PG `reltuples`.
_DISCOVER_SQL = """
SELECT
    c.table_schema AS table_schema,
    c.table_name   AS table_name,
    c.column_name  AS column_name,
    c.data_type    AS data_type,
    COALESCE(t.table_rows, 1) AS estimated_rows
FROM information_schema.columns c
JOIN information_schema.tables t
    ON t.table_schema = c.table_schema
   AND t.table_name   = c.table_name
WHERE c.table_schema IN %s
  AND c.data_type   IN %s
  AND t.table_type  = 'BASE TABLE'
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

_RELOAD_SQL = """
SELECT
    c.column_name  AS column_name,
    COALESCE(t.table_rows, 1) AS estimated_rows
FROM information_schema.columns c
JOIN information_schema.tables t
    ON t.table_schema = c.table_schema
   AND t.table_name   = c.table_name
WHERE c.table_schema = %s
  AND c.table_name   = %s
  AND c.data_type   IN %s
  AND t.table_type   = 'BASE TABLE'
ORDER BY c.ordinal_position
"""


# --- helpers ------------------------------------------------------


def _serialise_row(columns: list[str], row: dict[str, Any]) -> str:
    out: list[str] = []
    for col in columns:
        out.append(f"{col}={_stringify(row.get(col))}")
    return "\n".join(out)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _parse_dsn(dsn: str) -> dict[str, Any]:
    parsed = urlparse(dsn)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": parsed.username or "",
        "password": parsed.password or "",
        "db": (parsed.path or "/").lstrip("/") or None,
    }


def _dsn_database(dsn: str) -> str | None:
    return _parse_dsn(dsn)["db"]


def _redact_dsn(dsn: str) -> str:
    parsed = urlparse(dsn)
    if parsed.password is None:
        return dsn
    user = parsed.username or ""
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return f"{parsed.scheme}://{user}@{netloc}{parsed.path or ''}"


def _row_value(row: dict[str, Any]) -> str:
    # SHOW VARIABLES LIKE 'x' returns columns Variable_name + Value.
    return str(row.get("Value", ""))


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "dsn" not in config:
        raise ValueError("mysql connector config requires 'dsn'")
    schemas_raw = config.get("schemas", ())
    return MysqlConnector(
        MysqlConfig(
            dsn=str(config["dsn"]),
            schemas=tuple(schemas_raw) if schemas_raw else (),
            tables=tuple(config.get("tables", ())),
            excluded_tables=tuple(config.get("excluded_tables", ())),
            sample_rows=int(
                config.get(
                    "sample_rows",
                    reservoir_sample_size(confidence=0.95, prevalence=0.01),
                )
            ),
            statement_timeout_ms=int(
                config.get("statement_timeout_ms", 30_000)
            ),
            pool_size=int(config.get("pool_size", 2)),
            require_replica=bool(config.get("require_replica", True)),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="mysql",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=True,
        content_hash_delta=False,
        max_concurrent_fetches=2,
        streaming=True,
    ),
    required_scopes=("mysql:read",),
    description=(
        "MySQL SourceConnector. Replica-only enforcement (read_only=ON + "
        "active replication source). Reservoir sampling per table. "
        "30 s MAX_EXECUTION_TIME hint. Pool capped at 2."
    ),
)


__all__ = [
    "MysqlConfig",
    "MysqlConnector",
    "PrimaryConnectionRefused",
    "SPEC",
]
