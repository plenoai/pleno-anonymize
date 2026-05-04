"""PostgreSQL SourceConnector for pleno-pii-scanner (ADR-0007 §16)."""

from pleno_pii_scanner_postgres.connector import (
    PostgresConfig,
    PostgresConnector,
    SPEC,
)

__all__ = ["PostgresConfig", "PostgresConnector", "SPEC"]
