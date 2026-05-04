"""Bitbucket Cloud + Bitbucket Server SourceConnector (Task #19, ADR §13).

Re-exports the public surface so the entry-point loader sees `SPEC`
directly and downstream code can `from pleno_pii_scanner_bitbucket
import BitbucketConfig, BitbucketConnector` without diving into
submodules.
"""

from pleno_pii_scanner_bitbucket.api import (
    DEFAULT_CLOUD_BASE_URL,
    BasicAuth,
    BearerAuth,
    BitbucketApi,
    BitbucketApiError,
)
from pleno_pii_scanner_bitbucket.connector import (
    KIND,
    SPEC,
    BitbucketConfig,
    BitbucketConnector,
)


__all__ = [
    "DEFAULT_CLOUD_BASE_URL",
    "KIND",
    "SPEC",
    "BasicAuth",
    "BearerAuth",
    "BitbucketApi",
    "BitbucketApiError",
    "BitbucketConfig",
    "BitbucketConnector",
]

__version__ = "0.1.0"
