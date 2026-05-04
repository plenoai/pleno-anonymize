"""Tests for app_auth: JWT minting + installation token caching."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding

from pleno_pii_scanner_github.api import GithubApi
from pleno_pii_scanner_github.app_auth import (
    AppAuth,
    InstallationToken,
    _parse_expiry,
    mint_app_jwt,
)


# ---------------------------------------------------------------------
# mint_app_jwt
# ---------------------------------------------------------------------


class TestMintAppJwt:
    def test_returns_three_part_jwt(self, rsa_pem: str) -> None:
        token = mint_app_jwt(app_id="123", private_key_pem=rsa_pem, now=1000.0)
        parts = token.split(".")
        assert len(parts) == 3

    def test_payload_contains_iat_exp_iss(self, rsa_pem: str) -> None:
        token = mint_app_jwt(app_id="42", private_key_pem=rsa_pem, now=1000.0)
        _, payload_b64, _ = token.split(".")
        payload = json.loads(_b64decode(payload_b64))
        # iat shifted -30s for clock skew.
        assert payload["iat"] == 970
        assert payload["exp"] == 1000 + 9 * 60
        assert payload["iss"] == "42"

    def test_app_id_int_is_stringified(self, rsa_pem: str) -> None:
        token = mint_app_jwt(app_id=99, private_key_pem=rsa_pem, now=1000.0)
        _, payload_b64, _ = token.split(".")
        payload = json.loads(_b64decode(payload_b64))
        assert payload["iss"] == "99"

    def test_signature_verifies_with_public_key(self, rsa_pem: str) -> None:
        token = mint_app_jwt(app_id="1", private_key_pem=rsa_pem, now=1000.0)
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode()
        signature = _b64decode(sig_b64)
        public_key = serialization.load_pem_private_key(
            rsa_pem.encode(), password=None
        ).public_key()
        # No exception => signature valid.
        public_key.verify(
            signature, signing_input, padding.PKCS1v15(), hashes.SHA256()
        )

    def test_uses_default_clock_when_now_omitted(self, rsa_pem: str) -> None:
        # Just smoke — mint_app_jwt(now=None) must not crash.
        token = mint_app_jwt(app_id="1", private_key_pem=rsa_pem)
        assert token.count(".") == 2

    def test_accepts_bytes_pem(self, rsa_pem: str) -> None:
        token = mint_app_jwt(
            app_id="1", private_key_pem=rsa_pem.encode(), now=1000.0
        )
        assert token.count(".") == 2

    def test_rejects_non_rsa_key(self) -> None:
        ec_key = ec.generate_private_key(ec.SECP256R1())
        ec_pem = ec_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        with pytest.raises(ValueError, match="must be RSA"):
            mint_app_jwt(app_id="1", private_key_pem=ec_pem, now=1000.0)

    def test_custom_ttl_reflected_in_exp(self, rsa_pem: str) -> None:
        token = mint_app_jwt(
            app_id="1", private_key_pem=rsa_pem, now=1000.0, ttl_seconds=120
        )
        _, payload_b64, _ = token.split(".")
        payload = json.loads(_b64decode(payload_b64))
        assert payload["exp"] - payload["iat"] == 120 + 30


# ---------------------------------------------------------------------
# _parse_expiry
# ---------------------------------------------------------------------


class TestParseExpiry:
    def test_iso_8601_with_z_suffix(self) -> None:
        ts = _parse_expiry({"expires_at": "2024-01-01T00:00:00Z"}, now=0.0)
        assert ts == 1704067200.0

    def test_iso_8601_with_offset(self) -> None:
        ts = _parse_expiry(
            {"expires_at": "2024-01-01T00:00:00+00:00"}, now=0.0
        )
        assert ts == 1704067200.0

    def test_relative_expires_in_fallback(self) -> None:
        ts = _parse_expiry({"expires_in": 3600}, now=1000.0)
        assert ts == 4600.0

    def test_relative_float_accepted(self) -> None:
        ts = _parse_expiry({"expires_in": 1800.5}, now=1000.0)
        assert ts == 2800.5

    def test_missing_both_defaults_to_one_hour(self) -> None:
        ts = _parse_expiry({}, now=1000.0)
        assert ts == 4600.0

    def test_unparseable_iso_raises(self) -> None:
        with pytest.raises(PermissionError, match="expires_at"):
            _parse_expiry({"expires_at": "not-a-date"}, now=0.0)


# ---------------------------------------------------------------------
# AppAuth installation token cache
# ---------------------------------------------------------------------


def _make_api(handler) -> GithubApi:
    transport = httpx.MockTransport(handler)
    return GithubApi(transport=transport)


class TestAppAuthCache:
    async def test_first_call_mints_via_jwt_then_returns_installation_token(
        self, rsa_pem: str
    ) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers["Authorization"]
            seen["url"] = str(request.url)
            return httpx.Response(
                201,
                json={
                    "token": "ghs_install_abc",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )

        api = _make_api(handler)
        auth = AppAuth(
            app_id="1",
            installation_id="42",
            private_key_pem=rsa_pem,
            api=api,
            now=lambda: 1000.0,
        )
        try:
            token = await auth.get_installation_token()
            assert token == "ghs_install_abc"
            assert seen["auth"].startswith("Bearer ey")  # JWT, not ghs_
            assert "/app/installations/42/access_tokens" in seen["url"]
        finally:
            await api.aclose()

    async def test_cached_token_is_reused_within_skew_window(
        self, rsa_pem: str
    ) -> None:
        calls = {"n": 0}

        def handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                201,
                json={
                    "token": "ghs_x",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )

        api = _make_api(handler)
        auth = AppAuth(
            app_id="1",
            installation_id="42",
            private_key_pem=rsa_pem,
            api=api,
            now=lambda: 1000.0,
        )
        try:
            await auth.get_installation_token()
            await auth.get_installation_token()
            await auth.get_installation_token()
            assert calls["n"] == 1
        finally:
            await api.aclose()

    async def test_token_refreshed_when_within_skew_of_expiry(
        self, rsa_pem: str
    ) -> None:
        responses = iter(
            [
                httpx.Response(
                    201,
                    json={
                        "token": "ghs_first",
                        "expires_at": "1970-01-01T00:01:00Z",  # ~60s past epoch
                    },
                ),
                httpx.Response(
                    201,
                    json={
                        "token": "ghs_second",
                        "expires_at": "2099-01-01T00:00:00Z",
                    },
                ),
            ]
        )

        def handler(_: httpx.Request) -> httpx.Response:
            return next(responses)

        api = _make_api(handler)
        # `now` returns 0 — first token expires in 60s but skew=300s, so
        # the cache treats it as expired and refreshes immediately.
        auth = AppAuth(
            app_id="1",
            installation_id="42",
            private_key_pem=rsa_pem,
            api=api,
            now=lambda: 0.0,
        )
        try:
            t1 = await auth.get_installation_token()
            t2 = await auth.get_installation_token()
            assert t1 == "ghs_first"
            assert t2 == "ghs_second"
        finally:
            await api.aclose()

    async def test_failure_response_raises_permission_error(
        self, rsa_pem: str
    ) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="installation not found")

        api = _make_api(handler)
        auth = AppAuth(
            app_id="1",
            installation_id="42",
            private_key_pem=rsa_pem,
            api=api,
            now=lambda: 1000.0,
        )
        try:
            with pytest.raises(PermissionError, match="404"):
                await auth.get_installation_token()
        finally:
            await api.aclose()

    async def test_concurrent_first_call_only_mints_once(
        self, rsa_pem: str
    ) -> None:
        # Two coroutines race past the unlocked check at the same time.
        # The asyncio.Lock + double-checked re-read of `self._cached`
        # must collapse them onto a single mint. We assert by counting
        # how many times the access_tokens endpoint is called.
        import asyncio

        calls = {"n": 0}

        async def slow_handler(_: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            # Yield once so the other coroutine grabs the lock-wait.
            await asyncio.sleep(0)
            return httpx.Response(
                201,
                json={
                    "token": "ghs_x",
                    "expires_at": "2099-01-01T00:00:00Z",
                },
            )

        # MockTransport accepts an async handler returning Response.
        api = GithubApi(transport=httpx.MockTransport(slow_handler))
        auth = AppAuth(
            app_id="1",
            installation_id="42",
            private_key_pem=rsa_pem,
            api=api,
            now=lambda: 1000.0,
        )
        try:
            await asyncio.gather(
                auth.get_installation_token(),
                auth.get_installation_token(),
                auth.get_installation_token(),
            )
            assert calls["n"] == 1
        finally:
            await api.aclose()

    async def test_install_token_seam_pre_seeds_cache(
        self, rsa_pem: str
    ) -> None:
        # If the cache is pre-populated with a still-valid token, no
        # network call should happen at all.
        def handler(_: httpx.Request) -> httpx.Response:
            raise AssertionError("must not hit the network")

        api = _make_api(handler)
        auth = AppAuth(
            app_id="1",
            installation_id="42",
            private_key_pem=rsa_pem,
            api=api,
            now=lambda: 1000.0,
        )
        try:
            auth.install_token("ghs_pre", expires_at=1_000_000.0)
            assert await auth.get_installation_token() == "ghs_pre"
        finally:
            await api.aclose()


class TestInstallationToken:
    def test_frozen_dataclass(self) -> None:
        t = InstallationToken(token="ghs_x", expires_at=123.0)
        with pytest.raises(Exception):
            t.token = "ghs_y"  # type: ignore[misc]


def _b64decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)
