"""Daemon-less OCI registry SourceConnector for pleno-pii-scanner (ADR §15)."""

from pleno_pii_scanner_oci.connector import (
    OciConfig,
    OciConnector,
    SPEC,
)

__all__ = ["OciConfig", "OciConnector", "SPEC"]
