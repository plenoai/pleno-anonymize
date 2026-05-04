"""Built-in connectors that ship with the core wheel.

These are the connectors that need zero new dependencies — `dir`, `git`,
and `github` already worked in the pre-multi-source design via the
`walker`, `git_history`, and `github` modules. The classes here adapt
those existing helpers to the SourceConnector Protocol so the scheduler
can drive them through the same interface as third-party connectors
(`pleno-pii-scanner-aws`, etc.).

Registration is performed at import time so `pleno-pii-scanner` always
has the three built-ins available without depending on entry-point
discovery (the registry's lazy import happens once per process; built-ins
that ship in the same wheel pre-empt that latency).

ADR-0007 §7.
"""

from pleno_pii_scanner.sources.builtin.dir_source import (
    DirConnector,
    DirConfig,
    SPEC as DIR_SPEC,
)
from pleno_pii_scanner.sources.builtin.git_source import (
    GitConnector,
    GitConfig,
    SPEC as GIT_SPEC,
)
from pleno_pii_scanner.sources.builtin.github_source import (
    GithubConnector,
    GithubConfig,
    SPEC as GITHUB_SPEC,
)

# Re-export the canonical kinds so tests and other modules can refer to
# them by symbol instead of magic strings.
DIR_KIND = DIR_SPEC.kind
GIT_KIND = GIT_SPEC.kind
GITHUB_KIND = GITHUB_SPEC.kind

__all__ = [
    "DIR_KIND",
    "DIR_SPEC",
    "DirConfig",
    "DirConnector",
    "GIT_KIND",
    "GIT_SPEC",
    "GITHUB_KIND",
    "GITHUB_SPEC",
    "GitConfig",
    "GitConnector",
    "GithubConfig",
    "GithubConnector",
]
