"""IncrementalRunner — sub-source + document level cache short-circuiting.

These tests use lightweight in-memory connectors so they exercise the
runner's orchestration without needing real git / network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import pytest

from pleno_pii_scanner.scheduler import (
    GlobalRateLimiter,
    IncrementalRunner,
    Scheduler,
    SchedulerConfig,
    SourcePlan,
)
from pleno_pii_scanner.sources.base import (
    SUBSOURCE_METADATA_KEY,
    Capabilities,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceFilter,
    Subsource,
)
from pleno_pii_scanner.state import MemoryScanCache


# --- Test doubles ----------------------------------------------------------


@dataclass
class _FlatConnector:
    """Bare-minimum SourceConnector with no sub-source hierarchy."""

    id: str = "flat"
    kind: str = "flat"
    refs: tuple[tuple[str, str], ...] = ()  # (path, body)

    async def discover(
        self, filter: SourceFilter, cursor: str | None
    ) -> AsyncIterator[DocumentRef]:
        del cursor
        for path, _ in self.refs:
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=path,
            )

    async def fetch(self, ref: DocumentRef) -> AsyncIterator[Document | DocumentChunk]:
        body = next(b for p, b in self.refs if p == ref.path)
        yield Document(ref=ref, text=body, content_hash=f"h:{body}")

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def close(self) -> None:
        return None


@dataclass
class _HierarchicalConnector:
    """SourceConnector that opts into IncrementalSourceConnector.

    `sub_layout` is `{sub_id: (fingerprint, [(path, body), ...])}`. Each
    sub-source's docs surface the SUBSOURCE_METADATA_KEY tag the runner
    needs to attribute findings.
    """

    id: str = "hier"
    kind: str = "hier"
    sub_layout: dict[str, tuple[str, list[tuple[str, str]]]] = field(
        default_factory=dict
    )
    skip: frozenset[str] = field(default_factory=frozenset)
    list_calls: int = 0
    discover_subs: list[str] = field(default_factory=list)

    async def list_subsources(self) -> Sequence[Subsource]:
        self.list_calls += 1
        return tuple(
            Subsource(sub_id=sid, fingerprint=fp)
            for sid, (fp, _) in self.sub_layout.items()
        )

    def set_subsource_skip(self, skip: frozenset[str]) -> None:
        self.skip = skip

    async def discover(
        self, filter: SourceFilter, cursor: str | None
    ) -> AsyncIterator[DocumentRef]:
        del cursor
        for sub_id, (_, items) in self.sub_layout.items():
            if sub_id in self.skip:
                continue
            self.discover_subs.append(sub_id)
            for path, _body in items:
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=f"{sub_id}/{path}",
                    metadata={SUBSOURCE_METADATA_KEY: sub_id},
                )

    async def fetch(self, ref: DocumentRef) -> AsyncIterator[Document | DocumentChunk]:
        sub_id = ref.metadata[SUBSOURCE_METADATA_KEY]
        path = ref.path[len(sub_id) + 1 :]
        body = next(b for p, b in self.sub_layout[sub_id][1] if p == path)
        yield Document(ref=ref, text=body, content_hash=f"h:{body}")

    def capabilities(self) -> Capabilities:
        return Capabilities(incremental=True, content_hash_delta=True)

    async def close(self) -> None:
        return None


def _runner_pieces() -> tuple[Scheduler, MemoryScanCache]:
    return (
        Scheduler(
            config=SchedulerConfig(per_source_concurrency=2),
            rate_limiter=GlobalRateLimiter(),
        ),
        MemoryScanCache(),
    )


def _detector_returning(per_doc_count: int = 1):
    """Build a `DetectorFn` that emits a known count + payload per doc."""

    async def detector(
        ref: DocumentRef, _doc: Document | DocumentChunk
    ) -> tuple[int, bytes]:
        return per_doc_count, f"finding:{ref.path}".encode()

    return detector


def _collect_findings():
    """Build a recording `OnFindingsFn`."""

    received: list[tuple[str, str | None, int, bytes, bool]] = []

    async def on_findings(
        source_id: str,
        sub_id: str | None,
        count: int,
        payload: bytes,
        replayed: bool,
    ) -> None:
        received.append((source_id, sub_id, count, payload, replayed))

    return received, on_findings


# --- Document-level cache --------------------------------------------------


class TestDocumentLevelCache:
    @pytest.mark.asyncio
    async def test_first_run_misses_second_run_hits(self) -> None:
        sch, cache = _runner_pieces()
        runner = IncrementalRunner(sch, cache, schema_version="v1")
        connector = _FlatConnector(refs=(("a.txt", "alpha"), ("b.txt", "bravo")))
        plan = SourcePlan(connector=connector)

        try:
            received, on_findings = _collect_findings()
            results = await runner.run(
                [plan],
                scan_id="run-1",
                detector=_detector_returning(2),
                on_findings=on_findings,
            )
            assert results[0].cache_stats.document_total == 2
            assert results[0].cache_stats.document_misses == 2
            assert results[0].cache_stats.document_hits == 0
            assert all(not replayed for *_, replayed in received)

            # Re-create the connector — same content, fresh discover.
            connector2 = _FlatConnector(refs=(("a.txt", "alpha"), ("b.txt", "bravo")))
            plan2 = SourcePlan(connector=connector2)
            received2, on_findings2 = _collect_findings()
            results2 = await runner.run(
                [plan2],
                scan_id="run-2",
                detector=_detector_returning(2),
                on_findings=on_findings2,
            )
            assert results2[0].cache_stats.document_hits == 2
            assert results2[0].cache_stats.document_misses == 0
            assert all(replayed for *_, replayed in received2)
        finally:
            await sch.close()
            await cache.close()

    @pytest.mark.asyncio
    async def test_changed_content_misses(self) -> None:
        sch, cache = _runner_pieces()
        runner = IncrementalRunner(sch, cache, schema_version="v1")

        connector1 = _FlatConnector(refs=(("x.txt", "old"),))
        _, on_findings = _collect_findings()
        await runner.run(
            [SourcePlan(connector=connector1)],
            scan_id="r1",
            detector=_detector_returning(1),
            on_findings=on_findings,
        )

        connector2 = _FlatConnector(refs=(("x.txt", "NEW"),))
        _, on_findings2 = _collect_findings()
        results = await runner.run(
            [SourcePlan(connector=connector2)],
            scan_id="r2",
            detector=_detector_returning(1),
            on_findings=on_findings2,
        )
        try:
            assert results[0].cache_stats.document_misses == 1
            assert results[0].cache_stats.document_hits == 0
        finally:
            await sch.close()
            await cache.close()

    @pytest.mark.asyncio
    async def test_schema_version_bump_invalidates(self) -> None:
        sch, cache = _runner_pieces()
        runner_v1 = IncrementalRunner(sch, cache, schema_version="v1")
        connector = _FlatConnector(refs=(("a.txt", "x"),))
        _, on_findings = _collect_findings()
        await runner_v1.run(
            [SourcePlan(connector=connector)],
            scan_id="r1",
            detector=_detector_returning(1),
            on_findings=on_findings,
        )

        runner_v2 = IncrementalRunner(sch, cache, schema_version="v2")
        connector2 = _FlatConnector(refs=(("a.txt", "x"),))
        _, on_findings2 = _collect_findings()
        results = await runner_v2.run(
            [SourcePlan(connector=connector2)],
            scan_id="r2",
            detector=_detector_returning(1),
            on_findings=on_findings2,
        )
        try:
            assert results[0].cache_stats.document_misses == 1
        finally:
            await sch.close()
            await cache.close()


# --- Sub-source level cache ------------------------------------------------


class TestSubsourceCache:
    @pytest.mark.asyncio
    async def test_unchanged_subsource_skipped_on_second_run(self) -> None:
        sch, cache = _runner_pieces()
        runner = IncrementalRunner(sch, cache, schema_version="v1")

        layout = {
            "repo-a": ("sha-aaa", [("f1.py", "alpha")]),
            "repo-b": ("sha-bbb", [("f2.py", "bravo")]),
        }
        c1 = _HierarchicalConnector(sub_layout=layout)
        _, on_findings1 = _collect_findings()
        r1 = await runner.run(
            [SourcePlan(connector=c1)],
            scan_id="run-1",
            detector=_detector_returning(3),
            on_findings=on_findings1,
        )
        assert r1[0].cache_stats.subsource_hits == 0
        assert r1[0].cache_stats.subsource_misses == 2
        assert sorted(c1.discover_subs) == ["repo-a", "repo-b"]

        # Same fingerprints → both skipped on second run.
        c2 = _HierarchicalConnector(sub_layout=layout)
        received2, on_findings2 = _collect_findings()
        r2 = await runner.run(
            [SourcePlan(connector=c2)],
            scan_id="run-2",
            detector=_detector_returning(3),
            on_findings=on_findings2,
        )
        try:
            assert r2[0].cache_stats.subsource_hits == 2
            assert r2[0].cache_stats.subsource_misses == 0
            # Connector.discover never visited any sub-source.
            assert c2.discover_subs == []
            # All emitted findings came from the cache (replayed=True).
            assert received2 and all(replayed for *_, replayed in received2)
            # Per-sub-source counts match the rollup we stored on run 1.
            counts_by_sub = {sub: count for _, sub, count, _, _ in received2}
            assert counts_by_sub == {"repo-a": 3, "repo-b": 3}
        finally:
            await sch.close()
            await cache.close()

    @pytest.mark.asyncio
    async def test_partial_change_only_rescans_changed_subsource(self) -> None:
        sch, cache = _runner_pieces()
        runner = IncrementalRunner(sch, cache, schema_version="v1")

        c1 = _HierarchicalConnector(
            sub_layout={
                "repo-a": ("sha-1", [("a.py", "x")]),
                "repo-b": ("sha-2", [("b.py", "y")]),
            }
        )
        _, on_findings1 = _collect_findings()
        await runner.run(
            [SourcePlan(connector=c1)],
            scan_id="r1",
            detector=_detector_returning(1),
            on_findings=on_findings1,
        )

        # repo-b's fingerprint flips — repo-a should still hit.
        c2 = _HierarchicalConnector(
            sub_layout={
                "repo-a": ("sha-1", [("a.py", "x")]),
                "repo-b": ("sha-2-new", [("b.py", "y2")]),
            }
        )
        _, on_findings2 = _collect_findings()
        r2 = await runner.run(
            [SourcePlan(connector=c2)],
            scan_id="r2",
            detector=_detector_returning(1),
            on_findings=on_findings2,
        )
        try:
            assert r2[0].cache_stats.subsource_hits == 1
            assert r2[0].cache_stats.subsource_misses == 1
            # discover only walked the changed sub-source.
            assert c2.discover_subs == ["repo-b"]
        finally:
            await sch.close()
            await cache.close()

    @pytest.mark.asyncio
    async def test_errored_plan_does_not_cache_rollup(self) -> None:
        sch, cache = _runner_pieces()
        runner = IncrementalRunner(sch, cache, schema_version="v1")

        layout = {"repo": ("sha-x", [("f.py", "v1")])}
        c1 = _HierarchicalConnector(sub_layout=layout)

        async def failing_detector(
            _ref: DocumentRef, _doc: Document | DocumentChunk
        ) -> tuple[int, bytes]:
            raise RuntimeError("detector blew up")

        _, on_findings = _collect_findings()
        r1 = await runner.run(
            [SourcePlan(connector=c1)],
            scan_id="r1",
            detector=failing_detector,
            on_findings=on_findings,
        )
        # The scheduler captures the detector error onto SourceResult.error.
        assert r1[0].source_result.error is not None

        # A clean rerun must MISS, not silently hit a half-formed entry.
        c2 = _HierarchicalConnector(sub_layout=layout)
        _, on_findings2 = _collect_findings()
        r2 = await runner.run(
            [SourcePlan(connector=c2)],
            scan_id="r2",
            detector=_detector_returning(1),
            on_findings=on_findings2,
        )
        try:
            assert r2[0].cache_stats.subsource_misses == 1
            assert r2[0].cache_stats.subsource_hits == 0
        finally:
            await sch.close()
            await cache.close()


# --- Schedule-stat propagation ---------------------------------------------


class TestSchedulerCounts:
    @pytest.mark.asyncio
    async def test_findings_count_reflects_detector_return(self) -> None:
        sch, cache = _runner_pieces()
        runner = IncrementalRunner(sch, cache, schema_version="v1")
        connector = _FlatConnector(refs=(("a", "1"), ("b", "2")))
        _, on_findings = _collect_findings()
        try:
            r = await runner.run(
                [SourcePlan(connector=connector)],
                scan_id="run",
                detector=_detector_returning(5),
                on_findings=on_findings,
            )
            assert r[0].source_result.findings_emitted == 10
        finally:
            await sch.close()
            await cache.close()
