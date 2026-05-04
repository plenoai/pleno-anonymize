"""Google Cloud Storage connector wheel for pleno-pii-scanner.

Public surface re-exported here so third-party code can `from
pleno_pii_scanner_gcs import SPEC, GcsConnector, GcsConfig,
GcsAuthConfig, GcsBucketDiscovery` without reaching into submodules.

Entry-point registration:

    [project.entry-points."pleno_pii_scanner.connectors"]
    gcs = "pleno_pii_scanner_gcs:SPEC"

The core CLI (`pleno-pii-scanner scan gcs`) calls
`pleno_pii_scanner.sources.create("gcs", config)` which in turn
invokes `SPEC.factory(config)` defined in `connector.py`.
"""

from pleno_pii_scanner_gcs._oauth_token import (
    AccessToken,
    ApplicationDefaultTokenSource,
    DEFAULT_SCOPES,
    ServiceAccountKeyTokenSource,
    TokenCache,
    TokenSource,
    WorkloadIdentityTokenSource,
)
from pleno_pii_scanner_gcs.connector import (
    DEFAULT_CONCURRENCY,
    GcsAuthConfig,
    GcsBucketDiscovery,
    GcsConfig,
    GcsConnector,
    KIND,
    SPEC,
)

__version__ = "0.1.0"

__all__ = [
    "AccessToken",
    "ApplicationDefaultTokenSource",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_SCOPES",
    "GcsAuthConfig",
    "GcsBucketDiscovery",
    "GcsConfig",
    "GcsConnector",
    "KIND",
    "SPEC",
    "ServiceAccountKeyTokenSource",
    "TokenCache",
    "TokenSource",
    "WorkloadIdentityTokenSource",
    "__version__",
]
