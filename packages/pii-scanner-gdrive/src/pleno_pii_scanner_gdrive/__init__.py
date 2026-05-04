"""Google Drive SourceConnector for pleno-pii-scanner (ADR-0007 §13)."""

from pleno_pii_scanner_gdrive.connector import (
    GdriveConfig,
    GdriveConnector,
    SPEC,
)

__all__ = ["GdriveConfig", "GdriveConnector", "SPEC"]
