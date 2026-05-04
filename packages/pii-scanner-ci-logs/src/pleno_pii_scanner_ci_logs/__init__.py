"""CI build-log SourceConnector (Task #41, ADR-0007 §13).

Re-exports the public surface so the entry-point loader sees `SPEC`
directly and downstream code can `from pleno_pii_scanner_ci_logs
import CiLogsConfig, CiLogsConnector` without diving into submodules.
"""

from pleno_pii_scanner_ci_logs.api import (
    DEFAULT_BUILDKITE_BASE_URL,
    DEFAULT_CIRCLECI_BASE_URL,
    DEFAULT_GITHUB_ACTIONS_BASE_URL,
    BasicAuth,
    BearerAuth,
    CircleTokenAuth,
    CiLogsApi,
    CiLogsApiError,
)
from pleno_pii_scanner_ci_logs.connector import (
    DEFAULT_MAX_BUILDS,
    DEFAULT_MAX_LOG_BYTES,
    KIND,
    SPEC,
    CiLogsConfig,
    CiLogsConnector,
)


__all__ = [
    "DEFAULT_BUILDKITE_BASE_URL",
    "DEFAULT_CIRCLECI_BASE_URL",
    "DEFAULT_GITHUB_ACTIONS_BASE_URL",
    "DEFAULT_MAX_BUILDS",
    "DEFAULT_MAX_LOG_BYTES",
    "KIND",
    "SPEC",
    "BasicAuth",
    "BearerAuth",
    "CircleTokenAuth",
    "CiLogsApi",
    "CiLogsApiError",
    "CiLogsConfig",
    "CiLogsConnector",
]

__version__ = "0.1.0"
