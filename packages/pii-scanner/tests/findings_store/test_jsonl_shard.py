"""JSONL shard writer/reader durability + crash-safety tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pleno_pii_scanner.findings_store.encryption import (
    EncryptionError,
    encrypt_payload,
    generate_dek,
)
from pleno_pii_scanner.findings_store.jsonl_shard import (
    JsonlShardWriter,
    default_shard_base,
    read_shard,
    shard_path,
)


class TestShardPath:
    def test_layout(self, tmp_path: Path) -> None:
        p = shard_path(tmp_path, "scan-1", "src-a", 3)
        assert p == tmp_path / "scan-1" / "findings" / "src-a" / "3.jsonl"

    def test_default_shard_base_uses_xdg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert default_shard_base() == tmp_path / "pleno"

    def test_default_shard_base_falls_back_to_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_shard_base() == tmp_path / ".local" / "state" / "pleno"

    def test_default_shard_base_empty_xdg_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", "")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert default_shard_base() == tmp_path / ".local" / "state" / "pleno"


class TestWriterReader:
    @pytest.mark.asyncio
    async def test_round_trip_single_batch(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        writer = JsonlShardWriter(path)
        dek = generate_dek()
        payloads = [
            encrypt_payload(dek, "tenant-a", {"matched": "v1"}),
            encrypt_payload(dek, "tenant-a", {"matched": "v2"}),
        ]
        n = await writer.write_batch(["f1", "f2"], ["fp1", "fp2"], payloads)
        await writer.close()
        assert n == 2

        loaded = read_shard(path)
        assert [fid for fid, _, _ in loaded] == ["f1", "f2"]
        assert [fp for _, fp, _ in loaded] == ["fp1", "fp2"]

    @pytest.mark.asyncio
    async def test_empty_batch_no_op(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        writer = JsonlShardWriter(path)
        n = await writer.write_batch([], [], [])
        await writer.close()
        assert n == 0
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_length_mismatch_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        writer = JsonlShardWriter(path)
        with pytest.raises(ValueError, match="equal-length"):
            await writer.write_batch(["a"], [], [])

    @pytest.mark.asyncio
    async def test_close_blocks_further_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        writer = JsonlShardWriter(path)
        dek = generate_dek()
        await writer.close()
        with pytest.raises(RuntimeError, match="closed"):
            await writer.write_batch(
                ["a"], ["b"],
                [encrypt_payload(dek, "t", {"matched": "x"})],
            )

    @pytest.mark.asyncio
    async def test_path_property(self, tmp_path: Path) -> None:
        path = tmp_path / "x.jsonl"
        writer = JsonlShardWriter(path)
        try:
            assert writer.path == path
        finally:
            await writer.close()

    @pytest.mark.asyncio
    async def test_appends_across_calls(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        writer = JsonlShardWriter(path)
        dek = generate_dek()
        try:
            await writer.write_batch(
                ["a"], ["fp-a"],
                [encrypt_payload(dek, "t", {"matched": "1"})],
            )
            await writer.write_batch(
                ["b"], ["fp-b"],
                [encrypt_payload(dek, "t", {"matched": "2"})],
            )
        finally:
            await writer.close()
        assert len(read_shard(path)) == 2

    @pytest.mark.asyncio
    async def test_concurrent_writes_serialized(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        writer = JsonlShardWriter(path)
        dek = generate_dek()

        async def write_one(i: int) -> None:
            await writer.write_batch(
                [f"f{i}"],
                [f"fp{i}"],
                [encrypt_payload(dek, "t", {"matched": f"v{i}"})],
            )

        try:
            await asyncio.gather(*(write_one(i) for i in range(20)))
        finally:
            await writer.close()
        loaded = read_shard(path)
        assert len(loaded) == 20
        # WHY: lock-serialized writes mean every line is well-formed
        # JSON; absence of duplicates / missing IDs proves no interleaving.
        ids = sorted(fid for fid, _, _ in loaded)
        assert ids == sorted(f"f{i}" for i in range(20))

    def test_read_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert read_shard(tmp_path / "absent.jsonl") == []

    def test_read_truncated_trailing_line_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        # Write one well-formed line and a torn second line (no newline).
        path.write_bytes(
            b'{"finding_id":"a","fingerprint":"fa","tenant_id":"t",'
            b'"nonce_b64":"AAAAAAAAAAAAAAAA","ciphertext_b64":"AA==",'
            b'"tag_b64":"AAAAAAAAAAAAAAAAAAAAAA=="}\n'
            b'{"finding_id":"b"  '
        )
        loaded = read_shard(path)
        assert len(loaded) == 1
        assert loaded[0][0] == "a"

    def test_corrupt_json_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        path.write_bytes(b"this is not json\n")
        with pytest.raises(EncryptionError, match="corrupt shard line"):
            read_shard(path)

    def test_non_object_line_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "0.jsonl"
        path.write_bytes(b"[1,2,3]\n")
        with pytest.raises(EncryptionError, match="non-object"):
            read_shard(path)

    @pytest.mark.asyncio
    async def test_file_permissions_600(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX-only permission semantics")
        path = tmp_path / "sub" / "0.jsonl"
        writer = JsonlShardWriter(path)
        dek = generate_dek()
        try:
            await writer.write_batch(
                ["a"], ["fa"], [encrypt_payload(dek, "t", {"matched": "x"})]
            )
        finally:
            await writer.close()
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600
        # WHY: the parent directory is also created with 0o700 so the
        # shard files cannot be enumerated by another local user.
        assert (path.parent.stat().st_mode & 0o777) == 0o700
