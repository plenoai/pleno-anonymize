"""ConnectorSpec exported via the ``pleno_pii_scanner.connectors`` entry point."""

from __future__ import annotations

from pleno_pii_scanner.sources.base import Capabilities
from pleno_pii_scanner.sources.registry import ConnectorSpec

from pleno_pii_scanner_saas_retriever.adapter import KIND, build_connector

SPEC = ConnectorSpec(
    kind=KIND,
    version="0.2.0",
    factory=build_connector,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=8,
        streaming=False,
    ),
    # API-token-driven adapter — concrete scopes vary per connector
    # (github needs `repo` for private repos; slack needs `channels:history`
    # etc. when those land). The label here flags that an upstream API
    # token is the implicit prerequisite; the smoke test expects a
    # non-empty tuple.
    required_scopes=("api:token",),
    description=(
        "API-only SourceConnector backed by saas-retriever. "
        "config.connector_kind selects the underlying provider (github "
        "today; slack / jira / confluence / notion / gitlab / bitbucket "
        "land in subsequent saas-retriever releases). Token comes from "
        "config.token, the GITHUB_TOKEN env var, or `gh auth token`."
    ),
)


__all__ = ["KIND", "SPEC"]
