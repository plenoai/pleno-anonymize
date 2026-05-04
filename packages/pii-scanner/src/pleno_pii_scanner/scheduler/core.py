"""Scheduler — drives discover/fetch across multiple SourceConnectors.

The Scheduler ties together every core abstraction: SourceConnector
(#3) yields refs and payloads, CredentialBroker (#5) supplies tokens,
CheckpointStore (#6) persists resume cursors and shard receipts,
ContentExtractor (#8) post-processes payloads into scannable text,
GlobalRateLimiter throttles per (kind, tenant), and `retry_async`
wraps every connector boundary in jittered exponential backoff.

What the Scheduler intentionally does NOT do:
  * Run the regex / NER detector — that is the existing `regex_pass` /
    `ner_pass` pipeline. Scheduler emits text fragments; detectors
    consume them. Keeps detector compute (which may live in a worker
    process) decoupled from I/O orchestration.
  * Persist findings — that is FindingsStore (#9). Scheduler invokes the
    `on_findings` callback the caller provides.
  * Make scan-vs-skip decisions — that is the SuppressionEngine (#10).
    Scheduler delegates via a `should_scan` callback.

ADR-0007 §4.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from pleno_pii_scanner.sources.base import (
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.scheduler.rate_limit import (
    BucketKey,
    GlobalRateLimiter,
    RateLimited,
)
from pleno_pii_scanner.scheduler.retry import (
    RetryConfig,
    retry_async,
)


# Callback that produces zero or more findings from a fetched document.
# Returns the count emitted so the scheduler can update shard receipts.
ScanFn = Callable[[DocumentRef, Document | DocumentChunk], Awaitable[int]]

# Callback consulted before fetching a ref. Returning False skips it.
ShouldScanFn = Callable[[DocumentRef], Awaitable[bool]]

# Callback awaited after each batch (default 100 docs) so the caller can
# checkpoint, flush findings, etc. Receives the number of docs in this
# batch and the latest cursor (if the connector supplied one via
# DocumentRef.metadata['_cursor']).
OnBatchFn = Callable[[str, str, int, Cursor | None], Awaitable[None]]


class CheckpointStoreProtocol(Protocol):
    """Subset of CheckpointStore (#6) the Scheduler actually depends on.

    Re-declared here as a Protocol to keep `scheduler` a leaf module
    that does not import the heavy `state` package — useful for tests
    that pass a fake. Real callers pass `pleno_pii_scanner.state.SqliteCheckpointStore`
    which structurally satisfies this.
    """

    async def save(self, cp: object) -> None: ...
    async def load(
        self, scan_id: str, source_id: str
    ) -> object | None: ...


@dataclass(frozen=True, slots=True)
class SourcePlan:
    """One source's slice of a multi-source scan job.

    `tenant_id` defaults to `connector.id` so per-connector rate buckets
    are independent by default; operators can pin multiple connectors to
    the same tenant when they share an upstream quota (e.g. two Slack
    connectors hitting the same workspace).
    """

    connector: SourceConnector
    filter: SourceFilter = field(default_factory=SourceFilter)
    tenant_id: str | None = None

    def resolved_tenant(self) -> str:
        return self.tenant_id if self.tenant_id is not None else self.connector.id


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    """Top-level scheduler tuning.

    `batch_size` controls how often the on_batch callback fires (the
    smaller, the more crash-resilient; the larger, the more efficient).
    `per_source_concurrency` and `global_concurrency` bound parallel
    fetches; the smaller of the two binds at runtime.
    """

    batch_size: int = 100
    per_source_concurrency: int = 8
    global_concurrency: int = 64
    fetch_retry: RetryConfig = field(default_factory=RetryConfig)
    discover_retry: RetryConfig = field(
        default_factory=lambda: RetryConfig(max_attempts=3)
    )
    rate_acquire_timeout: float | None = 30.0


@dataclass(frozen=True, slots=True)
class SourceResult:
    """Per-source roll-up returned to the caller after run_one finishes."""

    source_id: str
    source_kind: str
    refs_seen: int
    docs_fetched: int
    findings_emitted: int
    started_at: datetime
    completed_at: datetime
    error: str | None = None


class Scheduler:
    """Drive scans across SourceConnectors.

    Lifecycle:
      1. caller builds `SchedulerConfig` + `GlobalRateLimiter` + optional
         `CheckpointStoreProtocol` and constructs a Scheduler
      2. caller calls `await scheduler.run(plans, scan_id, scan_fn, ...)`
         passing the per-source `SourcePlan` list and the scan callback
      3. scheduler returns a list of `SourceResult` (one per plan)
      4. caller closes the scheduler (no-op today; here for symmetry and
         future resource cleanup of background tasks)

    Concurrency model:
      * each SourcePlan owns an asyncio.Task driving discover()
      * within a plan, fetches are parallelised up to
        `per_source_concurrency` via an asyncio.Semaphore
      * across all plans, total in-flight fetches are bounded by
        `global_concurrency`
      * rate limiter throttles per (connector_kind, tenant_id) before
        every fetch — does NOT throttle discover() since most
        connectors paginate within their own rate budget already
    """

    def __init__(
        self,
        *,
        config: SchedulerConfig | None = None,
        rate_limiter: GlobalRateLimiter | None = None,
        checkpoint_store: CheckpointStoreProtocol | None = None,
        on_throttle: Callable[[BucketKey], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config or SchedulerConfig()
        self._rate = rate_limiter or GlobalRateLimiter()
        self._cp = checkpoint_store
        self._on_throttle = on_throttle
        self._global_sem = asyncio.Semaphore(self._config.global_concurrency)

    async def run(
        self,
        plans: Sequence[SourcePlan],
        *,
        scan_id: str,
        scan_fn: ScanFn,
        should_scan: ShouldScanFn | None = None,
        on_batch: OnBatchFn | None = None,
    ) -> list[SourceResult]:
        """Drive every `plan` concurrently and collect per-source results.

        Each plan runs to completion independently; one connector raising
        an exception sets `SourceResult.error` for that plan but does not
        cancel siblings. The caller decides how to escalate.
        """
        coros = [
            self.run_one(
                plan,
                scan_id=scan_id,
                scan_fn=scan_fn,
                should_scan=should_scan,
                on_batch=on_batch,
            )
            for plan in plans
        ]
        return await asyncio.gather(*coros)

    async def run_one(
        self,
        plan: SourcePlan,
        *,
        scan_id: str,
        scan_fn: ScanFn,
        should_scan: ShouldScanFn | None = None,
        on_batch: OnBatchFn | None = None,
    ) -> SourceResult:
        """Drive a single plan: discover → (rate-limited, parallel) fetch → scan."""
        connector = plan.connector
        bucket = BucketKey(
            connector_kind=connector.kind,
            tenant_id=plan.resolved_tenant(),
        )
        per_source_sem = asyncio.Semaphore(self._config.per_source_concurrency)
        started_at = datetime.now(UTC)
        refs_seen = 0
        docs_fetched = 0
        findings_emitted = 0
        last_cursor: Cursor | None = None
        batch_count = 0
        error: str | None = None

        cursor = await self._load_cursor(scan_id, connector.id)

        try:
            async for ref in self._discover_with_retry(connector, plan.filter, cursor):
                refs_seen += 1
                if should_scan is not None and not await should_scan(ref):
                    continue
                # Discover handed us a tracking cursor via metadata?
                ref_cursor = ref.metadata.get("_cursor")
                if isinstance(ref_cursor, str):
                    last_cursor = ref_cursor
                # Schedule the fetch — wait for both per-source and
                # global capacity, then for a rate-limit token.
                result = await self._fetch_and_scan(
                    connector=connector,
                    ref=ref,
                    bucket=bucket,
                    per_source_sem=per_source_sem,
                    scan_fn=scan_fn,
                )
                docs_fetched += result[0]
                findings_emitted += result[1]
                batch_count += 1
                if batch_count >= self._config.batch_size and on_batch is not None:
                    await on_batch(scan_id, connector.id, batch_count, last_cursor)
                    batch_count = 0
            if batch_count > 0 and on_batch is not None:
                await on_batch(scan_id, connector.id, batch_count, last_cursor)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await connector.close()

        return SourceResult(
            source_id=connector.id,
            source_kind=connector.kind,
            refs_seen=refs_seen,
            docs_fetched=docs_fetched,
            findings_emitted=findings_emitted,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            error=error,
        )

    async def _discover_with_retry(
        self,
        connector: SourceConnector,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        # We deliberately only retry the kickoff. Mid-stream discover
        # failures are the connector's responsibility to recover from
        # (typically by re-paginating from the last successful cursor).
        # Restarting the whole stream from the scheduler would either
        # lose progress or duplicate refs across the failure boundary.
        # An explicit `_start()` wrapper gives one retry opportunity
        # for transient connection failures during the very first call.
        @retry_async(self._config.discover_retry)
        async def _start() -> AsyncIterator[DocumentRef]:
            return connector.discover(filter, cursor)

        agen = await _start()
        async for ref in agen:
            yield ref

    async def _fetch_and_scan(
        self,
        *,
        connector: SourceConnector,
        ref: DocumentRef,
        bucket: BucketKey,
        per_source_sem: asyncio.Semaphore,
        scan_fn: ScanFn,
    ) -> tuple[int, int]:
        """Returns (docs_fetched, findings_emitted) for this ref."""
        async with per_source_sem, self._global_sem:
            try:
                await self._rate.acquire(
                    bucket, timeout=self._config.rate_acquire_timeout
                )
            except RateLimited:
                # Surface as a soft failure for this ref — caller's
                # checkpoint will let us pick it up next run. Tightening
                # this to a hard error would break long scans on flaky
                # tenants.
                if self._on_throttle is not None:
                    await self._on_throttle(bucket)
                return (0, 0)

            fetched_count = 0
            findings_count = 0
            try:
                async for doc in self._fetch_with_retry(connector, ref, bucket):
                    fetched_count += 1
                    findings_count += await scan_fn(ref, doc)
                await self._rate.on_success(bucket)
                return (fetched_count, findings_count)
            except Exception:
                await self._rate.on_throttle_signal(bucket)
                if self._on_throttle is not None:
                    await self._on_throttle(bucket)
                raise

    async def _fetch_with_retry(
        self,
        connector: SourceConnector,
        ref: DocumentRef,
        bucket: BucketKey,
    ) -> AsyncIterator[Document | DocumentChunk]:
        # The retry decorator must observe failures that happen during
        # iteration (most SDK errors raise on the first __anext__, not on
        # the iterator-construction call). We materialise the fetch into
        # a list inside the wrapped coroutine so the retry boundary
        # encloses the actual network calls. Bounded by ContentExtractor's
        # --max-doc-bytes; chunked streams stay within the connector's
        # advertised in-memory budget.
        async def on_retry(_attempt: int, _exc: BaseException, _delay: float) -> None:
            await self._rate.on_throttle_signal(bucket)

        @retry_async(self._config.fetch_retry, on_retry=on_retry)
        async def _materialise() -> list[Document | DocumentChunk]:
            return [doc async for doc in connector.fetch(ref)]

        for doc in await _materialise():
            yield doc

    async def _load_cursor(self, scan_id: str, source_id: str) -> Cursor | None:
        if self._cp is None:
            return None
        cp = await self._cp.load(scan_id, source_id)
        if cp is None:
            return None
        # CheckpointStore returns Checkpoint dataclass with `cursor` attr;
        # we deliberately depend on duck typing to avoid importing the
        # state package and inverting the layering.
        return getattr(cp, "cursor", None)

    async def close(self) -> None:
        """Reserved for future resource cleanup."""
        return None
