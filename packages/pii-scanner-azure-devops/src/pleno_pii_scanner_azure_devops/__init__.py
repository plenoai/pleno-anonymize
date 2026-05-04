"""Azure DevOps SourceConnector for pleno-pii-scanner (ADR-0007 §13).

Single wheel covers both Azure DevOps Services (dev.azure.com) and
Azure DevOps Server (on-prem TFS successor); the `flavor` config switch
picks the URL shape and the `ca_bundle_path` allows a private CA for
Server installs. Three auth modes: PAT (basic auth, empty user), OAuth
2 access token (Bearer), and federated workload identity (OIDC token
exchange against Microsoft Entra).
"""

from pleno_pii_scanner_azure_devops.api import (
    AzureDevOpsApi,
    AzureDevOpsApiError,
)
from pleno_pii_scanner_azure_devops.auth import (
    AzureDevOpsAuth,
    FederatedConfig,
    FederatedTokenError,
)
from pleno_pii_scanner_azure_devops.connector import (
    KIND,
    SPEC,
    AzureDevOpsConfig,
    AzureDevOpsConnector,
)

__all__ = [
    "KIND",
    "SPEC",
    "AzureDevOpsApi",
    "AzureDevOpsApiError",
    "AzureDevOpsAuth",
    "AzureDevOpsConfig",
    "AzureDevOpsConnector",
    "FederatedConfig",
    "FederatedTokenError",
]

__version__ = "0.1.0"
