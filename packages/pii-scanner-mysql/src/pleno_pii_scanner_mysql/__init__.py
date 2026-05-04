"""MySQL SourceConnector for pleno-pii-scanner (ADR-0007 §16)."""

from pleno_pii_scanner_mysql.connector import (
    MysqlConfig,
    MysqlConnector,
    PrimaryConnectionRefused,
    SPEC,
)

__all__ = [
    "MysqlConfig",
    "MysqlConnector",
    "PrimaryConnectionRefused",
    "SPEC",
]
