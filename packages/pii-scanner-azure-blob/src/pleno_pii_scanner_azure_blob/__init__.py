"""Azure Blob Storage connector wheel for pleno-pii-scanner.

Public surface re-exported here so third-party code can `from
pleno_pii_scanner_azure_blob import SPEC, AzureBlobConnector,
AzureBlobConfig, AzureBlobAuthConfig, AzureBlobDiscovery` without
reaching into submodules.

Entry-point registration:

    [project.entry-points."pleno_pii_scanner.connectors"]
    azure_blob = "pleno_pii_scanner_azure_blob:SPEC"

The core CLI (`pleno-pii-scanner scan azure_blob`) calls
`pleno_pii_scanner.sources.create("azure_blob", config)` which in turn
invokes `SPEC.factory(config)` defined in `connector.py`.
"""

from pleno_pii_scanner_azure_blob._auth import (
    AZURE_STORAGE_DEFAULT_SCOPE,
    AZURE_STORAGE_RESOURCE,
    AccessToken,
    ManagedIdentityTokenSource,
    SharedKeyCredential,
    TokenCache,
    TokenSource,
    WorkloadIdentityTokenSource,
    sign_shared_key,
)
from pleno_pii_scanner_azure_blob.connector import (
    AZURE_STORAGE_API_VERSION,
    AzureAccount,
    AzureBlobAuthConfig,
    AzureBlobConfig,
    AzureBlobConnector,
    AzureBlobDiscovery,
    ContainerSpec,
    DEFAULT_CONCURRENCY,
    DEFAULT_ENDPOINT_SUFFIX,
    KIND,
    SPEC,
)

__version__ = "0.1.0"

__all__ = [
    "AZURE_STORAGE_API_VERSION",
    "AZURE_STORAGE_DEFAULT_SCOPE",
    "AZURE_STORAGE_RESOURCE",
    "AccessToken",
    "AzureAccount",
    "AzureBlobAuthConfig",
    "AzureBlobConfig",
    "AzureBlobConnector",
    "AzureBlobDiscovery",
    "ContainerSpec",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_ENDPOINT_SUFFIX",
    "KIND",
    "ManagedIdentityTokenSource",
    "SPEC",
    "SharedKeyCredential",
    "TokenCache",
    "TokenSource",
    "WorkloadIdentityTokenSource",
    "__version__",
    "sign_shared_key",
]
