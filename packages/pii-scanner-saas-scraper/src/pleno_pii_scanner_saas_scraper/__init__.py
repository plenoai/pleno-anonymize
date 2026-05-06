"""saas-scraper-backed SourceConnector for pleno-pii-scanner.

Wraps any ``saas_scraper.Connector`` (slack, github, gitlab, bitbucket,
jira, confluence, notion) behind the ``pleno_pii_scanner.sources.base``
SourceConnector protocol. Selected at config time via
``scraper_kind=...`` so a single registered kind (``saas-scraper``)
covers every saas-scraper provider — new providers landed in saas-scraper
become available here without a wheel change.
"""

from pleno_pii_scanner_saas_scraper.adapter import (
    SaasScraperAdapter,
    SaasScraperConfig,
    build_connector,
)
from pleno_pii_scanner_saas_scraper.spec import KIND, SPEC

__all__ = [
    "KIND",
    "SPEC",
    "SaasScraperAdapter",
    "SaasScraperConfig",
    "build_connector",
]

__version__ = "0.1.0"
