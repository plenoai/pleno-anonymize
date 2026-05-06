"""Scheduler — orchestrates discover/fetch/scan across SourceConnectors.

Pulls together SourceConnector (#3), Connector Registry (#4),
CredentialBroker (#5), CheckpointStore (#6), and ContentExtractor (#8) so
the rest of the system is shielded from per-source plumbing. The
Scheduler is the single object the CLI builds for each `scan` invocation.

See ADR-0007 §4.
"""

from pleno_pii_scanner.scheduler.core import (
    Scheduler,
    SchedulerConfig,
    SourcePlan,
    SourceResult,
)
from pleno_pii_scanner.scheduler.incremental import (
    DetectorFn,
    IncrementalResult,
    IncrementalRunner,
    IncrementalStats,
    OnFindingsFn,
)
from pleno_pii_scanner.scheduler.rate_limit import (
    AdaptiveTokenBucket,
    BucketKey,
    GlobalRateLimiter,
    RateLimited,
)
from pleno_pii_scanner.scheduler.retry import (
    RetryConfig,
    RetryError,
    retry_async,
)

__all__ = [
    "AdaptiveTokenBucket",
    "BucketKey",
    "DetectorFn",
    "GlobalRateLimiter",
    "IncrementalResult",
    "IncrementalRunner",
    "IncrementalStats",
    "OnFindingsFn",
    "RateLimited",
    "RetryConfig",
    "RetryError",
    "Scheduler",
    "SchedulerConfig",
    "SourcePlan",
    "SourceResult",
    "retry_async",
]
