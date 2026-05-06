"""IncrementalRunner — two-tier cache wrapper around `Scheduler`.

The plain `Scheduler` always re-walks every source from scratch. For
long-running multi-source scans (a nightly org-scan over hundreds of
repos, a daily Confluence sweep, an hourly database export), most of
that work is wasted on content that has not changed since the last run.

`IncrementalRunner` short-circuits two layers, in order:

    1. **Sub-source level** — connectors that implement
       `IncrementalSourceConnector` enumerate sub-units cheaply
       (typically one network call per repo / channel / drive) and tag
       each with a content fingerprint. Sub-units whose fingerprint
       matches the ScanCache are skipped entirely; their prior findings
       are replayed via `on_findings` without any download or detector
       work.

    2. **Document level** — within sub-units that *are* re-scanned (and
       for connectors that present a flat namespace), each fetched
       document is keyed on its `content_hash` (or, when missing, a
       SHA-256 of the body). Hits short-circuit the detector callback
       and replay cached findings; misses run the detector and store
       the result for the next scan.

Both layers share the same `ScanCache` and a caller-supplied
`schema_version` that ties cached findings to the detector pipeline
that produced them — bumping the regex pack or NER model auto-
invalidates every cached entry on the next scan.

The runner is connector-agnostic: any future connector (Slack,
SharePoint, Postgres, ...) gets sub-source caching by implementing
`IncrementalSourceConnector` and gets document caching for free.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from hashlib import sha256

from pleno_pii_scanner.scheduler.core import (
    Scheduler,
    SourcePlan,
    SourceResult,
)
from pleno_pii_scanner.sources.base import (
    SUBSOURCE_METADATA_KEY,
    Document,
    DocumentChunk,
    DocumentRef,
    IncrementalSourceConnector,
    Subsource,
)
from pleno_pii_scanner.state.scan_cache import CacheLookup, ScanCache


# Caller-supplied detector. Returns `(count, payload)` where:
#   * `count`   — number of findings emitted; surfaces in `SourceResult`
#                 so cached replays still report accurate totals.
#   * `payload` — opaque bytes the caller serializes (JSON, msgpack, ...).
#                 The runner stores them verbatim and replays them
#                 verbatim on hit; no internal parsing.
DetectorFn = Callable[
    [DocumentRef, Document | DocumentChunk],
    Awaitable[tuple[int, bytes]],
]


# Notification callback. Fired for every findings batch — fresh-
# detected or cache-replayed alike — so the caller's findings sink
# (FindingsStore, OTLP exporter, dashboards) sees a uniform stream
# regardless of cache state.
#
#   source_id : the connector's id
#   sub_id    : SUBSOURCE_METADATA_KEY value if present, else None
#   count     : number of findings in `payload`
#   payload   : caller-defined wire format
#   replayed  : True when `payload` came from the cache
OnFindingsFn = Callable[
    [str, str | None, int, bytes, bool],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class IncrementalStats:
    """Per-plan cache-effectiveness telemetry. Surfaces in
    `IncrementalResult` so operators can see how much work the cache
    actually saved this run.
    """

    subsource_total: int = 0
    subsource_hits: int = 0
    subsource_misses: int = 0
    document_total: int = 0
    document_hits: int = 0
    document_misses: int = 0


@dataclass(frozen=True, slots=True)
class IncrementalResult:
    """`SourceResult` plus per-plan cache hit/miss breakdown."""

    source_result: SourceResult
    cache_stats: IncrementalStats


@dataclass(slots=True)
class _MutableStats:
    """Counter that becomes an immutable `IncrementalStats` at freeze."""

    subsource_total: int = 0
    subsource_hits: int = 0
    subsource_misses: int = 0
    document_total: int = 0
    document_hits: int = 0
    document_misses: int = 0

    def freeze(self) -> IncrementalStats:
        return IncrementalStats(
            subsource_total=self.subsource_total,
            subsource_hits=self.subsource_hits,
            subsource_misses=self.subsource_misses,
            document_total=self.document_total,
            document_hits=self.document_hits,
            document_misses=self.document_misses,
        )


@dataclass(slots=True)
class _SubsourceBuffer:
    """Per-sub-source rollup so a successful scan caches its full result.

    `payloads` accumulates each fresh-detected document's payload bytes.
    On clean plan completion, the runner concatenates them into a single
    rollup blob keyed on `(source_id, sub_id, fingerprint)`, ready for
    the next run to replay as a single tier-1 cache hit.
    """

    fingerprint: str
    count: int = 0
    payloads: list[bytes] = field(default_factory=list)


# Wire format for cached findings:
#   bytes 0..7  : count (uint64, big-endian)
#   bytes 8..   : caller-defined payload bytes
#
# A fixed-width prefix lets the runner round-trip `(count, payload)`
# through the bytes-only ScanCache without inventing yet another framing
# protocol. 8 bytes covers any plausible finding count for one document
# or sub-source rollup.
_COUNT_PREFIX_BYTES = 8


def _encode(count: int, payload: bytes) -> bytes:
    return count.to_bytes(_COUNT_PREFIX_BYTES, "big") + payload


def _decode(blob: bytes) -> tuple[int, bytes]:
    if len(blob) < _COUNT_PREFIX_BYTES:
        raise ValueError(
            f"cached blob shorter than count prefix ({len(blob)} bytes); "
            "the cache file is corrupt — delete it and re-scan"
        )
    count = int.from_bytes(blob[:_COUNT_PREFIX_BYTES], "big")
    return count, blob[_COUNT_PREFIX_BYTES:]


def _document_fingerprint(doc: Document | DocumentChunk) -> str | None:
    """Return a stable content fingerprint for one fetched payload, or
    None when the connector did not give us enough to cache safely.

    Preference order:
      1. `Document.content_hash` if the connector populated it (git,
         object stores, anything with a server-side digest).
      2. SHA-256 of the body — `text` decoded as UTF-8 or `binary`
         verbatim. Cheap on the typical multi-MB document.
      3. None for `DocumentChunk` — chunk-level caching would have to
         live across all chunks of the document, which the runner does
         not orchestrate. Streaming connectors fall through to a fresh
         scan; this is correct, just not maximally efficient.
    """
    if isinstance(doc, Document):
        if doc.content_hash:
            return doc.content_hash
        h = sha256()
        if doc.text is not None:
            h.update(doc.text.encode("utf-8", errors="replace"))
        elif doc.binary is not None:
            h.update(doc.binary)
        return h.hexdigest()[:32]
    return None


# Cache-key helpers. Newline is illegal in a `source_id` (it is either a
# URL fragment or a config-supplied label) so the delimiter is
# unambiguous and the keys read cleanly in `pleno-pii-scanner cache ls`
# style diagnostic dumps.
def _sub_cache_key(source_kind: str, source_id: str, sub_id: str) -> str:
    return f"sub\n{source_kind}\n{source_id}\n{sub_id}"


def _doc_cache_key(source_kind: str, source_id: str, ref_path: str) -> str:
    return f"doc\n{source_kind}\n{source_id}\n{ref_path}"


class IncrementalRunner:
    """Wraps a `Scheduler` with sub-source + document level caching.

    Construction takes a Scheduler, a ScanCache, and the schema_version
    that ties cached findings to the current detector pipeline. `run()`
    mirrors `Scheduler.run()`'s signature except the caller passes a
    `DetectorFn` (returning serialized findings) plus an `OnFindingsFn`
    (consuming them) instead of the lower-level `scan_fn`.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        cache: ScanCache,
        *,
        schema_version: str,
    ) -> None:
        self._scheduler = scheduler
        self._cache = cache
        self._schema_version = schema_version

    @property
    def cache(self) -> ScanCache:
        return self._cache

    async def run(
        self,
        plans: Sequence[SourcePlan],
        *,
        scan_id: str,
        detector: DetectorFn,
        on_findings: OnFindingsFn,
    ) -> list[IncrementalResult]:
        """Drive every plan through the cache + scheduler.

        Plans run concurrently; one plan's cache misses do not delay
        another's hits. Each plan's `IncrementalResult` is independent
        so the caller can decide whether one source's failure
        invalidates the whole run.
        """
        coros = [
            self.run_one(
                plan,
                scan_id=scan_id,
                detector=detector,
                on_findings=on_findings,
            )
            for plan in plans
        ]
        return await asyncio.gather(*coros)

    async def run_one(
        self,
        plan: SourcePlan,
        *,
        scan_id: str,
        detector: DetectorFn,
        on_findings: OnFindingsFn,
    ) -> IncrementalResult:
        connector = plan.connector
        kind = connector.kind
        source_id = connector.id
        stats = _MutableStats()
        sub_buffers: dict[str, _SubsourceBuffer] = {}

        skipped = await self._apply_subsource_skip(
            connector=connector,
            kind=kind,
            source_id=source_id,
            sub_buffers=sub_buffers,
            stats=stats,
            on_findings=on_findings,
        )

        scan_fn = self._make_scan_fn(
            kind=kind,
            source_id=source_id,
            detector=detector,
            on_findings=on_findings,
            sub_buffers=sub_buffers,
            stats=stats,
        )

        result = await self._scheduler.run_one(plan, scan_id=scan_id, scan_fn=scan_fn)

        # On clean completion, persist per-sub-source rollups so the
        # next run hits at tier 1. Errored plans skip this — partial
        # rollups would silently shrink the cached findings of an
        # unchanged sub-source on the following run.
        if result.error is None:
            await self._persist_rollups(
                kind=kind,
                source_id=source_id,
                sub_buffers=sub_buffers,
                skipped=skipped,
            )

        return IncrementalResult(source_result=result, cache_stats=stats.freeze())

    async def _apply_subsource_skip(
        self,
        *,
        connector: object,
        kind: str,
        source_id: str,
        sub_buffers: dict[str, _SubsourceBuffer],
        stats: _MutableStats,
        on_findings: OnFindingsFn,
    ) -> set[str]:
        """Look up each sub-source in the cache; on hit, replay its
        findings and tell the connector to skip it. Returns the set of
        sub_ids that were replayed so the caller does not re-cache them
        as fresh rollups.

        Cache lookups fan out across all sub-sources concurrently — at
        org-scan scale (10**3 repos) the sequential variant blocks for
        seconds on SQLite I/O. `on_findings` callbacks for hits run on
        the same task that observed the hit so the caller sees results
        as soon as they are available; ordering across sub-sources is
        not preserved (callers that need a stable order must sort
        downstream).
        """
        if not isinstance(connector, IncrementalSourceConnector):
            return set()
        subsources: Sequence[Subsource] = await connector.list_subsources()
        stats.subsource_total = len(subsources)
        skip: set[str] = set()
        if not subsources:
            connector.set_subsource_skip(frozenset())
            return skip

        # One batched SELECT instead of N round-trips. At org-scan
        # scale (10**3 sub-sources) this is the difference between a
        # 50 ms pre-pass and a multi-second one — the SQLite WAL lock
        # serializes per-statement waits even when we asyncio.gather.
        lookups = [
            CacheLookup(
                key=_sub_cache_key(kind, source_id, sub.sub_id),
                fingerprint=sub.fingerprint,
                schema_version=self._schema_version,
            )
            for sub in subsources
        ]
        hits = await self._cache.get_many(lookups)

        # Cache-hit `on_findings` callbacks fan out concurrently. The
        # caller's findings sink is normally I/O bound (FindingsStore
        # write, OTLP export); awaiting them in series would needlessly
        # serialize work the scheduler is happy to parallelize.
        replay_tasks: list[Awaitable[None]] = []
        for sub, lk in zip(subsources, lookups, strict=True):
            blob = hits.get(lk.key)
            if blob is None:
                stats.subsource_misses += 1
                sub_buffers[sub.sub_id] = _SubsourceBuffer(fingerprint=sub.fingerprint)
                continue
            count, payload = _decode(blob)
            stats.subsource_hits += 1
            skip.add(sub.sub_id)
            replay_tasks.append(
                on_findings(source_id, sub.sub_id, count, payload, True)
            )
        if replay_tasks:
            await asyncio.gather(*replay_tasks)
        connector.set_subsource_skip(frozenset(skip))
        return skip

    def _make_scan_fn(
        self,
        *,
        kind: str,
        source_id: str,
        detector: DetectorFn,
        on_findings: OnFindingsFn,
        sub_buffers: dict[str, _SubsourceBuffer],
        stats: _MutableStats,
    ) -> Callable[[DocumentRef, Document | DocumentChunk], Awaitable[int]]:
        cache = self._cache
        schema_version = self._schema_version

        async def scan_fn(ref: DocumentRef, doc: Document | DocumentChunk) -> int:
            stats.document_total += 1
            sub_id = ref.metadata.get(SUBSOURCE_METADATA_KEY)

            content_fp = _document_fingerprint(doc)
            doc_key = _doc_cache_key(kind, source_id, ref.path)

            blob: bytes | None = None
            if content_fp is not None:
                blob = await cache.get(
                    doc_key,
                    fingerprint=content_fp,
                    schema_version=schema_version,
                )

            if blob is not None:
                count, payload = _decode(blob)
                stats.document_hits += 1
                await on_findings(source_id, sub_id, count, payload, True)
            else:
                count, payload = await detector(ref, doc)
                stats.document_misses += 1
                if content_fp is not None:
                    await cache.put(
                        doc_key,
                        fingerprint=content_fp,
                        schema_version=schema_version,
                        value=_encode(count, payload),
                    )
                await on_findings(source_id, sub_id, count, payload, False)

            if sub_id is not None and sub_id in sub_buffers:
                buf = sub_buffers[sub_id]
                buf.count += count
                buf.payloads.append(payload)
            return count

        return scan_fn

    async def _persist_rollups(
        self,
        *,
        kind: str,
        source_id: str,
        sub_buffers: dict[str, _SubsourceBuffer],
        skipped: set[str],
    ) -> None:
        """Store one cache entry per sub-source we actually scanned.

        Rollups for skipped sub-sources are already cached from a prior
        run; rewriting them is wasted I/O. Writes fan out concurrently;
        the underlying SqliteScanCache serializes through its internal
        writer lock, so on-disk order matches commit order, but the
        Python-side waits run in parallel.
        """
        pending: list[Awaitable[None]] = []
        for sub_id, buf in sub_buffers.items():
            if sub_id in skipped:
                continue
            rollup = _encode(buf.count, b"".join(buf.payloads))
            pending.append(
                self._cache.put(
                    _sub_cache_key(kind, source_id, sub_id),
                    fingerprint=buf.fingerprint,
                    schema_version=self._schema_version,
                    value=rollup,
                )
            )
        if pending:
            await asyncio.gather(*pending)


__all__ = [
    "DetectorFn",
    "IncrementalResult",
    "IncrementalRunner",
    "IncrementalStats",
    "OnFindingsFn",
]
