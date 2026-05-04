"""MongoDB SourceConnector for pleno-pii-scanner (ADR-0007 §16)."""

from pleno_pii_scanner_mongodb.connector import (
    MongoConfig,
    MongoConnector,
    PrimaryConnectionRefused,
    SPEC,
    reservoir_sample_size,
)

__all__ = [
    "MongoConfig",
    "MongoConnector",
    "PrimaryConnectionRefused",
    "SPEC",
    "reservoir_sample_size",
]
