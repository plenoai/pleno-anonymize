"""MemoryFindingsStore: dedup, query, audit hook, secret hygiene."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pleno_pii_scanner.findings_store import (
    InMemoryKekProvider,
    MemoryFindingsStore,
)
from pleno_pii_scanner.findings_store.base import default_severity
from pleno_pii_scanner.models import Finding


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = dict(
        entity="EMAIL_ADDRESS",
        file="src/app.py",
        line=10,
        col=4,
        score=0.9,
        snippet="contact alice@example.com please",
        matched="alice@example.com",
        pattern_name="email_address",
        verification="unverified",
    )
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


@pytest.fixture
def kek(capsys: pytest.CaptureFixture[str]) -> InMemoryKekProvider:
    k = InMemoryKekProvider()
    capsys.readouterr()
    return k


class TestSaveAndQuery:
    @pytest.mark.asyncio
    async def test_save_then_get(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            ref = await store.save_findings("scan-1", "src-a", [_finding()])
            assert ref.finding_count == 1
            records = await store.query(scan_id="scan-1")
            assert len(records) == 1
            got = await store.get(records[0].finding_id)
            assert got is not None
            assert got.entity == "EMAIL_ADDRESS"
            assert got.value_excerpt != "alice@example.com"

    @pytest.mark.asyncio
    async def test_query_filters(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek, source_kind="dir") as store:
            await store.save_findings(
                "scan-1",
                "src-a",
                [
                    _finding(matched="a@x", entity="EMAIL_ADDRESS"),
                    _finding(
                        matched="AKIA0000000000000000",
                        entity="AWS_ACCESS_KEY_ID",
                        verification="passed",
                    ),
                    _finding(
                        matched="failedval",
                        entity="EMAIL_ADDRESS",
                        verification="failed",
                    ),
                ],
            )
            assert len(await store.query(scan_id="scan-1")) == 3
            assert len(await store.query(entity="EMAIL_ADDRESS")) == 2
            assert len(await store.query(severity="critical")) == 1
            assert len(await store.query(severity="low")) == 1
            assert len(await store.query(verification="passed")) == 1
            assert len(await store.query(source_kind="dir")) == 3
            assert len(await store.query(source_kind="git")) == 0
            assert len(await store.query(status="open")) == 3

    @pytest.mark.asyncio
    async def test_pagination(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            findings = [_finding(matched=f"v{i}") for i in range(10)]
            await store.save_findings("scan-1", "src-a", findings)
            page1 = await store.query(scan_id="scan-1", limit=4, offset=0)
            page2 = await store.query(scan_id="scan-1", limit=4, offset=4)
            assert len(page1) == 4
            assert len(page2) == 4
            assert {r.finding_id for r in page1} & {
                r.finding_id for r in page2
            } == set()

    @pytest.mark.asyncio
    async def test_dedup_within_scan(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            f1 = _finding(matched="dup@example.com")
            f2 = _finding(matched="dup@example.com", line=99)
            await store.save_findings("scan-1", "src-a", [f1])
            await store.save_findings("scan-1", "src-a", [f2])
            records = await store.query(scan_id="scan-1")
            assert len(records) == 1
            assert records[0].line == 99

    @pytest.mark.asyncio
    async def test_get_missing_returns_none(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            assert await store.get("absent") is None


class TestRevealValueAudit:
    @pytest.mark.asyncio
    async def test_reveal_round_trip(self, kek: InMemoryKekProvider) -> None:
        spy: list[tuple[str, str]] = []

        def hook(fid: str, who: str) -> None:
            spy.append((fid, who))

        async with MemoryFindingsStore(kek=kek, audit_hook=hook) as store:
            ref = await store.save_findings(
                "scan-1", "src-a", [_finding(matched="alice@example.com")]
            )
            assert ref.finding_count == 1
            recs = await store.query(scan_id="scan-1")
            value = await store.reveal_value(
                recs[0].finding_id, audit_principal="alice@ops"
            )
            assert value == "alice@example.com"
            assert spy == [(recs[0].finding_id, "alice@ops")]

    @pytest.mark.asyncio
    async def test_audit_hook_async(self, kek: InMemoryKekProvider) -> None:
        spy: list[str] = []

        async def hook(fid: str, who: str) -> None:
            await asyncio.sleep(0)
            spy.append(who)

        async with MemoryFindingsStore(kek=kek, audit_hook=hook) as store:
            ref = await store.save_findings(
                "scan-1", "src-a", [_finding(matched="x@y")]
            )
            assert ref.finding_count == 1
            recs = await store.query(scan_id="scan-1")
            await store.reveal_value(recs[0].finding_id, audit_principal="bob@ops")
            assert spy == ["bob@ops"]

    @pytest.mark.asyncio
    async def test_reveal_missing_audits_then_raises(
        self, kek: InMemoryKekProvider
    ) -> None:
        spy: list[Any] = []

        def hook(fid: str, who: str) -> None:
            spy.append((fid, who))

        async with MemoryFindingsStore(kek=kek, audit_hook=hook) as store:
            with pytest.raises(KeyError):
                await store.reveal_value("absent", audit_principal="ops")
            assert spy == [("absent", "ops")]

    @pytest.mark.asyncio
    async def test_no_hook_is_noop(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            ref = await store.save_findings(
                "scan-1", "src-a", [_finding(matched="y@z")]
            )
            recs = await store.query()
            assert ref.finding_count == 1
            value = await store.reveal_value(recs[0].finding_id, audit_principal="ops")
            assert value == "y@z"


class TestSecretHygiene:
    @pytest.mark.asyncio
    async def test_repr_no_raw(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="leaky-secret-123")]
            )
            recs = await store.query()
            assert "leaky-secret-123" not in repr(recs[0])
            assert "leaky-secret-123" not in str(recs[0])

    @pytest.mark.asyncio
    async def test_log_format_no_raw(
        self, kek: InMemoryKekProvider, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        async with MemoryFindingsStore(kek=kek) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="sneaky-pw-321")]
            )
            recs = await store.query()
            with caplog.at_level(logging.INFO):
                logging.info("record=%s", recs[0])
        assert "sneaky-pw-321" not in caplog.text

    @pytest.mark.asyncio
    async def test_exception_message_no_raw(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="trace-leak-999")]
            )
            recs = await store.query()
            try:
                raise RuntimeError(f"context: {recs[0]}")
            except RuntimeError as exc:
                assert "trace-leak-999" not in str(exc)


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_use_after_close_raises(self, kek: InMemoryKekProvider) -> None:
        store = MemoryFindingsStore(kek=kek)
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.save_findings("scan-1", "src-a", [_finding()])
        with pytest.raises(RuntimeError, match="closed"):
            await store.query()
        with pytest.raises(RuntimeError, match="closed"):
            await store.get("x")
        with pytest.raises(RuntimeError, match="closed"):
            await store.reveal_value("x", audit_principal="ops")

    @pytest.mark.asyncio
    async def test_close_idempotent(self, kek: InMemoryKekProvider) -> None:
        store = MemoryFindingsStore(kek=kek)
        await store.close()
        await store.close()


class TestCustomSeverityClassifier:
    @pytest.mark.asyncio
    async def test_classifier_overrides_default(self, kek: InMemoryKekProvider) -> None:
        def always_low(_: Finding) -> str:
            return "low"

        async with MemoryFindingsStore(
            kek=kek,
            severity_classifier=always_low,  # type: ignore[arg-type]
        ) as store:
            await store.save_findings(
                "scan-1",
                "src-a",
                [
                    _finding(
                        entity="AWS_SECRET_ACCESS_KEY",
                        verification="passed",
                    )
                ],
            )
            recs = await store.query()
            assert recs[0].severity == "low"

    @pytest.mark.asyncio
    async def test_default_classifier_used_when_none(
        self, kek: InMemoryKekProvider
    ) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            await store.save_findings(
                "scan-1",
                "src-a",
                [
                    _finding(
                        entity="AWS_SECRET_ACCESS_KEY",
                        verification="passed",
                    )
                ],
            )
            recs = await store.query()
            f = _finding(entity="AWS_SECRET_ACCESS_KEY", verification="passed")
            assert recs[0].severity == default_severity(f)


class TestEmptySaves:
    @pytest.mark.asyncio
    async def test_empty_findings_returns_zero(self, kek: InMemoryKekProvider) -> None:
        async with MemoryFindingsStore(kek=kek) as store:
            ref = await store.save_findings("scan-1", "src-a", [])
            assert ref.finding_count == 0
            assert ref.shard_index == 0


class TestDekRehydration:
    @pytest.mark.asyncio
    async def test_unwrap_path_when_cache_dropped(
        self, kek: InMemoryKekProvider
    ) -> None:
        # WHY: covers the else-branch in _ensure_dek that runs when a
        # previously-stored wrapped DEK exists but the in-process cache
        # has been cleared (mimics a future cache eviction policy).
        store = MemoryFindingsStore(kek=kek)
        try:
            await store._ensure_dek()
            store._dek_cache.clear()
            dek = await store._ensure_dek()
            assert len(dek) == 32
        finally:
            await store.close()


class TestDecryptedPayloadShape:
    @pytest.mark.asyncio
    async def test_missing_matched_field_raises(self, kek: InMemoryKekProvider) -> None:
        from pleno_pii_scanner.findings_store.encryption import (
            EncryptionError,
            encrypt_payload,
        )

        store = MemoryFindingsStore(kek=kek)
        try:
            dek = await store._ensure_dek()
            # Hand-craft a payload whose plaintext lacks "matched".
            payload = encrypt_payload(dek, store._tenant_id, {"snippet": "..."})
            store._payloads["forged"] = payload
            from datetime import UTC, datetime

            from pleno_pii_scanner.findings_store.base import FindingRecord

            store._records["forged"] = FindingRecord(
                finding_id="forged",
                fingerprint="x",
                scan_id="s",
                source_id="x",
                source_kind="x",
                entity="X",
                file_path="x",
                line=0,
                col=0,
                score=0.0,
                verification="unverified",
                severity="medium",
                status="open",
                value_excerpt="",
                shard_index=0,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            with pytest.raises(EncryptionError, match="missing 'matched'"):
                await store.reveal_value("forged", audit_principal="ops")
        finally:
            await store.close()


class TestParallelSources:
    @pytest.mark.asyncio
    async def test_concurrent_save_no_id_collision(
        self, kek: InMemoryKekProvider
    ) -> None:
        async with MemoryFindingsStore(kek=kek) as store:

            async def save(src: str) -> None:
                await store.save_findings(
                    "scan-1",
                    src,
                    [_finding(matched=f"v-{src}-{i}") for i in range(5)],
                )

            await asyncio.gather(*(save(f"src-{i:02d}") for i in range(8)))
            recs = await store.query(scan_id="scan-1", limit=1000)
            ids = [r.finding_id for r in recs]
            assert len(ids) == 8 * 5
            assert len(set(ids)) == len(ids)
