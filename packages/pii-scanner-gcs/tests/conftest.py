"""Shared fixtures: RSA key + isolated registry.

The RSA key is generated once per test session (slow-ish: ~50 ms) and
re-used across every test that needs to mint a service-account JWT.
We never write it to disk; tests pass the in-memory key dict directly
into `ServiceAccountKeyTokenSource`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from pleno_pii_scanner.sources import registry as _registry_mod


@pytest.fixture(scope="session")
def rsa_private_pem() -> str:
    """A fresh 2048-bit RSA private key in PKCS8 PEM format.

    2048 is the smallest size Google accepts for SA key uploads. We
    only use it inside the test process to sign and verify JWTs;
    nothing is persisted.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode("utf-8")


@pytest.fixture
def service_account_key(rsa_private_pem: str) -> dict[str, Any]:
    """A minimal service-account JSON-key dict, fields matching Google's."""
    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "kid-deadbeef",
        "private_key": rsa_private_pem,
        "client_email": "scanner@test-project.iam.gserviceaccount.com",
        "client_id": "123",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


@pytest.fixture
def service_account_key_file(tmp_path, service_account_key) -> str:
    """Write the SA key JSON to a tmp file and return the path."""
    p = tmp_path / "sa-key.json"
    p.write_text(json.dumps(service_account_key))
    return str(p)


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch):
    """Stub entry-points discovery so our tests do not pick up other
    installed connector wheels (which would bleed kinds across tests)."""
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()
