"""ConnectorSpec exported via the ``pleno_pii_scanner.connectors`` entry point."""

from __future__ import annotations

from pleno_pii_scanner.sources.base import Capabilities
from pleno_pii_scanner.sources.registry import ConnectorSpec

from pleno_pii_scanner_saas_scraper.adapter import KIND, build_connector

SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=build_connector,
    # max_concurrent_fetches=1 because every saas-scraper Connector
    # serialises on its parent BrowserSession (single Chromium process).
    # The scheduler honours this via per-connector semaphores.
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=1,
        streaming=False,
    ),
    required_scopes=(),
    description=(
        "Chrome-driven SourceConnector backed by saas-scraper. "
        "config.scraper_kind selects the underlying provider "
        "(slack, github, gitlab, bitbucket, jira, confluence, notion). "
        "Inherits the user's Chrome profile so SAML / SSO / MFA "
        "sessions don't have to be re-implemented per provider. "
        "Concurrency is 1 — every fetch routes through one Chromium "
        "instance."
    ),
)


__all__ = ["KIND", "SPEC"]
