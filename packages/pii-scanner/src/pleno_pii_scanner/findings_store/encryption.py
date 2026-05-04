"""Envelope encryption for FindingsStore raw payloads (ADR-0007 §11).

Two key tiers:

  * KEK (Key Encryption Key) — held by an external system (KMS, Vault,
    HSM). Wraps per-tenant DEKs at rest. Implementations satisfy
    `KekProvider`. The default `InMemoryKekProvider` is for tests / dev
    only and emits a stderr warning on construction so it cannot silently
    end up in production.
  * DEK (Data Encryption Key) — per-tenant 32-byte AES-256-GCM key. The
    wrapped form lives in the index next to the tenant row; the unwrapped
    form lives in process memory only and is never written to disk or
    log. Plaintext payloads (raw matched value + snippet + extra) are
    encrypted with the unwrapped DEK using AES-256-GCM with a fresh
    12-byte random nonce per encryption.

Wire format on shard disk (one JSON object per JSONL line):

    {"finding_id": "...", "fingerprint": "...", "tenant_id": "...",
     "nonce_b64": "...", "ciphertext_b64": "...", "tag_b64": "..."}

`ciphertext_b64` is the AES-GCM ciphertext minus the trailing tag;
`tag_b64` is the 16-byte authentication tag, stored separately so a
future migration to a different AEAD that puts the tag elsewhere
(XChaCha20-Poly1305, AES-GCM-SIV) can keep the same wire shape.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# WHY: AES-256-GCM key size is fixed at 32 bytes; nonce length is 12
# bytes per NIST SP 800-38D recommendation for random nonces; tag length
# is 16 bytes (the AESGCM API always emits 16-byte tags appended to the
# ciphertext, which we split off so the wire format keeps tag isolated).
DEK_SIZE = 32
NONCE_SIZE = 12
TAG_SIZE = 16


class EncryptionError(RuntimeError):
    """Raised when wrap/unwrap/encrypt/decrypt fails on the FindingsStore boundary."""


@runtime_checkable
class KekProvider(Protocol):
    """Pluggable Key Encryption Key provider — KMS / Vault / HSM / etc.

    Implementations MUST refuse to unwrap a DEK that was wrapped under a
    different tenant_id, even if the caller hands them the bytes; this is
    the only defense against a confused-deputy bug in the FindingsStore
    that would let a query for tenant A decrypt tenant B's payload.
    """

    name: str

    async def wrap_dek(self, tenant_id: str, dek: bytes) -> bytes:
        """Encrypt the raw DEK under the KEK for `tenant_id`."""
        ...

    async def unwrap_dek(self, tenant_id: str, wrapped: bytes) -> bytes:
        """Decrypt a previously wrapped DEK; raise if `tenant_id` mismatches."""
        ...


class InMemoryKekProvider:
    """Process-local KEK for tests and dev. NEVER for production.

    A single random 32-byte master key encrypts every tenant's DEK with
    AES-256-GCM, using the tenant_id as additional authenticated data so
    a wrapped DEK from one tenant cannot be unwrapped under another.
    Constructing this class prints a warning to stderr; production
    deployments should plug in a `KekProvider` backed by KMS / Vault.
    """

    name = "in-memory"

    def __init__(self, master_key: bytes | None = None) -> None:
        if master_key is None:
            master_key = os.urandom(DEK_SIZE)
        if len(master_key) != DEK_SIZE:
            raise ValueError(
                f"master_key must be {DEK_SIZE} bytes; got {len(master_key)}"
            )
        # WHY: the warning has to be loud enough that a CI grep for it
        # would catch a production deployment that forgot to swap in a
        # real KEK. We unconditionally write to stderr (not logging) so
        # the message survives any downstream log filter.
        print(
            "PRODUCTION WARNING: InMemoryKekProvider in use. "
            "Tenant DEKs are protected by a process-local random master "
            "key only and will be lost on restart. Plug in a KMS/Vault "
            "KekProvider before any deployment that handles real PII.",
            file=sys.stderr,
        )
        self._aead = AESGCM(master_key)

    async def wrap_dek(self, tenant_id: str, dek: bytes) -> bytes:
        if len(dek) != DEK_SIZE:
            raise ValueError(
                f"DEK must be {DEK_SIZE} bytes; got {len(dek)}"
            )
        nonce = os.urandom(NONCE_SIZE)
        aad = tenant_id.encode("utf-8")
        ct = self._aead.encrypt(nonce, dek, aad)
        return nonce + ct

    async def unwrap_dek(self, tenant_id: str, wrapped: bytes) -> bytes:
        if len(wrapped) <= NONCE_SIZE:
            raise EncryptionError("wrapped DEK is too short")
        nonce, ct = wrapped[:NONCE_SIZE], wrapped[NONCE_SIZE:]
        aad = tenant_id.encode("utf-8")
        try:
            return self._aead.decrypt(nonce, ct, aad)
        except InvalidTag as exc:
            # WHY: surface a uniform error type so callers don't have to
            # reach into cryptography's exception hierarchy. The original
            # tenant_id is intentionally left out of the message — we do
            # not want a confused-deputy probe to learn which tenant a
            # wrapped DEK belongs to.
            raise EncryptionError(
                "failed to unwrap DEK: KEK or tenant_id mismatch"
            ) from exc


def generate_dek() -> bytes:
    """Generate a fresh per-tenant 32-byte DEK from the OS CSPRNG."""
    return os.urandom(DEK_SIZE)


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    """AES-GCM ciphertext bundle for one finding, ready for shard write.

    `tenant_id` is stored on the wire so a multi-tenant index that
    re-encrypts under a rotated DEK can locate the correct wrapped DEK
    on read. `__repr__` is overridden to refuse to leak the ciphertext
    bytes so an accidental log statement still hides the encrypted blob
    length pattern (which can fingerprint the underlying value class).
    """

    tenant_id: str
    nonce: bytes
    ciphertext: bytes
    tag: bytes

    def __repr__(self) -> str:
        return f"EncryptedPayload(tenant_id={self.tenant_id!r}, <redacted>)"

    def to_jsonl(self) -> dict[str, str]:
        """Serialize to the JSONL on-disk shape (base64 fields)."""
        return {
            "tenant_id": self.tenant_id,
            "nonce_b64": base64.b64encode(self.nonce).decode("ascii"),
            "ciphertext_b64": base64.b64encode(self.ciphertext).decode("ascii"),
            "tag_b64": base64.b64encode(self.tag).decode("ascii"),
        }

    @classmethod
    def from_jsonl(cls, obj: dict[str, str]) -> EncryptedPayload:
        """Inverse of `to_jsonl`; raises EncryptionError on shape errors."""
        try:
            return cls(
                tenant_id=obj["tenant_id"],
                nonce=base64.b64decode(obj["nonce_b64"]),
                ciphertext=base64.b64decode(obj["ciphertext_b64"]),
                tag=base64.b64decode(obj["tag_b64"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise EncryptionError(f"malformed EncryptedPayload: {exc}") from exc


def encrypt_payload(
    dek: bytes, tenant_id: str, plaintext: dict[str, object]
) -> EncryptedPayload:
    """Encrypt a JSON-serializable payload under `dek` for `tenant_id`."""
    if len(dek) != DEK_SIZE:
        raise ValueError(f"DEK must be {DEK_SIZE} bytes; got {len(dek)}")
    aead = AESGCM(dek)
    nonce = os.urandom(NONCE_SIZE)
    pt_bytes = json.dumps(plaintext, ensure_ascii=False, sort_keys=True).encode(
        "utf-8"
    )
    aad = tenant_id.encode("utf-8")
    combined = aead.encrypt(nonce, pt_bytes, aad)
    # WHY: cryptography's AESGCM returns ciphertext||tag concatenated. We
    # split the trailing 16-byte tag so the wire format keeps tag
    # isolated for future AEAD swaps.
    ciphertext, tag = combined[:-TAG_SIZE], combined[-TAG_SIZE:]
    return EncryptedPayload(
        tenant_id=tenant_id, nonce=nonce, ciphertext=ciphertext, tag=tag
    )


def decrypt_payload(dek: bytes, payload: EncryptedPayload) -> dict[str, object]:
    """Decrypt an `EncryptedPayload` back into the original dict."""
    if len(dek) != DEK_SIZE:
        raise ValueError(f"DEK must be {DEK_SIZE} bytes; got {len(dek)}")
    aead = AESGCM(dek)
    aad = payload.tenant_id.encode("utf-8")
    try:
        pt_bytes = aead.decrypt(
            payload.nonce, payload.ciphertext + payload.tag, aad
        )
    except InvalidTag as exc:
        raise EncryptionError(
            "failed to decrypt payload: wrong DEK, tenant, or tampered ciphertext"
        ) from exc
    obj = json.loads(pt_bytes.decode("utf-8"))
    if not isinstance(obj, dict):
        raise EncryptionError(
            f"decrypted payload is not a JSON object: {type(obj).__name__}"
        )
    return obj


__all__ = [
    "DEK_SIZE",
    "EncryptedPayload",
    "EncryptionError",
    "InMemoryKekProvider",
    "KekProvider",
    "NONCE_SIZE",
    "TAG_SIZE",
    "decrypt_payload",
    "encrypt_payload",
    "generate_dek",
]
