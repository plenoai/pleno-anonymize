"""Atlassian Confluence SourceConnector (Task #28, ADR §13).

Re-exports the public surface so the entry-point loader sees `SPEC`
directly and downstream code can `from pleno_pii_scanner_confluence
import ConfluenceConfig, ConfluenceConnector` without diving into
submodules.
"""

from pleno_pii_scanner_confluence.api import (
    BasicAuth,
    BearerAuth,
    ConfluenceApi,
    ConfluenceApiError,
)
from pleno_pii_scanner_confluence.connector import (
    KIND,
    SPEC,
    ConfluenceConfig,
    ConfluenceConnector,
)
from pleno_pii_scanner_confluence.storage import storage_to_text


__all__ = [
    "KIND",
    "SPEC",
    "BasicAuth",
    "BearerAuth",
    "ConfluenceApi",
    "ConfluenceApiError",
    "ConfluenceConfig",
    "ConfluenceConnector",
    "storage_to_text",
]

__version__ = "0.1.0"
