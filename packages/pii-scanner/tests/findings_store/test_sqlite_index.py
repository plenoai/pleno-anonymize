"""SqliteFindingsStore: durability, dedup, parallel shards, reveal, hygiene."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import aiosqlite
import pytest

from pleno_pii_scanner.findings_store import (
    InMemoryKekProvider,
    SqliteFindingsStore,
    default_index_path,
)
from pleno_pii_scanner.findings_store.encryption import EncryptionError
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


class TestDefaultIndexPath:
    def test_uses_xdg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert default_index_path("scan-x") == (
            tmp_path / "pleno" / "scan-x" / "findings.sqlite"
        )

    def test_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_index_path("scan-x") == (
            tmp_path / ".local" / "state" / "pleno" / "scan-x" / "findings.sqlite"
        )

    def test_empty_xdg_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", "")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_index_path("scan-x") == (
            tmp_path / ".local" / "state" / "pleno" / "scan-x" / "findings.sqlite"
        )


class TestOpenAndClose:
    @pytest.mark.asyncio
    async def test_uses_default_paths(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        kek: InMemoryKekProvider,
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        store = await SqliteFindingsStore.open("scan-d", kek=kek)
        try:
            assert store.index_path == (
                tmp_path / "pleno" / "scan-d" / "findings.sqlite"
            )
            assert store.shard_base == tmp_path / "pleno"
            assert store.index_path.exists()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_open_failure_closes_conn(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        kek: InMemoryKekProvider,
    ) -> None:
        original_connect = aiosqlite.connect

        class FailingConn:
            def __init__(self, real: aiosqlite.Connection) -> None:
                self._real = real
                self._calls = 0

            async def execute(self, *args: object, **kw: object) -> object:
                self._calls += 1
                if self._calls > 1:
                    raise RuntimeError("simulated migration failure")
                return await self._real.execute(*args, **kw)

            async def commit(self) -> None:
                await self._real.commit()

            async def close(self) -> None:
                await self._real.close()

        async def patched_connect(*args: object, **kw: object) -> object:
            real = await original_connect(*args, **kw)
            return FailingConn(real)

        monkeypatch.setattr(aiosqlite, "connect", patched_connect)
        with pytest.raises(RuntimeError, match="simulated migration failure"):
            await SqliteFindingsStore.open(
                "scan-x",
                kek=kek,
                index_path=tmp_path / "f.sqlite",
                shard_base=tmp_path / "shards",
            )

    @pytest.mark.asyncio
    async def test_close_idempotent(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        await store.close()
        await store.close()

    @pytest.mark.asyncio
    async def test_async_context_manager(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings("scan-1", "src-a", [_finding()])
        # Reopen and confirm survival
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store2:
            assert len(await store2.query()) == 1


class TestSaveQueryGet:
    @pytest.mark.asyncio
    async def test_save_and_query(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            source_kind="dir",
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            ref = await store.save_findings(
                "scan-1",
                "src-a",
                [
                    _finding(matched="alice@x", entity="EMAIL_ADDRESS"),
                    _finding(
                        matched="AKIA0000000000000000",
                        entity="AWS_ACCESS_KEY_ID",
                        verification="passed",
                    ),
                ],
            )
            assert ref.finding_count == 2
            assert ref.shard_index == 0
            assert ref.path.exists()

            all_recs = await store.query(scan_id="scan-1")
            assert len(all_recs) == 2
            crit = await store.query(severity="critical")
            assert len(crit) == 1
            kind = await store.query(source_kind="dir")
            assert len(kind) == 2
            assert await store.query(source_kind="github") == []
            ver = await store.query(verification="passed")
            assert len(ver) == 1
            stat = await store.query(status="open")
            assert len(stat) == 2
            ent = await store.query(entity="EMAIL_ADDRESS")
            assert len(ent) == 1

            got = await store.get(all_recs[0].finding_id)
            assert got is not None
            assert got.value_excerpt != "alice@x"

            assert await store.get("absent") is None

    @pytest.mark.asyncio
    async def test_pagination(self, tmp_path: Path, kek: InMemoryKekProvider) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings(
                "scan-1",
                "src-a",
                [_finding(matched=f"v{i}") for i in range(7)],
            )
            page1 = await store.query(limit=3, offset=0)
            page2 = await store.query(limit=3, offset=3)
            page3 = await store.query(limit=3, offset=6)
            assert len(page1) == 3
            assert len(page2) == 3
            assert len(page3) == 1

    @pytest.mark.asyncio
    async def test_dedup_within_scan(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="dup", line=1)]
            )
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="dup", line=99)]
            )
            recs = await store.query()
            assert len(recs) == 1
            assert recs[0].line == 99


class TestKillMinus9Durability:
    @pytest.mark.asyncio
    async def test_resume_after_close(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        idx = tmp_path / "f.sqlite"
        shards = tmp_path / "shards"
        # Use a deterministic master key so a second open can reuse
        # the previously wrapped DEK from disk.
        from pleno_pii_scanner.findings_store.encryption import DEK_SIZE

        master = b"\x33" * DEK_SIZE
        capsys_kek_a = InMemoryKekProvider(master_key=master)
        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=capsys_kek_a,
            index_path=idx,
            shard_base=shards,
        )
        ref = await store.save_findings(
            "scan-1", "src-a", [_finding(matched="durable")]
        )
        await store.close()

        capsys_kek_b = InMemoryKekProvider(master_key=master)
        reopened = await SqliteFindingsStore.open(
            "scan-1",
            kek=capsys_kek_b,
            index_path=idx,
            shard_base=shards,
        )
        try:
            recs = await reopened.query(scan_id="scan-1")
            assert len(recs) == 1
            value = await reopened.reveal_value(
                recs[0].finding_id, audit_principal="ops"
            )
            assert value == "durable"
            assert ref.path.exists()
        finally:
            await reopened.close()


class TestRevealValue:
    @pytest.mark.asyncio
    async def test_reveal_with_audit(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        spy: list[tuple[str, str]] = []

        def hook(fid: str, who: str) -> None:
            spy.append((fid, who))

        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
            audit_hook=hook,
        ) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="alice@example.com")]
            )
            recs = await store.query()
            value = await store.reveal_value(
                recs[0].finding_id, audit_principal="ops@team"
            )
            assert value == "alice@example.com"
            assert spy == [(recs[0].finding_id, "ops@team")]

    @pytest.mark.asyncio
    async def test_reveal_async_audit_hook(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        spy: list[str] = []

        async def hook(fid: str, who: str) -> None:
            await asyncio.sleep(0)
            spy.append(who)

        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
            audit_hook=hook,
        ) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="async-secret")]
            )
            recs = await store.query()
            value = await store.reveal_value(recs[0].finding_id, audit_principal="ops")
            assert value == "async-secret"
            assert spy == ["ops"]

    @pytest.mark.asyncio
    async def test_reveal_missing_audits_then_keyerror(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        spy: list[str] = []

        def hook(fid: str, who: str) -> None:
            spy.append(fid)

        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
            audit_hook=hook,
        ) as store:
            with pytest.raises(KeyError):
                await store.reveal_value("absent", audit_principal="ops")
            assert spy == ["absent"]

    @pytest.mark.asyncio
    async def test_reveal_no_audit_hook_works(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings("scan-1", "src-a", [_finding(matched="silent")])
            recs = await store.query()
            assert (
                await store.reveal_value(recs[0].finding_id, audit_principal="x")
                == "silent"
            )

    @pytest.mark.asyncio
    async def test_reveal_when_shard_truncated(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            ref = await store.save_findings(
                "scan-1", "src-a", [_finding(matched="lost")]
            )
            recs = await store.query()
            # Wipe the shard file to simulate disk-side loss.
            ref.path.unlink()
            with pytest.raises(EncryptionError, match="indexed but absent"):
                await store.reveal_value(recs[0].finding_id, audit_principal="ops")

    @pytest.mark.asyncio
    async def test_reveal_with_wrong_kek_fails(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from pleno_pii_scanner.findings_store.encryption import DEK_SIZE

        kek_a = InMemoryKekProvider(master_key=b"\x11" * DEK_SIZE)
        capsys.readouterr()
        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek_a,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        await store.save_findings("scan-1", "src-a", [_finding(matched="protected")])
        await store.close()

        kek_b = InMemoryKekProvider(master_key=b"\x22" * DEK_SIZE)
        capsys.readouterr()
        reopened = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek_b,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        try:
            recs = await reopened.query()
            with pytest.raises(EncryptionError):
                await reopened.reveal_value(recs[0].finding_id, audit_principal="ops")
        finally:
            await reopened.close()


class TestSecretHygiene:
    @pytest.mark.asyncio
    async def test_repr_no_raw(self, tmp_path: Path, kek: InMemoryKekProvider) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="leaky-987")]
            )
            recs = await store.query()
            assert "leaky-987" not in repr(recs[0])

    @pytest.mark.asyncio
    async def test_log_no_raw(
        self,
        tmp_path: Path,
        kek: InMemoryKekProvider,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="logleak-555")]
            )
            recs = await store.query()
            with caplog.at_level(logging.INFO):
                logging.info("rec=%s, store=%r", recs[0], store)
        assert "logleak-555" not in caplog.text

    @pytest.mark.asyncio
    async def test_index_db_no_raw(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        idx = tmp_path / "f.sqlite"
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=idx,
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings(
                "scan-1", "src-a", [_finding(matched="raw-must-not-leak")]
            )
        # The raw value must not appear anywhere in the index file.
        blob = idx.read_bytes()
        assert b"raw-must-not-leak" not in blob


class TestParallelSources:
    @pytest.mark.asyncio
    async def test_concurrent_save_independent_shards(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:

            async def save(src: str) -> None:
                await store.save_findings(
                    "scan-1",
                    src,
                    [_finding(matched=f"v-{src}-{i}") for i in range(4)],
                )

            await asyncio.gather(*(save(f"src-{i:02d}") for i in range(6)))
            all_recs = await store.query(scan_id="scan-1", limit=1000)
            assert len(all_recs) == 6 * 4
            ids = [r.finding_id for r in all_recs]
            assert len(set(ids)) == len(ids)


class TestUseAfterClose:
    @pytest.mark.asyncio
    async def test_methods_raise_after_close(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        await store.close()
        with pytest.raises(RuntimeError, match="closed"):
            await store.save_findings("scan-1", "src-a", [_finding()])
        with pytest.raises(RuntimeError, match="closed"):
            await store.query()
        with pytest.raises(RuntimeError, match="closed"):
            await store.get("x")
        with pytest.raises(RuntimeError, match="closed"):
            await store.reveal_value("x", audit_principal="ops")


class TestIndexPermissions:
    @pytest.mark.asyncio
    async def test_parent_directory_700(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        if os.name == "nt":
            pytest.skip("POSIX-only permission semantics")
        target = tmp_path / "scoped" / "f.sqlite"
        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=target,
            shard_base=tmp_path / "shards",
        )
        try:
            assert (target.parent.stat().st_mode & 0o777) == 0o700
        finally:
            await store.close()


class TestEmptySaves:
    @pytest.mark.asyncio
    async def test_empty_findings_records_zero_count(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            ref = await store.save_findings("scan-1", "src-a", [])
            assert ref.finding_count == 0
            assert ref.shard_index == 0
            recs = await store.query()
            assert recs == []


class TestCorruptionPaths:
    @pytest.mark.asyncio
    async def test_shard_tenant_mismatch_raises(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        from pleno_pii_scanner.findings_store.encryption import (
            encrypt_payload,
        )
        from pleno_pii_scanner.findings_store.jsonl_shard import (
            JsonlShardWriter,
            shard_path,
        )

        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        try:
            await store.save_findings("scan-1", "src-a", [_finding(matched="forged")])
            recs = await store.query()
            dek = await store._ensure_dek()
            # Overwrite the shard with a payload whose tenant_id is wrong.
            path = shard_path(
                store.shard_base,
                "scan-1",
                "src-a",
                recs[0].shard_index,
            )
            path.unlink()
            writer = JsonlShardWriter(path)
            forged = encrypt_payload(dek, "wrong-tenant", {"matched": "x"})
            await writer.write_batch(
                [recs[0].finding_id],
                [recs[0].fingerprint],
                [forged],
            )
            await writer.close()
            with pytest.raises(EncryptionError, match="tenant_id does not match"):
                await store.reveal_value(recs[0].finding_id, audit_principal="ops")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_decrypted_missing_matched_raises(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        from pleno_pii_scanner.findings_store.encryption import (
            encrypt_payload,
        )
        from pleno_pii_scanner.findings_store.jsonl_shard import (
            JsonlShardWriter,
            shard_path,
        )

        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        try:
            await store.save_findings("scan-1", "src-a", [_finding(matched="trace")])
            recs = await store.query()
            dek = await store._ensure_dek()
            path = shard_path(store.shard_base, "scan-1", "src-a", recs[0].shard_index)
            path.unlink()
            writer = JsonlShardWriter(path)
            no_matched = encrypt_payload(
                dek, store._tenant_id, {"snippet": "no matched key"}
            )
            await writer.write_batch(
                [recs[0].finding_id],
                [recs[0].fingerprint],
                [no_matched],
            )
            await writer.close()
            with pytest.raises(EncryptionError, match="missing 'matched'"):
                await store.reveal_value(recs[0].finding_id, audit_principal="ops")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_unwrap_for_unknown_tenant_raises(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        try:
            with pytest.raises(EncryptionError, match="no wrapped DEK"):
                await store._unwrap_for_tenant("never-saved")
        finally:
            await store.close()


class TestNaiveDatetimeBackfill:
    @pytest.mark.asyncio
    async def test_naive_iso_promoted_to_utc(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        from datetime import UTC, datetime

        store = await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        )
        try:
            await store.save_findings("scan-1", "src-a", [_finding(matched="x")])
            recs = await store.query()
            # Forcibly write a naive datetime to simulate a legacy row.
            await store._conn.execute(
                "UPDATE findings SET created_at=?, updated_at=?, "
                "sla_due_at=? WHERE finding_id=?",
                (
                    "2026-05-04T12:00:00",
                    "2026-05-04T12:00:00",
                    "2026-05-04T12:00:00",
                    recs[0].finding_id,
                ),
            )
            await store._conn.commit()
            got = await store.get(recs[0].finding_id)
            assert got is not None
            assert got.created_at == datetime(2026, 5, 4, 12, tzinfo=UTC)
            assert got.sla_due_at == datetime(2026, 5, 4, 12, tzinfo=UTC)
        finally:
            await store.close()


class TestPatternNameAndStringFields:
    @pytest.mark.asyncio
    async def test_pattern_name_round_trips(
        self, tmp_path: Path, kek: InMemoryKekProvider
    ) -> None:
        async with await SqliteFindingsStore.open(
            "scan-1",
            kek=kek,
            index_path=tmp_path / "f.sqlite",
            shard_base=tmp_path / "shards",
        ) as store:
            await store.save_findings(
                "scan-1",
                "src-a",
                [_finding(pattern_name="custom-rule-1")],
            )
            recs = await store.query()
            assert recs[0].pattern_name == "custom-rule-1"
