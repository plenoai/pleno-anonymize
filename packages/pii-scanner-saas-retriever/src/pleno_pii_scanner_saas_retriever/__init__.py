"""saas-retriever-backed SourceConnector for pleno-pii-scanner.

Wraps any ``saas_retriever.Connector`` (currently github; slack / jira /
confluence / notion / gitlab / bitbucket land in subsequent
saas-retriever releases) behind the ``pleno_pii_scanner.sources.base``
SourceConnector protocol. The underlying connector is selected at
config time via ``connector_kind=...`` so a single registered kind
(``saas-retriever``) covers every saas-retriever provider — new
providers landed upstream become available here without a wheel change.
"""

from pleno_pii_scanner_saas_retriever.adapter import (
    SaasRetrieverAdapter,
    SaasRetrieverConfig,
    build_connector,
)
from pleno_pii_scanner_saas_retriever.spec import KIND, SPEC

__all__ = [
    "KIND",
    "SPEC",
    "SaasRetrieverAdapter",
    "SaasRetrieverConfig",
    "build_connector",
]

__version__ = "0.2.0"
