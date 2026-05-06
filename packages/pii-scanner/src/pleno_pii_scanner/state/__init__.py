"""Incremental scan state — checkpoints, shard receipts, and the shared
content-fingerprinted scan cache (ADR-0007 §5).

The CheckpointStore (#6) gives a single scan kill -9 durability via per-
source resume cursors. The ScanCache is its long-running counterpart:
results survive across `scan_id`s so a nightly org-scan can amortize the
95% of work that did not change since yesterday.
"""

from .checkpoint import (
    Checkpoint,
    CheckpointStore,
    ShardRecord,
    decode_byte_range,
    encode_byte_range,
)
from .memory_store import MemoryCheckpointStore
from .scan_cache import (
    CacheEntry,
    CacheLookup,
    MemoryScanCache,
    ScanCache,
    SqliteScanCache,
    default_cache_path,
)
from .schema_version import schema_version
from .sqlite_store import SqliteCheckpointStore, default_state_path

__all__ = [
    "CacheEntry",
    "CacheLookup",
    "Checkpoint",
    "CheckpointStore",
    "MemoryCheckpointStore",
    "MemoryScanCache",
    "ScanCache",
    "ShardRecord",
    "SqliteCheckpointStore",
    "SqliteScanCache",
    "decode_byte_range",
    "default_cache_path",
    "default_state_path",
    "encode_byte_range",
    "schema_version",
]
