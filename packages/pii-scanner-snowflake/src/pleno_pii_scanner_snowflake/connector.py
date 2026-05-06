"""Snowflake SourceConnector — XS-warehouse, key-pair JWT, SAMPLE.

Hard requirements driven by ADR-0007 §16:

  * **Dedicated XS warehouse.** Configurable via `warehouse` (default
    `PII_SCANNER_XS`). The session opens with `USE WAREHOUSE` so the
    scan never lands on the BI / ELT compute pool.
  * **`STATEMENT_TIMEOUT_IN_SECONDS`.** Set per session via
    `parameters` on every statement request. A runaway sample on a
    multi-billion-row table would otherwise burn the credit budget.
  * **Key-pair JWT auth.** Snowflake's password auth path is being
    deprecated for service users. The connector signs a short-lived
    RS256 JWT with the user's RSA private key and presents it as
    `Authorization: Bearer <jwt>` plus
    `X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT`. The actual
    JWT signing is delegated to `_acquire_token`, which tests
    monkeypatch.
  * **Server-side sampling via `SAMPLE (n ROWS)`.** Bypasses full
    table scan; required for fact tables.

Talks to the [Snowflake SQL REST API
v2](https://docs.snowflake.com/en/developer-guide/sql-api/intro):

  * `POST /api/v2/statements` — submit a statement, optionally
    with session parameters in `parameters`. Response carries
    `resultSetMetaData.rowType` (column descriptors) and `data`
    (the first partition).
  * `GET  /api/v2/statements/{handle}?partition=N` — additional
    partitions, when `resultSetMetaData.partitionInfo` lists
    more than one.

Cursor is `str` per `pleno_pii_scanner.sources.base.Cursor`; we do
not currently emit one (incremental=False) but accept it for
protocol conformance.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

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


# Snowflake SQL REST API v2 lives at this prefix on every account
# host. The account host is `https://{account}.snowflakecomputing.com`.
_API_PATH = "/api/v2/statements"


@dataclass(frozen=True, slots=True)
class SnowflakeConfig:
    """Construction config for `SnowflakeConnector`.

    `account` is the Snowflake account locator (`abc12345.us-east-1`,
    `xyz98765`, etc. — without the `.snowflakecomputing.com` suffix).
    `private_key_pem` is the PEM-encoded RSA private key whose public
    half is registered on the Snowflake user.

    `warehouse` defaults to `PII_SCANNER_XS` — operators are expected
    to provision a dedicated XS warehouse so PII scans never contend
    with production workloads. `statement_timeout_seconds` becomes
    `STATEMENT_TIMEOUT_IN_SECONDS` on every statement.
    """

    account: str
    user: str
    private_key_pem: str
    warehouse: str = "PII_SCANNER_XS"
    databases: tuple[str, ...] = ()
    schemas: tuple[str, ...] = ()
    sample_rows: int = 1000
    statement_timeout_seconds: int = 60
    role: str = "PUBLIC"
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.account:
            raise ValueError("account must be non-empty")
        if not self.user:
            raise ValueError("user must be non-empty")
        if not self.private_key_pem:
            raise ValueError("private_key_pem must be non-empty")
        if not self.warehouse:
            raise ValueError("warehouse must be non-empty")
        if not self.role:
            raise ValueError("role must be non-empty")
        if self.statement_timeout_seconds <= 0:
            raise ValueError("statement_timeout_seconds must be > 0")
        if self.sample_rows < 1:
            raise ValueError("sample_rows must be >= 1")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Account+user uniquely identify a credential without leaking
        # the private key.
        return f"snowflake:{self.account}:{self.user}"

    def base_url(self) -> str:
        return f"https://{self.account}.snowflakecomputing.com"


class SnowflakeConnector:
    """Read-only SourceConnector for Snowflake via SQL REST API v2."""

    kind = "snowflake"

    def __init__(
        self,
        config: SnowflakeConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        # Test seam — the test suite injects a MockTransport-backed
        # client. Production callers let the connector own its client.
        if client is None:
            self._client = httpx.AsyncClient(base_url=config.base_url())
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        del cursor  # incremental=False
        databases = await self._resolve_databases()
        for db in databases:
            schemas = await self._resolve_schemas(db)
            for schema in schemas:
                tables = await self._resolve_tables(db, schema)
                for table in tables:
                    rows, columns = await self._sample_table(db, schema, table)
                    for i, row in enumerate(rows):
                        path = f"{db}/{schema}/{table}/{i}"
                        if filter.include and not _matches_any(path, filter.include):
                            continue
                        if filter.exclude and _matches_any(path, filter.exclude):
                            continue
                        # JSON-encode the projected row dict so the
                        # row payload travels as the DocumentRef
                        # cursor / metadata only — fetch() rebuilds
                        # the Document from this same payload.
                        encoded = json.dumps(
                            _project_row(columns, row),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        yield DocumentRef(
                            source_id=self.id,
                            source_kind=self.kind,
                            path=path,
                            content_type="application/json",
                            metadata={
                                "database": db,
                                "schema": schema,
                                "table": table,
                                "row_index": str(i),
                                "_payload": encoded,
                            },
                        )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        text = ref.metadata.get("_payload", "{}")
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- internals -----------------------------------------------

    async def _resolve_databases(self) -> list[str]:
        if self._config.databases:
            return list(self._config.databases)
        rows, columns = await self._run("SHOW DATABASES")
        return [_pick(columns, row, "name") for row in rows]

    async def _resolve_schemas(self, database: str) -> list[str]:
        if self._config.schemas:
            return list(self._config.schemas)
        rows, columns = await self._run(
            f"SHOW SCHEMAS IN DATABASE {_quote_ident(database)}"
        )
        return [_pick(columns, row, "name") for row in rows]

    async def _resolve_tables(self, database: str, schema: str) -> list[str]:
        rows, columns = await self._run(
            f"SHOW TABLES IN SCHEMA {_quote_ident(database)}.{_quote_ident(schema)}"
        )
        return [_pick(columns, row, "name") for row in rows]

    async def _sample_table(
        self, database: str, schema: str, table: str
    ) -> tuple[list[list[Any]], list[str]]:
        sql = (
            f"SELECT * FROM "
            f"{_quote_ident(database)}.{_quote_ident(schema)}."
            f"{_quote_ident(table)} "
            f"SAMPLE ({self._config.sample_rows} ROWS)"
        )
        return await self._run(sql)

    async def _run(self, sql: str) -> tuple[list[list[Any]], list[str]]:
        """Execute one statement, transparently aggregating partitions."""
        token = await self._acquire_token()
        body = {
            "statement": sql,
            "parameters": {
                "WAREHOUSE": self._config.warehouse,
                "ROLE": self._config.role,
                "STATEMENT_TIMEOUT_IN_SECONDS": str(
                    self._config.statement_timeout_seconds
                ),
            },
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        resp = await self._client.post(_API_PATH, json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        meta = payload.get("resultSetMetaData", {}) or {}
        columns = [str(c.get("name", "")) for c in (meta.get("rowType") or [])]
        rows: list[list[Any]] = list(payload.get("data") or [])
        partitions = meta.get("partitionInfo") or []
        handle = payload.get("statementHandle") or payload.get("statementHandles")
        # First partition is already in `data`; fetch the remaining
        # ones via GET /statements/{handle}?partition=N.
        if isinstance(handle, str) and len(partitions) > 1:
            for n in range(1, len(partitions)):
                part = await self._client.get(
                    f"{_API_PATH}/{handle}",
                    params={"partition": str(n)},
                    headers=headers,
                )
                part.raise_for_status()
                part_body = part.json()
                rows.extend(part_body.get("data") or [])
        return rows, columns

    async def _acquire_token(
        self,
    ) -> str:  # pragma: no cover - signing path is exercised in integration; unit tests monkeypatch
        """Sign a short-lived RS256 JWT with the user's private key.

        Production code wires in `cryptography`-based signing; the
        unit-test suite monkeypatches this method to return a fixed
        token so the suite stays hermetic and key-material-free.
        """
        return _sign_jwt(
            self._config.account, self._config.user, self._config.private_key_pem
        )


# --- helpers ------------------------------------------------------


def _sign_jwt(
    account: str, user: str, private_key_pem: str
) -> str:  # pragma: no cover - exercises real RSA crypto in deployment
    """Build and sign the Snowflake key-pair JWT.

    Pulled out of the connector to keep the I/O class testable
    without dragging `cryptography` into every unit-test run. This
    function is exercised in deployment integration tests, not in
    the in-repo unit suite.
    """
    import base64
    import hashlib
    import time

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(public_bytes).digest()).decode()
    sub = f"{account.upper()}.{user.upper()}"
    iss = f"{sub}.{fp}"
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iss": iss, "sub": sub, "iat": now, "exp": now + 3600}

    def _b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    signing_input = (
        _b64(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64(sig)


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    from fnmatch import fnmatch

    return any(fnmatch(s, p) for p in patterns)


def _quote_ident(name: str) -> str:
    # Snowflake identifiers — wrap in double quotes and escape any
    # embedded ones. Keeps mixed-case and reserved words working.
    return '"' + name.replace('"', '""') + '"'


def _pick(columns: list[str], row: list[Any], wanted: str) -> str:
    # `SHOW DATABASES` / `SHOW SCHEMAS` / `SHOW TABLES` return a
    # `name` column among many; pluck it positionally to avoid relying
    # on Snowflake's own dict-ification.
    for i, c in enumerate(columns):
        if c.lower() == wanted.lower():
            return str(row[i])
    # Fall back: first column is conventionally the name.
    return str(row[0]) if row else ""


def _project_row(columns: list[str], row: list[Any]) -> dict[str, Any]:
    return {
        columns[i] if i < len(columns) else f"_col{i}": v for i, v in enumerate(row)
    }


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    for required in ("account", "user", "private_key_pem"):
        if required not in config:
            raise ValueError(f"snowflake connector config requires {required!r}")
    return SnowflakeConnector(
        SnowflakeConfig(
            account=str(config["account"]),
            user=str(config["user"]),
            private_key_pem=str(config["private_key_pem"]),
            warehouse=str(config.get("warehouse", "PII_SCANNER_XS")),
            databases=tuple(config.get("databases", ())),
            schemas=tuple(config.get("schemas", ())),
            sample_rows=int(config.get("sample_rows", 1000)),
            statement_timeout_seconds=int(config.get("statement_timeout_seconds", 60)),
            role=str(config.get("role", "PUBLIC")),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="snowflake",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=2,
        streaming=False,
    ),
    required_scopes=("snowflake:read",),
    description=(
        "Snowflake SourceConnector. Dedicated XS warehouse, "
        "STATEMENT_TIMEOUT_IN_SECONDS ceiling, key-pair JWT auth, "
        "server-side SAMPLE() per table."
    ),
)


__all__ = [
    "SPEC",
    "SnowflakeConfig",
    "SnowflakeConnector",
]
