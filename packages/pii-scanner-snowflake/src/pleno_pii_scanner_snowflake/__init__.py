"""Snowflake SourceConnector for pleno-pii-scanner (ADR-0007 §16)."""

from pleno_pii_scanner_snowflake.connector import (
    SPEC,
    SnowflakeConfig,
    SnowflakeConnector,
)

__all__ = ["SPEC", "SnowflakeConfig", "SnowflakeConnector"]
