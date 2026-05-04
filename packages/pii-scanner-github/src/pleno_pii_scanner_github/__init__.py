"""Enterprise GitHub connector for pleno-pii-scanner (Task #17 / ADR §13).

Replaces the builtin `github` connector's gh-CLI + shallow-clone path
with direct REST + GraphQL calls under GitHub App auth. Registered via
the `pleno_pii_scanner.connectors` entry-point group as kind
`github-app` so the builtin `github` (zero-deps) keeps working unchanged.
"""

from pleno_pii_scanner_github.app_auth import AppAuth, mint_app_jwt
from pleno_pii_scanner_github.connector import (
    KIND,
    SPEC,
    GithubAppConfig,
    GithubAppConnector,
)

__all__ = [
    "KIND",
    "SPEC",
    "AppAuth",
    "GithubAppConfig",
    "GithubAppConnector",
    "mint_app_jwt",
]

__version__ = "0.1.0"
