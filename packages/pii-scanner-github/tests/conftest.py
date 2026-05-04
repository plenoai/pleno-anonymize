"""Shared pytest fixtures for pleno-pii-scanner-github.

The single most-used fixture is `rsa_pem`: a fresh 2048-bit RSA key
generated once per test session. We don't ship a hard-coded test key
because anything committed to the repo gets indexed by GitHub's secret
scanner — even a clearly-marked test key would generate noise.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture(scope="session")
def rsa_pem() -> str:
    """A fresh PEM-encoded RSA private key for App-auth tests.

    Session-scoped because RSA keygen is ~200ms per 2048-bit pair and
    we do not need a different key per test for any correctness reason.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
