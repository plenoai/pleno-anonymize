"""Notion SourceConnector wheel for pleno-pii-scanner.

Workspace-internal integration token (Bearer) → search-based discovery
+ explicit page list + database query. The block tree is materialized
to Markdown so detectors see the same surface text a Notion reader
would, and database row properties are emitted as `key: value` lines so
structured columns (email / phone / URL) stay scannable.

Registered in the core registry via the entry-point group
`pleno_pii_scanner.connectors`; route via `pleno-pii-scanner scan notion ...`.
"""

from .api import (
    DEFAULT_BASE_URL,
    NOTION_VERSION,
    PAGE_SIZE,
    NotionApi,
    NotionApiError,
)
from .connector import KIND, SPEC, NotionConfig, NotionConnector
from .markdown import (
    DEPTH_TRUNCATED_MARKER,
    MAX_DEPTH,
    render_blocks,
    render_database_row,
    render_rich_text,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEPTH_TRUNCATED_MARKER",
    "KIND",
    "MAX_DEPTH",
    "NOTION_VERSION",
    "PAGE_SIZE",
    "SPEC",
    "NotionApi",
    "NotionApiError",
    "NotionConfig",
    "NotionConnector",
    "render_blocks",
    "render_database_row",
    "render_rich_text",
]

__version__ = "0.1.0"
