"""Incremental scan state — checkpoints and shard receipts (ADR-0007 §5)."""

from .checkpoint import (
    Checkpoint,
    CheckpointStore,
    ShardRecord,
    decode_byte_range,
    encode_byte_range,
)
from .memory_store import MemoryCheckpointStore
from .sqlite_store import SqliteCheckpointStore, default_state_path

__all__ = [
    "Checkpoint",
    "CheckpointStore",
    "MemoryCheckpointStore",
    "ShardRecord",
    "SqliteCheckpointStore",
    "decode_byte_range",
    "default_state_path",
    "encode_byte_range",
]
