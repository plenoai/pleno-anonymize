"""Encryption-layer tests: KEK round-trip, payload AEAD, secret hygiene."""

from __future__ import annotations

import os

import pytest

from pleno_pii_scanner.findings_store.encryption import (
    DEK_SIZE,
    NONCE_SIZE,
    TAG_SIZE,
    EncryptedPayload,
    EncryptionError,
    InMemoryKekProvider,
    decrypt_payload,
    encrypt_payload,
    generate_dek,
)


class TestInMemoryKekProvider:
    def test_warning_emitted_on_construction(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        InMemoryKekProvider()
        err = capsys.readouterr().err
        assert "PRODUCTION WARNING" in err
        assert "InMemoryKekProvider" in err

    def test_master_key_size_validated(self) -> None:
        with pytest.raises(ValueError, match="master_key must be"):
            InMemoryKekProvider(master_key=b"too-short")

    def test_default_master_key_is_random(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kek_a = InMemoryKekProvider()
        kek_b = InMemoryKekProvider()
        capsys.readouterr()
        assert kek_a._aead is not kek_b._aead

    @pytest.mark.asyncio
    async def test_wrap_unwrap_round_trip(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kek = InMemoryKekProvider()
        capsys.readouterr()
        dek = generate_dek()
        wrapped = await kek.wrap_dek("tenant-a", dek)
        unwrapped = await kek.unwrap_dek("tenant-a", wrapped)
        assert unwrapped == dek

    @pytest.mark.asyncio
    async def test_wrong_tenant_unwrap_fails(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kek = InMemoryKekProvider()
        capsys.readouterr()
        dek = generate_dek()
        wrapped = await kek.wrap_dek("tenant-a", dek)
        with pytest.raises(EncryptionError, match="KEK or tenant_id mismatch"):
            await kek.unwrap_dek("tenant-b", wrapped)

    @pytest.mark.asyncio
    async def test_dek_size_enforced_on_wrap(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kek = InMemoryKekProvider()
        capsys.readouterr()
        with pytest.raises(ValueError, match="DEK must be"):
            await kek.wrap_dek("tenant-a", b"short")

    @pytest.mark.asyncio
    async def test_short_wrapped_blob_rejected(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kek = InMemoryKekProvider()
        capsys.readouterr()
        with pytest.raises(EncryptionError, match="too short"):
            await kek.unwrap_dek("tenant-a", b"\x00" * NONCE_SIZE)

    @pytest.mark.asyncio
    async def test_different_master_keys_isolated(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        kek_a = InMemoryKekProvider(master_key=b"\x01" * DEK_SIZE)
        kek_b = InMemoryKekProvider(master_key=b"\x02" * DEK_SIZE)
        capsys.readouterr()
        dek = generate_dek()
        wrapped = await kek_a.wrap_dek("tenant-x", dek)
        with pytest.raises(EncryptionError):
            await kek_b.unwrap_dek("tenant-x", wrapped)


class TestPayloadAEAD:
    def test_round_trip(self) -> None:
        dek = generate_dek()
        plaintext = {"matched": "API_KEY=4242", "snippet": "...4242..."}
        payload = encrypt_payload(dek, "tenant-a", plaintext)
        out = decrypt_payload(dek, payload)
        assert out == plaintext

    def test_nonce_size_correct(self) -> None:
        dek = generate_dek()
        payload = encrypt_payload(dek, "tenant-a", {"matched": "x"})
        assert len(payload.nonce) == NONCE_SIZE
        assert len(payload.tag) == TAG_SIZE

    def test_unique_nonce_per_encryption(self) -> None:
        dek = generate_dek()
        a = encrypt_payload(dek, "tenant-a", {"matched": "same"})
        b = encrypt_payload(dek, "tenant-a", {"matched": "same"})
        assert a.nonce != b.nonce
        assert a.ciphertext != b.ciphertext

    def test_wrong_dek_fails(self) -> None:
        dek_a = generate_dek()
        dek_b = generate_dek()
        payload = encrypt_payload(dek_a, "tenant-a", {"matched": "secret"})
        with pytest.raises(EncryptionError, match="failed to decrypt"):
            decrypt_payload(dek_b, payload)

    def test_wrong_tenant_aad_fails(self) -> None:
        dek = generate_dek()
        payload = encrypt_payload(dek, "tenant-a", {"matched": "secret"})
        tampered = EncryptedPayload(
            tenant_id="tenant-b",
            nonce=payload.nonce,
            ciphertext=payload.ciphertext,
            tag=payload.tag,
        )
        with pytest.raises(EncryptionError):
            decrypt_payload(dek, tampered)

    def test_tampered_ciphertext_rejected(self) -> None:
        dek = generate_dek()
        payload = encrypt_payload(dek, "tenant-a", {"matched": "x"})
        flipped = bytes([payload.ciphertext[0] ^ 0x01]) + payload.ciphertext[1:]
        bad = EncryptedPayload(
            tenant_id=payload.tenant_id,
            nonce=payload.nonce,
            ciphertext=flipped,
            tag=payload.tag,
        )
        with pytest.raises(EncryptionError):
            decrypt_payload(dek, bad)

    def test_encrypt_dek_size_validated(self) -> None:
        with pytest.raises(ValueError, match="DEK must be"):
            encrypt_payload(b"short", "tenant-a", {"matched": "x"})

    def test_decrypt_dek_size_validated(self) -> None:
        dek = generate_dek()
        payload = encrypt_payload(dek, "tenant-a", {"matched": "x"})
        with pytest.raises(ValueError, match="DEK must be"):
            decrypt_payload(b"short", payload)

    def test_decrypted_non_object_rejected(self) -> None:
        # WHY: decrypt_payload contractually returns dict; if some prior
        # encryption put a list or string in, we surface a clean error.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import json as _json

        dek = generate_dek()
        aead = AESGCM(dek)
        nonce = os.urandom(NONCE_SIZE)
        combined = aead.encrypt(nonce, _json.dumps([1, 2, 3]).encode(), b"tenant-a")
        bad = EncryptedPayload(
            tenant_id="tenant-a",
            nonce=nonce,
            ciphertext=combined[:-TAG_SIZE],
            tag=combined[-TAG_SIZE:],
        )
        with pytest.raises(EncryptionError, match="not a JSON object"):
            decrypt_payload(dek, bad)


class TestEncryptedPayloadHygiene:
    def test_repr_does_not_leak_ciphertext(self) -> None:
        dek = generate_dek()
        payload = encrypt_payload(dek, "tenant-a", {"matched": "DO-NOT-LOG-RAW"})
        text = repr(payload)
        assert "redacted" in text
        # The base64 of the ciphertext should not appear; in particular
        # the ciphertext bytes themselves must not show up in repr.
        assert payload.ciphertext.hex() not in text

    def test_str_uses_repr(self) -> None:
        dek = generate_dek()
        payload = encrypt_payload(dek, "tenant-a", {"matched": "x"})
        # WHY: __str__ falls back to __repr__ on dataclasses with no
        # explicit __str__, so a print() also stays redacted.
        assert "redacted" in str(payload)

    def test_jsonl_round_trip(self) -> None:
        dek = generate_dek()
        payload = encrypt_payload(dek, "tenant-a", {"matched": "y"})
        wire = payload.to_jsonl()
        back = EncryptedPayload.from_jsonl(wire)
        assert back == payload

    def test_jsonl_malformed_raises(self) -> None:
        with pytest.raises(EncryptionError, match="malformed"):
            EncryptedPayload.from_jsonl({"tenant_id": "x"})

    def test_jsonl_bad_b64_raises(self) -> None:
        with pytest.raises(EncryptionError, match="malformed"):
            EncryptedPayload.from_jsonl(
                {
                    "tenant_id": "x",
                    "nonce_b64": "@@@notb64@@@",
                    "ciphertext_b64": "",
                    "tag_b64": "",
                }
            )


class TestGenerateDek:
    def test_returns_correct_size(self) -> None:
        assert len(generate_dek()) == DEK_SIZE

    def test_two_calls_differ(self) -> None:
        assert generate_dek() != generate_dek()
