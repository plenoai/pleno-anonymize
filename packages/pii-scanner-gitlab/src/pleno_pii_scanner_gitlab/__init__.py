"""GitLab SourceConnector for pleno-pii-scanner (Task #18 / ADR §13).

Targets both SaaS gitlab.com and self-managed CE/EE under PAT, OAuth2,
or project-access-token credentials. Group walks recurse through every
subgroup; clones are shallow (`git clone --depth=1`) and rmtree'd in
`close()`. Registered via the `pleno_pii_scanner.connectors` entry-point
group as kind `gitlab`.
"""

from pleno_pii_scanner_gitlab.auth import (
    GitlabAuthMode,
    InvalidGitlabAuthError,
    parse_auth_mode,
)
from pleno_pii_scanner_gitlab.connector import (
    KIND,
    SPEC,
    GitlabConfig,
    GitlabConnector,
)

__all__ = [
    "KIND",
    "SPEC",
    "GitlabAuthMode",
    "GitlabConfig",
    "GitlabConnector",
    "InvalidGitlabAuthError",
    "parse_auth_mode",
]

__version__ = "0.1.0"
