"""Protocol-level tests: severity rules, fingerprint, masking, dataclass hygiene."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pleno_pii_scanner.findings_store.base import (
    CRITICAL_ENTITIES,
    EncryptedFinding,
    FindingRecord,
    FindingsStore,
    ShardRef,
    default_severity,
    derive_finding_id,
    fingerprint_value,
    mask_excerpt,
)
from pleno_pii_scanner.findings_store import (
    MemoryFindingsStore,
    SqliteFindingsStore,
)
from pleno_pii_scanner.models import Finding


def _finding(**overrides: object) -> Finding:
    base: dict[str, object] = dict(
        entity="EMAIL_ADDRESS",
        file="src/app.py",
        line=42,
        col=8,
        score=0.95,
        snippet="contact alice@example.com please",
        matched="alice@example.com",
        pattern_name="email_address",
        verification="unverified",
    )
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


class TestDefaultSeverity:
    def test_passed_critical_entity(self) -> None:
        f = _finding(entity="AWS_SECRET_ACCESS_KEY", verification="passed")
        assert default_severity(f) == "critical"

    def test_passed_high_entity(self) -> None:
        f = _finding(entity="EMAIL_ADDRESS", verification="passed")
        assert default_severity(f) == "high"

    def test_passed_unknown_entity_defaults_to_high(self) -> None:
        f = _finding(entity="WEIRD_ENTITY", verification="passed")
        assert default_severity(f) == "high"

    def test_unverified_is_medium(self) -> None:
        f = _finding(verification="unverified")
        assert default_severity(f) == "medium"

    def test_failed_is_low(self) -> None:
        f = _finding(verification="failed")
        assert default_severity(f) == "low"

    def test_every_critical_entity_resolves_critical(self) -> None:
        for ent in CRITICAL_ENTITIES:
            f = _finding(entity=ent, verification="passed")
            assert default_severity(f) == "critical"


class TestFingerprintAndId:
    def test_fingerprint_is_stable(self) -> None:
        a = fingerprint_value("alice@example.com")
        b = fingerprint_value("alice@example.com")
        assert a == b
        assert len(a) == 16

    def test_fingerprint_distinguishes_values(self) -> None:
        assert fingerprint_value("a") != fingerprint_value("b")

    def test_finding_id_includes_scan_and_source(self) -> None:
        fp = fingerprint_value("x")
        a = derive_finding_id("scan-1", "src-a", fp)
        b = derive_finding_id("scan-1", "src-b", fp)
        c = derive_finding_id("scan-2", "src-a", fp)
        assert a != b != c
        assert len(a) == 32


class TestMaskExcerpt:
    def test_empty(self) -> None:
        assert mask_excerpt("") == ""

    def test_short_fully_masked(self) -> None:
        assert mask_excerpt("abc") == "***"

    def test_exact_4_chars_fully_masked(self) -> None:
        assert mask_excerpt("abcd") == "****"

    def test_long_value_keeps_2_each_side(self) -> None:
        assert mask_excerpt("4242424242424242") == "42************42"

    def test_does_not_round_trip_to_raw(self) -> None:
        raw = "alice@example.com"
        assert mask_excerpt(raw) != raw


class TestFindingRecordHygiene:
    def _record(self) -> FindingRecord:
        now = datetime(2026, 5, 4, tzinfo=UTC)
        return FindingRecord(
            finding_id="f-1",
            fingerprint="abcd1234",
            scan_id="scan-1",
            source_id="src-a",
            source_kind="dir",
            entity="EMAIL_ADDRESS",
            file_path="src/app.py",
            line=10,
            col=4,
            score=0.9,
            verification="unverified",
            severity="medium",
            status="open",
            value_excerpt="al*************om",
            shard_index=0,
            created_at=now,
            updated_at=now,
        )

    def test_repr_does_not_contain_raw_value(self) -> None:
        rec = self._record()
        text = repr(rec)
        assert "alice@example.com" not in text
        # WHY: defense in depth — reject any leak of unmasked plaintext
        # patterns even if a future field rename would normally surface them.
        assert "al*************om" in text

    def test_str_does_not_contain_raw_value(self) -> None:
        rec = self._record()
        assert "alice@example.com" not in str(rec)

    def test_format_does_not_contain_raw_value(self) -> None:
        rec = self._record()
        assert "alice@example.com" not in f"{rec}"


class TestEncryptedFindingHygiene:
    def test_repr_redacts(self) -> None:
        ef = EncryptedFinding(
            finding_id="f-1",
            fingerprint="abcd1234",
            tenant_id="tenant-a",
            nonce=b"\x01" * 12,
            ciphertext=b"DO-NOT-LOG",
            tag=b"\x02" * 16,
        )
        text = repr(ef)
        assert "encrypted" in text
        assert "DO-NOT-LOG" not in text


class TestProtocolConformance:
    """The two real implementations must satisfy the FindingsStore Protocol."""

    @pytest.mark.asyncio
    async def test_memory_store_is_findings_store(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pleno_pii_scanner.findings_store import InMemoryKekProvider

        kek = InMemoryKekProvider()
        capsys.readouterr()
        store = MemoryFindingsStore(kek=kek)
        try:
            assert isinstance(store, FindingsStore)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sqlite_store_is_findings_store(
        self,
        tmp_path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from pleno_pii_scanner.findings_store import InMemoryKekProvider

        kek = InMemoryKekProvider()
        capsys.readouterr()
        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        try:
            assert isinstance(store, FindingsStore)
        finally:
            await store.close()


class TestShardRef:
    def test_frozen(self) -> None:
        from pathlib import Path

        ref = ShardRef(
            scan_id="s",
            source_id="src",
            shard_index=0,
            path=Path("/tmp/x"),
            finding_count=3,
            created_at=datetime(2026, 5, 4, tzinfo=UTC),
        )
        with pytest.raises(Exception):
            ref.shard_index = 1  # type: ignore[misc]
