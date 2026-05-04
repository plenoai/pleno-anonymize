"""Slack SourceConnector wheel for pleno-pii-scanner.

Auto-routes between three Slack token shapes:

    xoxb-...  bot token    conversations.* + files.*  (single workspace)
    xoxp-...  user token   conversations.* + files.*  (single workspace, full visibility)
    xoxa-...  org-wide     discovery.*                (Enterprise Grid, all workspaces)

The Discovery API path is the differentiator for enterprise: org-wide
xoxa tokens reach every workspace under one auth and use a separate (much
more generous) rate-limit budget than the per-team Tier 1-4 limits, which
is why ADR-0007 §13 calls it the "Tier 3 制限回避" path.

Registered in the core registry via the entry-point group
`pleno_pii_scanner.connectors`; route via `pleno-pii-scanner scan slack ...`.
"""

from .connector import SPEC, SlackConfig, SlackConnector
from .tokens import InvalidSlackTokenError, SlackTokenType, classify_token

__all__ = [
    "SPEC",
    "InvalidSlackTokenError",
    "SlackConfig",
    "SlackConnector",
    "SlackTokenType",
    "classify_token",
]

__version__ = "0.1.0"
