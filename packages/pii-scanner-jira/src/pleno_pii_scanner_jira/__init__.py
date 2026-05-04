"""Atlassian Jira SourceConnector for pleno-pii-scanner (ADR-0007 §13)."""

from pleno_pii_scanner_jira.connector import (
    JiraConfig,
    JiraConnector,
    SPEC,
    adf_to_text,
)

__all__ = ["JiraConfig", "JiraConnector", "SPEC", "adf_to_text"]
