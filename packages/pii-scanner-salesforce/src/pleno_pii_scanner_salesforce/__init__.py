"""Salesforce SourceConnector for pleno-pii-scanner (ADR-0007 §13)."""

from pleno_pii_scanner_salesforce.connector import (
    SPEC,
    SalesforceConfig,
    SalesforceConnector,
)

__all__ = ["SPEC", "SalesforceConfig", "SalesforceConnector"]
