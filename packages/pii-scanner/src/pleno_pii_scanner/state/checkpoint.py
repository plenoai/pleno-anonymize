"""CheckpointStore protocol and shared dataclasses for incremental scan state.

The store persists per-(scan_id, source_id) resume cursors and per-shard
finding counts so that a scan interrupted by SIGKILL, machine reboot, or
preemption resumes from the last durably-saved batch instead of replaying
the whole source. ADR-0007 §5.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable


# WHY: byte_range is serialized as 'start:end' (decimal, inclusive of start,
# exclusive of end). Keeping the wire format here so SQLite + Memory stores
# round-trip identically and tests can assert on the textual form.
_BYTE_RANGE_SEP = ":"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Resume token for a single (scan_id, source_id) pair.

    `cursor` is opaque (`Cursor = str` in `sources.base`); the connector that
    produced it is the only component allowed to parse it. `last_doc_fingerprint`
    pins the most recently completed `DocumentRef.fingerprint()` so a resume
    can dedup against the FindingsStore. `last_byte_range` is non-None only
    for streamed objects mid-chunk (S3 / SharePoint), so resume can request
    the next byte range without re-scanning the head of a TB-scale object.
    """

    scan_id: str
    source_id: str
    cursor: str | None
    last_doc_fingerprint: str | None
    last_byte_range: tuple[int, int] | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ShardRecord:
    """Per-shard finding-count receipt.

    Every time the FindingsStore (#11) flushes a Parquet/JSONL shard it
    records the shard index and how many findings landed in it. On resume
    the Scheduler reads the shard list to skip already-emitted batches and
    to detect partial writes (recorded but missing-on-disk shards).
    """

    scan_id: str
    source_id: str
    shard_index: int
    finding_count: int
    written_at: datetime


def encode_byte_range(value: tuple[int, int] | None) -> str | None:
    """Serialize a (start, end) byte range to the SQLite TEXT column."""
    if value is None:
        return None
    start, end = value
    # WHY: defensive. Callers should already guarantee these invariants
    # (DocumentChunk enforces them at construction), but the store sits on
    # the persistence boundary so we re-check before writing junk to disk.
    if start < 0 or end < start:
        raise ValueError(
            f"byte_range must be (start>=0, end>=start); got ({start}, {end})"
        )
    return f"{start}{_BYTE_RANGE_SEP}{end}"


def decode_byte_range(value: str | None) -> tuple[int, int] | None:
    """Parse the SQLite TEXT representation back into a (start, end) tuple."""
    if value is None:
        return None
    try:
        start_str, end_str = value.split(_BYTE_RANGE_SEP, 1)
        start, end = int(start_str), int(end_str)
    except ValueError as exc:
        raise ValueError(f"invalid byte_range encoding: {value!r}") from exc
    if start < 0 or end < start:
        raise ValueError(f"invalid byte_range encoding: {value!r}")
    return (start, end)


@runtime_checkable
class CheckpointStore(Protocol):
    """Persistence contract for resume cursors and shard receipts.

    Implementations must be safe to call concurrently from multiple
    asyncio tasks. Last-writer-wins semantics for the same
    (scan_id, source_id) key; independent keys never block each other
    beyond the implementation's writer serialization.
    """

    async def save(self, cp: Checkpoint) -> None:
        """Upsert a checkpoint; the new row overwrites any prior one."""
        ...

    async def save_many(self, cps: list[Checkpoint]) -> None:
        """Upsert a batch of checkpoints in a single durable transaction.

        On commit, every checkpoint is visible; on crash, none are. This
        is what gives the scheduler kill -9 durability across a batch of
        sources scanned in parallel.
        """
        ...

    async def load(
        self, scan_id: str, source_id: str
    ) -> Checkpoint | None:
        """Return the latest checkpoint for the pair, or None if absent."""
        ...

    def list_for_scan(self, scan_id: str) -> AsyncIterator[Checkpoint]:
        """Yield every checkpoint belonging to a scan."""
        ...

    async def delete(self, scan_id: str, source_id: str) -> None:
        """Remove a checkpoint. No-op if absent."""
        ...

    async def record_shard(
        self,
        scan_id: str,
        source_id: str,
        shard_index: int,
        finding_count: int,
    ) -> None:
        """Record that a findings shard was successfully written."""
        ...

    async def list_shards(
        self, scan_id: str, source_id: str
    ) -> list[ShardRecord]:
        """Return all shard receipts for a (scan_id, source_id) pair."""
        ...

    async def close(self) -> None:
        """Release the underlying connection / file handle."""
        ...


__all__ = [
    "Checkpoint",
    "CheckpointStore",
    "ShardRecord",
    "decode_byte_range",
    "encode_byte_range",
]
