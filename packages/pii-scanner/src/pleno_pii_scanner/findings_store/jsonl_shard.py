"""JSONL shard writer/reader for encrypted findings (ADR-0007 §11).

Wire format: newline-delimited JSON, one encrypted finding per line. The
file is opened append-only; if the process is killed mid-flush, the
partial trailing line is rejected on the next read so the durability
model is "every fully-flushed line survives". Parquet support is a
follow-up — the writer interface (`write_batch`) is shaped so the
Parquet implementation can swap in without touching the FindingsStore.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .encryption import EncryptedPayload, EncryptionError


# WHY: the shard layout matches ADR §11 example:
#   ~/.local/state/pleno/<scan_id>/findings/<source_id>/<shard_index>.jsonl
# A separate path component for source_id means concurrent connectors
# never share a writer — no asyncio.Lock contention across sources.
def shard_path(
    base: Path, scan_id: str, source_id: str, shard_index: int
) -> Path:
    """Resolve the on-disk path for a single shard file."""
    return base / scan_id / "findings" / source_id / f"{shard_index}.jsonl"


def default_shard_base() -> Path:
    """XDG-aware base directory for shard files (mirrors checkpoint store)."""
    base_env = os.environ.get("XDG_STATE_HOME")
    base = Path(base_env) if base_env else Path.home() / ".local" / "state"
    return base / "pleno"


class JsonlShardWriter:
    """Append-only JSONL writer for one (scan_id, source_id) shard.

    The writer is awaited under a per-instance asyncio.Lock so multiple
    coroutines on the same shard cannot interleave bytes inside one line.
    Different shards (different source_id or shard_index) get distinct
    writer instances and proceed in parallel without contention.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    async def write_batch(
        self,
        finding_ids: list[str],
        fingerprints: list[str],
        payloads: list[EncryptedPayload],
    ) -> int:
        """Append a batch of encrypted findings; return the count written.

        All three lists must have the same length. The whole batch is
        flushed inside a single `os.write` to make crash semantics
        line-aligned: either every finding in the batch is on disk after
        fsync, or none is.
        """
        if not (len(finding_ids) == len(fingerprints) == len(payloads)):
            raise ValueError(
                "write_batch requires equal-length finding_ids, "
                "fingerprints, and payloads"
            )
        if self._closed:
            raise RuntimeError("JsonlShardWriter is closed")
        if not finding_ids:
            return 0
        lines: list[bytes] = []
        for fid, fp, payload in zip(
            finding_ids, fingerprints, payloads, strict=True
        ):
            obj: dict[str, object] = {
                "finding_id": fid,
                "fingerprint": fp,
            }
            obj.update(payload.to_jsonl())
            lines.append(
                (json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode(
                    "utf-8"
                )
            )
        blob = b"".join(lines)

        async with self._lock:
            # WHY: parent dirs can be missing on first write of a scan.
            # mode=0o700 because the encrypted bytes are still attacker-
            # interesting (length oracle, fingerprint correlation).
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await asyncio.to_thread(_append_and_fsync, self._path, blob)
        return len(finding_ids)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True


def _append_and_fsync(path: Path, blob: bytes) -> None:
    """Synchronous append + fsync; called via asyncio.to_thread."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, blob)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_shard(path: Path) -> list[tuple[str, str, EncryptedPayload]]:
    """Decode every complete JSONL line into (finding_id, fingerprint, payload).

    A trailing partial line (no `\\n`) is silently dropped so a SIGKILL
    mid-write does not poison subsequent reads.
    """
    if not path.exists():
        return []
    out: list[tuple[str, str, EncryptedPayload]] = []
    with path.open("rb") as fh:
        for raw in fh:
            if not raw.endswith(b"\n"):
                # WHY: torn write; skip the trailing fragment.
                break
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EncryptionError(
                    f"corrupt shard line in {path}: {exc}"
                ) from exc
            if not isinstance(obj, dict):
                raise EncryptionError(f"non-object shard line in {path}")
            payload = EncryptedPayload.from_jsonl(obj)
            out.append((obj["finding_id"], obj["fingerprint"], payload))
    return out


__all__ = [
    "JsonlShardWriter",
    "default_shard_base",
    "read_shard",
    "shard_path",
]
