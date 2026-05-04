"""Microsoft Teams SourceConnector for pleno-pii-scanner (ADR-0007 §13)."""

from pleno_pii_scanner_msteams.connector import (
    SPEC,
    MsTeamsConfig,
    MsTeamsConnector,
)

__all__ = ["MsTeamsConfig", "MsTeamsConnector", "SPEC"]
