"""Detector bridge — JSON wire format + DetectorFn integration.

Heavy NER pass is exercised in `test_ner_pass.py` already; here we keep
fixtures small and focus on the bridge: serialization round-trip, the
`DetectorFn` plumbing, and the IncrementalRunner replay equivalence.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from pleno_pii_scanner.detector import (
    DETECTOR_LOGIC_VERSION,
    DETECTOR_WIRE_VERSION,
    decode_findings,
    encode_findings,
    make_detector,
    recognizer_pack_fingerprint,
    schema_components,
)
from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.sources.base import (
    Document,
    DocumentRef,
)


# ---- helpers --------------------------------------------------------


def _ref(path: str = "src/secrets.py") -> DocumentRef:
    return DocumentRef(
        source_id="dir:/tmp/x",
        source_kind="dir",
        path=path,
    )


def _doc(text: str, ref: DocumentRef | None = None) -> Document:
    return Document(
        ref=ref or _ref(),
        text=text,
        fetched_at=datetime.now(UTC),
    )


def _sample_finding(**overrides: object) -> Finding:
    base = dict(
        entity="EMAIL_ADDRESS",
        file="src/x.py",
        line=3,
        col=11,
        score=0.85,
        snippet='SUPPORT = "foo@example.com"',
        matched="foo@example.com",
        pattern_name="presidio",
        verification="passed",
        commit=None,
        author=None,
        date=None,
    )
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


# ---- wire format round-trip ----------------------------------------


class TestWireFormat:
    def test_round_trip_preserves_all_fields(self) -> None:
        findings = [
            _sample_finding(),
            _sample_finding(
                entity="PHONE_NUMBER",
                matched="0120-123-456",
                snippet='SUPPORT = "0120-123-456"',
                score=0.99,
                verification="passed",
                commit="abc",
                author="alice",
                date="2026-05-01",
            ),
        ]
        decoded = decode_findings(encode_findings(findings))
        assert decoded == findings

    def test_empty_payload_yields_empty_list(self) -> None:
        assert decode_findings(b"") == []

    def test_empty_findings_yields_compact_array(self) -> None:
        # Encoding zero findings still produces parseable JSON so the
        # cache layer has no special-case for "no findings detected".
        payload = encode_findings([])
        assert payload == b"[]"
        assert decode_findings(payload) == []

    def test_garbage_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            decode_findings(b"\x00\x01\x02not-json")

    def test_non_array_payload_rejected(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            decode_findings(b'{"not": "an array"}')

    def test_unicode_survives_round_trip(self) -> None:
        # Japanese names stress the (ensure_ascii=False) decision; we
        # want bytes that decode back to the original code points
        # without escape sequences expanding.
        f = _sample_finding(
            entity="PERSON",
            matched="山田太郎",
            snippet="顧客は山田太郎さま",
        )
        out = decode_findings(encode_findings([f]))
        assert out[0].matched == "山田太郎"
        assert "山田太郎" in out[0].snippet

    def test_wire_version_constant_is_stringly_typed(self) -> None:
        # If this changes, every cache entry produced by the previous
        # version of the bridge must roll over. Schema_version takes
        # care of the auto-invalidation; we just verify the constant
        # is the kind of value that hashes cleanly.
        assert isinstance(DETECTOR_WIRE_VERSION, str)
        assert DETECTOR_WIRE_VERSION
        assert isinstance(DETECTOR_LOGIC_VERSION, str)
        assert DETECTOR_LOGIC_VERSION


# ---- recognizer pack fingerprint ----------------------------------


class TestRecognizerPackFingerprint:
    """The recognizer pack hash drives auto-invalidation when a regex
    pack flip ships — package version is intentionally not part of the
    cache key, so this fingerprint carries the load."""

    def test_same_pack_yields_same_hash(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        a = recognizer_pack_fingerprint(ALL_JA_RECOGNIZERS)
        b = recognizer_pack_fingerprint(ALL_JA_RECOGNIZERS)
        assert a == b

    def test_iteration_order_does_not_change_hash(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        normal = recognizer_pack_fingerprint(ALL_JA_RECOGNIZERS)
        reversed_ = recognizer_pack_fingerprint(
            tuple(reversed(ALL_JA_RECOGNIZERS))
        )
        assert normal == reversed_

    def test_dropping_one_recognizer_changes_hash(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        full = recognizer_pack_fingerprint(ALL_JA_RECOGNIZERS)
        partial = recognizer_pack_fingerprint(ALL_JA_RECOGNIZERS[:-1])
        assert full != partial

    def test_pattern_regex_change_flips_hash(self) -> None:
        from pleno_recognizers.types import PiiPattern, PiiRecognizer

        original = (
            PiiRecognizer(
                entity="EMAIL",
                language="en",
                patterns=(PiiPattern(name="basic", regex=r"\w+@\w+", score=0.5),),
                context=("email",),
            ),
        )
        edited = (
            PiiRecognizer(
                entity="EMAIL",
                language="en",
                patterns=(
                    PiiPattern(
                        name="basic", regex=r"[A-Za-z0-9]+@\w+", score=0.5
                    ),
                ),
                context=("email",),
            ),
        )
        assert recognizer_pack_fingerprint(
            original
        ) != recognizer_pack_fingerprint(edited)


class TestSchemaComponents:
    def test_includes_recognizer_fingerprint_and_flags(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        components = schema_components(
            ALL_JA_RECOGNIZERS,
            language="ja",
            entities=None,
            skip_ner=False,
        )
        # Every prefix must be present; downstream callers rely on the
        # tuple shape for clear cache-key debugging.
        prefixes = [c.split("/", 1)[0] for c in components]
        assert "detector-wire" in prefixes
        assert "detector-logic" in prefixes
        assert "recognizers" in prefixes
        assert "lang" in prefixes
        assert "entities" in prefixes
        assert "skip_ner" in prefixes

    def test_skip_ner_flip_changes_components(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        on = schema_components(
            ALL_JA_RECOGNIZERS,
            language="ja",
            entities=None,
            skip_ner=False,
        )
        off = schema_components(
            ALL_JA_RECOGNIZERS,
            language="ja",
            entities=None,
            skip_ner=True,
        )
        assert on != off

    def test_extra_components_appear_at_tail(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        components = schema_components(
            ALL_JA_RECOGNIZERS,
            language="ja",
            entities=None,
            skip_ner=False,
            extra=("model/onnx-v0.13.0",),
        )
        assert components[-1] == "model/onnx-v0.13.0"


# ---- DetectorFn ----------------------------------------------------


class TestMakeDetector:
    @pytest.mark.asyncio
    async def test_empty_text_returns_zero_findings(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        detector = make_detector(ALL_JA_RECOGNIZERS, skip_ner=True)
        count, payload = await detector(_ref(), _doc(""))
        assert count == 0
        assert payload == b""

    @pytest.mark.asyncio
    async def test_regex_only_pass_finds_email(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        detector = make_detector(ALL_JA_RECOGNIZERS, skip_ner=True)
        text = 'SUPPORT_EMAIL = "alice@example.com"\n'
        count, payload = await detector(_ref("emails.py"), _doc(text))
        assert count >= 1
        findings = decode_findings(payload)
        assert any(f.entity == "EMAIL_ADDRESS" for f in findings)
        for f in findings:
            assert f.file == "emails.py"

    @pytest.mark.asyncio
    async def test_skip_verify_keeps_findings_unverified(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        detector = make_detector(
            ALL_JA_RECOGNIZERS, skip_ner=True, skip_verify=True
        )
        text = 'EMAIL = "alice@example.com"\n'
        _, payload = await detector(_ref("e.py"), _doc(text))
        findings = decode_findings(payload)
        assert findings
        # When verify is skipped, every finding stays at its raw
        # detector verification (regex pack default = unverified).
        assert all(f.verification == "unverified" for f in findings)

    @pytest.mark.asyncio
    async def test_concurrent_invocations_are_safe(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        detector = make_detector(ALL_JA_RECOGNIZERS, skip_ner=True)
        text = 'SUPPORT = "alice@example.com"\n'
        results = await asyncio.gather(
            *(detector(_ref(f"f{i}.py"), _doc(text)) for i in range(8))
        )
        assert {c for c, _ in results} == {results[0][0]}
        # File attribution must be the per-call ref's path, not a
        # singleton state leak across coroutines.
        for i, (_, payload) in enumerate(results):
            findings = decode_findings(payload)
            assert findings
            assert all(f.file == f"f{i}.py" for f in findings)

    @pytest.mark.asyncio
    async def test_binary_payload_decodes_via_utf8_fallback(self) -> None:
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        detector = make_detector(ALL_JA_RECOGNIZERS, skip_ner=True)
        binary_doc = Document(
            ref=_ref("b.bin"),
            binary=b'TOKEN = "alice@example.com"',
            fetched_at=datetime.now(UTC),
        )
        count, payload = await detector(_ref("b.bin"), binary_doc)
        assert count >= 1
        findings = decode_findings(payload)
        assert any(f.entity == "EMAIL_ADDRESS" for f in findings)


# ---- IncrementalRunner replay equivalence --------------------------


class TestRunnerReplay:
    """A cache hit must produce findings byte-identical to a fresh
    detector call. This is the single most important property of the
    bridge: if the wire format mutates between encode and decode, the
    "incremental" feature silently degrades scan quality.
    """

    @pytest.mark.asyncio
    async def test_first_run_misses_second_run_replays_identical(
        self,
    ) -> None:
        from collections.abc import AsyncIterator

        from pleno_pii_scanner.scheduler import (
            GlobalRateLimiter,
            IncrementalRunner,
            Scheduler,
            SchedulerConfig,
            SourcePlan,
        )
        from pleno_pii_scanner.sources.base import (
            Capabilities,
            DocumentChunk,
            SourceFilter,
        )
        from pleno_pii_scanner.state import MemoryScanCache
        from pleno_recognizers.ja import ALL_JA_RECOGNIZERS

        # Simple flat connector with two text documents.
        class _C:
            id = "test"
            kind = "test"

            async def discover(
                self, _filter: SourceFilter, _cursor: str | None
            ) -> AsyncIterator[DocumentRef]:
                for path in ("a.py", "b.py"):
                    yield DocumentRef(source_id=self.id, source_kind=self.kind, path=path)

            async def fetch(
                self, ref: DocumentRef
            ) -> AsyncIterator[Document | DocumentChunk]:
                body = (
                    'EMAIL = "alice@example.com"\n'
                    if ref.path == "a.py"
                    else 'TOKEN = "bob@example.com"\n'
                )
                yield Document(ref=ref, text=body, content_hash=f"h:{body}")

            def capabilities(self) -> Capabilities:
                return Capabilities()

            async def close(self) -> None:
                return None

        sch = Scheduler(
            config=SchedulerConfig(per_source_concurrency=2),
            rate_limiter=GlobalRateLimiter(),
        )
        cache = MemoryScanCache()
        detector = make_detector(ALL_JA_RECOGNIZERS, skip_ner=True)

        first_findings: list[tuple[bool, Finding]] = []
        second_findings: list[tuple[bool, Finding]] = []

        async def collect(buf):
            async def emit(_sid, _sub, _count, payload, replayed):
                for f in decode_findings(payload):
                    buf.append((replayed, f))

            return emit

        try:
            runner = IncrementalRunner(sch, cache, schema_version="v1")
            await runner.run(
                [SourcePlan(connector=_C())],
                scan_id="run-1",
                detector=detector,
                on_findings=await collect(first_findings),
            )
            await runner.run(
                [SourcePlan(connector=_C())],
                scan_id="run-2",
                detector=detector,
                on_findings=await collect(second_findings),
            )
        finally:
            await sch.close()
            await cache.close()

        # Run 1: nothing replayed.
        assert first_findings
        assert all(not r for r, _ in first_findings)
        # Run 2: everything replayed.
        assert second_findings
        assert all(r for r, _ in second_findings)
        # Equality: replayed findings == fresh findings.
        first_set = {f for _, f in first_findings}
        second_set = {f for _, f in second_findings}
        assert first_set == second_set
