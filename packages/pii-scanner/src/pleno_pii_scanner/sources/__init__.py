"""Source connector framework.

Defines the Protocol that every connector (filesystem, GitHub, S3, Slack,
Confluence, Snowflake, ...) implements so the scan pipeline can stay
source-agnostic. Concrete connectors live in
`pleno_pii_scanner.sources.builtin.*` (zero extra deps) or in separately
distributed wheels (e.g. `pleno-pii-scanner-aws`) discovered via
`entry_points("pleno_pii_scanner.connectors")`.

See ADR-0007 §1 for the architectural rationale: discover/fetch are
separated so that a 10**9-key S3 bucket can be scanned without
materializing the full enumeration first, and so that TB-scale objects
can be streamed as `DocumentChunk` slices rather than buffered whole.
"""

from pleno_pii_scanner.sources.base import (
    SUBSOURCE_METADATA_KEY,
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    IncrementalSourceConnector,
    Principal,
    SourceConnector,
    SourceFilter,
    Subsource,
)
from pleno_pii_scanner.sources.registry import (
    ConnectorError,
    ConnectorFactory,
    ConnectorSpec,
    DuplicateConnectorError,
    UnknownConnectorError,
    create,
    get,
    list_kinds,
    list_specs,
    register,
    unregister,
)

__all__ = [
    "SUBSOURCE_METADATA_KEY",
    "Capabilities",
    "ConnectorError",
    "ConnectorFactory",
    "ConnectorSpec",
    "Cursor",
    "Document",
    "DocumentChunk",
    "DocumentRef",
    "DuplicateConnectorError",
    "IncrementalSourceConnector",
    "Principal",
    "SourceConnector",
    "SourceFilter",
    "Subsource",
    "UnknownConnectorError",
    "create",
    "get",
    "list_kinds",
    "list_specs",
    "register",
    "unregister",
]
