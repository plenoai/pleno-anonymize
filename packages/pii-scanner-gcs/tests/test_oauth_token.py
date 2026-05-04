"""Tests for the OAuth2 token acquisition layer — all three modes,
plus cache hit/miss/refresh behavior.

Hermetic: every network call goes through `httpx.MockTransport`. The
RS256 signature is structurally verified using the same public key
the test fixture minted.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from pleno_pii_scanner_gcs._oauth_token import (
    AccessToken,
    ApplicationDefaultTokenSource,
    ServiceAccountKeyTokenSource,
    TokenCache,
    WorkloadIdentityTokenSource,
)


def _b64url_decode(value: str) -> bytes:
    """Reverse the JWT URL-safe base64-without-padding encoding."""
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


# --- ServiceAccountKeyTokenSource -----------------------------------


class TestServiceAccountKey:
    async def test_signs_and_exchanges(
        self, service_account_key: dict[str, Any], rsa_private_pem: str
    ) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "oauth2.googleapis.com"
            assert request.url.path == "/token"
            # The form-encoded body carries the signed JWT in
            # `assertion=`. Verify the signature roundtrips against the
            # fixture's public key.
            body = request.content.decode()
            params = dict(p.split("=", 1) for p in body.split("&"))
            assertion = httpx.QueryParams(body).get("assertion")
            assert assertion is not None
            captured["assertion"] = assertion
            return httpx.Response(
                200,
                json={"access_token": "tok-abc", "expires_in": 3600},
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = ServiceAccountKeyTokenSource(key_data=service_account_key)
            token = await src.acquire(client)
        assert token.value == "tok-abc"
        # Verify the JWT signature with the fixture's public key.
        header_b64, payload_b64, sig_b64 = captured["assertion"].split(".")
        signing_input = f"{header_b64}.{payload_b64}".encode()
        signature = _b64url_decode(sig_b64)
        public_key = serialization.load_pem_private_key(
            rsa_private_pem.encode(), password=None
        ).public_key()
        public_key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        # Header has the kid from the SA key.
        header = json.loads(_b64url_decode(header_b64))
        assert header["alg"] == "RS256"
        assert header["kid"] == "kid-deadbeef"
        # Payload has the iss + scope.
        payload = json.loads(_b64url_decode(payload_b64))
        assert payload["iss"] == service_account_key["client_email"]
        assert "cloud-platform.read-only" in payload["scope"]

    async def test_rejects_missing_client_email(
        self, service_account_key: dict[str, Any]
    ) -> None:
        broken = dict(service_account_key)
        del broken["client_email"]

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(500))
        ) as client:
            src = ServiceAccountKeyTokenSource(key_data=broken)
            with pytest.raises(ValueError, match="client_email"):
                await src.acquire(client)

    async def test_rejects_non_string_private_key(
        self, service_account_key: dict[str, Any]
    ) -> None:
        broken = dict(service_account_key)
        broken["private_key"] = 12345  # type: ignore[assignment]

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(500))
        ) as client:
            src = ServiceAccountKeyTokenSource(key_data=broken)
            with pytest.raises(ValueError, match="PEM string"):
                await src.acquire(client)

    async def test_token_endpoint_error_does_not_leak_body(
        self, service_account_key: dict[str, Any]
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    400, json={"error": "invalid_grant", "assertion": "LEAK"}
                )
            )
        ) as client:
            src = ServiceAccountKeyTokenSource(key_data=service_account_key)
            with pytest.raises(httpx.HTTPStatusError) as info:
                await src.acquire(client)
        # Error message is structural — the upstream body must NOT leak
        # the assertion (signed JWT) string.
        assert "LEAK" not in str(info.value)
        assert "status=400" in str(info.value)

    async def test_token_endpoint_missing_access_token(
        self, service_account_key: dict[str, Any]
    ) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"expires_in": 3600})
            )
        ) as client:
            src = ServiceAccountKeyTokenSource(key_data=service_account_key)
            with pytest.raises(ValueError, match="missing access_token"):
                await src.acquire(client)


# --- WorkloadIdentityTokenSource ------------------------------------


class TestWorkloadIdentity:
    async def test_two_step_exchange_with_impersonation(
        self, tmp_path
    ) -> None:
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("external-oidc-token")
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            calls.append(url)
            if "sts.googleapis.com" in url:
                # STS receives the external OIDC token in `subjectToken`.
                body = json.loads(request.content)
                assert body["subjectToken"] == "external-oidc-token"
                assert body["audience"].startswith("//iam.googleapis.com/")
                return httpx.Response(
                    200,
                    json={
                        "access_token": "federated-tok",
                        "expires_in": 3600,
                    },
                )
            if "iamcredentials.googleapis.com" in url:
                # Impersonation step requires the federated token in
                # the Authorization header.
                assert request.headers["Authorization"] == "Bearer federated-tok"
                return httpx.Response(
                    200,
                    json={
                        "accessToken": "sa-tok",
                        "expireTime": "2099-01-01T00:00:00Z",
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                audience=(
                    "//iam.googleapis.com/projects/123/locations/global/"
                    "workloadIdentityPools/p/providers/q"
                ),
                token_path=str(token_file),
                service_account_email="scanner@p.iam.gserviceaccount.com",
            )
            token = await src.acquire(client)
        assert token.value == "sa-tok"
        assert token.expires_at.year == 2099
        # Two upstream calls: STS then iamcredentials.
        assert any("sts.googleapis" in c for c in calls)
        assert any("iamcredentials.googleapis" in c for c in calls)

    async def test_no_impersonation_returns_federated_directly(
        self, tmp_path
    ) -> None:
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("ext-tok")

        def handler(request: httpx.Request) -> httpx.Response:
            assert "sts.googleapis.com" in str(request.url)
            return httpx.Response(
                200, json={"access_token": "fed", "expires_in": 1800}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                audience="//iam.googleapis.com/x",
                token_path=str(token_file),
            )
            token = await src.acquire(client)
        assert token.value == "fed"
        # ~30 minutes ahead, allow a 5-second clock skew.
        assert (token.expires_at - datetime.now(UTC)).total_seconds() > 1700

    async def test_sts_missing_access_token_rejected(
        self, tmp_path
    ) -> None:
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("ext")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"expires_in": 60})
            )
        ) as client:
            src = WorkloadIdentityTokenSource(
                audience="//iam.googleapis.com/x",
                token_path=str(token_file),
            )
            with pytest.raises(ValueError, match="no access_token"):
                await src.acquire(client)

    async def test_naive_expire_time_normalized_to_utc(self, tmp_path) -> None:
        # iamcredentials normally returns RFC3339 with `Z`. Defensively
        # cover the naive-ISO form so an unusual fork / mock does not
        # produce a tz-naive AccessToken (cache comparisons would crash).
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("ext")

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "sts.googleapis.com" in url:
                return httpx.Response(
                    200, json={"access_token": "fed", "expires_in": 60}
                )
            if "iamcredentials.googleapis.com" in url:
                return httpx.Response(
                    200,
                    json={
                        "accessToken": "sa",
                        "expireTime": "2099-01-01T00:00:00",  # no Z, no tz
                    },
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                audience="//iam.googleapis.com/x",
                token_path=str(token_file),
                service_account_email="x@p.iam.gserviceaccount.com",
            )
            token = await src.acquire(client)
        assert token.expires_at.tzinfo == UTC

    async def test_impersonate_missing_fields_rejected(
        self, tmp_path
    ) -> None:
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("ext")

        def handler(request: httpx.Request) -> httpx.Response:
            if "sts.googleapis.com" in str(request.url):
                return httpx.Response(
                    200, json={"access_token": "fed", "expires_in": 60}
                )
            # iamcredentials returns body without expireTime.
            return httpx.Response(200, json={"accessToken": "x"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                audience="//iam.googleapis.com/x",
                token_path=str(token_file),
                service_account_email="x@p.iam.gserviceaccount.com",
            )
            with pytest.raises(ValueError, match="malformed"):
                await src.acquire(client)

    async def test_token_reader_seam_used_when_provided(self) -> None:
        async def reader(path: str) -> str:
            assert path == "/no/such/file"
            return "from-seam"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["subjectToken"] == "from-seam"
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 60}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                audience="//iam.googleapis.com/x",
                token_path="/no/such/file",
                token_reader=reader,
            )
            tok = await src.acquire(client)
        assert tok.value == "tok"


# --- ApplicationDefaultTokenSource ----------------------------------


class TestApplicationDefault:
    async def test_env_path_delegates_to_sa_source(
        self, service_account_key: dict[str, Any]
    ) -> None:
        # Simulate GOOGLE_APPLICATION_CREDENTIALS pointing at a key file.
        def env_get(name: str) -> str | None:
            return "/fake/path" if name == "GOOGLE_APPLICATION_CREDENTIALS" else None

        def file_reader(path: str) -> str:
            assert path == "/fake/path"
            return json.dumps(service_account_key)

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "oauth2.googleapis.com"
            return httpx.Response(
                200, json={"access_token": "adc-tok", "expires_in": 60}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = ApplicationDefaultTokenSource(
                env_get=env_get, file_reader=file_reader
            )
            token = await src.acquire(client)
        assert token.value == "adc-tok"

    async def test_metadata_server_path(self) -> None:
        def env_get(_name: str) -> str | None:
            return None

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "metadata.google.internal"
            assert request.headers.get("Metadata-Flavor") == "Google"
            return httpx.Response(
                200, json={"access_token": "metadata-tok", "expires_in": 900}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = ApplicationDefaultTokenSource(env_get=env_get)
            token = await src.acquire(client)
        assert token.value == "metadata-tok"

    async def test_metadata_missing_token_rejected(self) -> None:
        def env_get(_name: str) -> str | None:
            return None

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200, json={"expires_in": 60}
                )
            )
        ) as client:
            src = ApplicationDefaultTokenSource(env_get=env_get)
            with pytest.raises(ValueError, match="no access_token"):
                await src.acquire(client)


# --- TokenCache hit/miss/refresh ------------------------------------


class TestTokenCache:
    async def test_hit_returns_cached(self) -> None:
        calls = {"n": 0}

        class _OneShotSource:
            async def acquire(self, _client):
                calls["n"] += 1
                return AccessToken(
                    value=f"t-{calls['n']}",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

        cache = TokenCache(source=_OneShotSource())
        async with httpx.AsyncClient() as client:
            t1 = await cache.get(client)
            t2 = await cache.get(client)
        assert t1.value == t2.value == "t-1"
        assert calls["n"] == 1

    async def test_miss_then_refresh_when_near_expiry(self) -> None:
        # Use a fixed clock so the cache check is deterministic.
        now_box = {"now": datetime(2026, 1, 1, tzinfo=UTC)}

        def now_fn():
            return now_box["now"]

        calls = {"n": 0}

        class _OneShotSource:
            async def acquire(self, _client):
                calls["n"] += 1
                return AccessToken(
                    value=f"t-{calls['n']}",
                    expires_at=now_fn() + timedelta(seconds=20),
                )

        cache = TokenCache(source=_OneShotSource(), now=now_fn)
        async with httpx.AsyncClient() as client:
            t1 = await cache.get(client)
            # Cached token expires in 20 s but safety margin is 30 s,
            # so the next get() must refresh.
            t2 = await cache.get(client)
        assert t1.value == "t-1"
        assert t2.value == "t-2"
        assert calls["n"] == 2

    async def test_invalidate_forces_refresh(self) -> None:
        calls = {"n": 0}

        class _Source:
            async def acquire(self, _client):
                calls["n"] += 1
                return AccessToken(
                    value=f"t-{calls['n']}",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

        cache = TokenCache(source=_Source())
        async with httpx.AsyncClient() as client:
            await cache.get(client)
            cache.invalidate()
            t = await cache.get(client)
        assert t.value == "t-2"
        assert calls["n"] == 2


# --- AccessToken --------------------------------------------------


class TestAccessToken:
    def test_is_expired_within_safety_margin(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        # 10 s remaining < 30 s safety margin → expired.
        token = AccessToken(value="x", expires_at=now + timedelta(seconds=10))
        assert token.is_expired(now) is True

    def test_is_not_expired_with_long_ttl(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        token = AccessToken(value="x", expires_at=now + timedelta(hours=1))
        assert token.is_expired(now) is False
