"""Google BigQuery SourceConnector for pleno-pii-scanner (ADR-0007 §16)."""

from pleno_pii_scanner_bigquery.connector import (
    SPEC,
    BigQueryConfig,
    BigQueryConnector,
    BigQueryCostCapExceeded,
)

__all__ = [
    "SPEC",
    "BigQueryConfig",
    "BigQueryConnector",
    "BigQueryCostCapExceeded",
]
