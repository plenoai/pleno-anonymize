"""Tests for the auth layer — three modes (WIF, IMDS, Shared Key),
TokenCache hit/miss/refresh, and the byte-exact Shared Key signature
golden vector.

Hermetic: every network call goes through `httpx.MockTransport`. No
network. The Shared Key signature is verified against a fixed
StringToSign so any drift from the documented recipe is caught.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from pleno_pii_scanner_azure_blob._auth import (
    AZURE_STORAGE_DEFAULT_SCOPE,
    AccessToken,
    ManagedIdentityTokenSource,
    SharedKeyCredential,
    TokenCache,
    WorkloadIdentityTokenSource,
    sign_shared_key,
)


# --- AccessToken ---------------------------------------------------


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


# --- WorkloadIdentityTokenSource ------------------------------------


class TestWorkloadIdentity:
    async def test_oidc_jwt_exchanged_via_entra(self, tmp_path) -> None:
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("external-oidc-jwt")
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "login.microsoftonline.com"
            assert request.url.path == "/tenant-abc/oauth2/v2.0/token"
            body = request.content.decode()
            params = dict(httpx.QueryParams(body).items())
            captured.update(params)
            return httpx.Response(
                200,
                json={
                    "access_token": "azure-storage-bearer",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                tenant_id="tenant-abc",
                client_id="client-xyz",
                oidc_token_path=str(token_file),
            )
            token = await src.acquire(client)
        assert token.value == "azure-storage-bearer"
        # The form body carries the federated grant.
        assert captured["grant_type"] == "client_credentials"
        assert captured["client_id"] == "client-xyz"
        assert captured["client_assertion"] == "external-oidc-jwt"
        assert (
            captured["client_assertion_type"]
            == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        assert captured["scope"] == AZURE_STORAGE_DEFAULT_SCOPE

    async def test_oidc_token_reread_each_call(self, tmp_path) -> None:
        # The platform rotates the file every ~10 min. Re-reading
        # rather than capturing means we pick up the new JWT.
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("first-jwt")

        seen_assertions: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(request.content.decode()).items())
            seen_assertions.append(params["client_assertion"])
            return httpx.Response(
                200, json={"access_token": "x", "expires_in": 60}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                tenant_id="t",
                client_id="c",
                oidc_token_path=str(token_file),
            )
            await src.acquire(client)
            token_file.write_text("second-jwt")
            await src.acquire(client)
        assert seen_assertions == ["first-jwt", "second-jwt"]

    async def test_token_reader_seam_used_when_provided(self) -> None:
        async def reader(path: str) -> str:
            assert path == "/no/such/file"
            return "from-seam"

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(httpx.QueryParams(request.content.decode()).items())
            assert params["client_assertion"] == "from-seam"
            return httpx.Response(
                200, json={"access_token": "tok", "expires_in": 60}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = WorkloadIdentityTokenSource(
                tenant_id="t",
                client_id="c",
                oidc_token_path="/no/such/file",
                token_reader=reader,
            )
            tok = await src.acquire(client)
        assert tok.value == "tok"

    async def test_entra_error_does_not_leak_assertion(
        self, tmp_path
    ) -> None:
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("LEAKING-JWT")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    400, json={"error": "invalid_client", "client_assertion": "LEAKING-JWT"}
                )
            )
        ) as client:
            src = WorkloadIdentityTokenSource(
                tenant_id="t", client_id="c", oidc_token_path=str(token_file)
            )
            with pytest.raises(httpx.HTTPStatusError) as info:
                await src.acquire(client)
        assert "LEAKING-JWT" not in str(info.value)
        assert "status=400" in str(info.value)

    async def test_entra_missing_access_token_rejected(
        self, tmp_path
    ) -> None:
        token_file = tmp_path / "oidc.jwt"
        token_file.write_text("ok")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"expires_in": 60})
            )
        ) as client:
            src = WorkloadIdentityTokenSource(
                tenant_id="t", client_id="c", oidc_token_path=str(token_file)
            )
            with pytest.raises(ValueError, match="missing access_token"):
                await src.acquire(client)


# --- ManagedIdentityTokenSource ------------------------------------


class TestManagedIdentity:
    async def test_imds_endpoint_called_with_metadata_header(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "169.254.169.254"
            assert request.url.path == "/metadata/identity/oauth2/token"
            assert request.headers.get("Metadata") == "true"
            assert request.url.params.get("api-version") == "2018-02-01"
            assert (
                request.url.params.get("resource")
                == "https://storage.azure.com/"
            )
            assert request.url.params.get("client_id") is None
            return httpx.Response(
                200,
                json={
                    "access_token": "imds-bearer",
                    "expires_in": "3600",  # IMDS sometimes returns string
                    "token_type": "Bearer",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = ManagedIdentityTokenSource()
            token = await src.acquire(client)
        assert token.value == "imds-bearer"

    async def test_user_assigned_client_id_added(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params.get("client_id") == "uami-1234"
            return httpx.Response(
                200, json={"access_token": "x", "expires_in": 60}
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = ManagedIdentityTokenSource(client_id="uami-1234")
            await src.acquire(client)

    async def test_imds_non_200_raises(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(500, text="oops")
            )
        ) as client:
            src = ManagedIdentityTokenSource()
            with pytest.raises(httpx.HTTPStatusError) as info:
                await src.acquire(client)
        assert "status=500" in str(info.value)

    async def test_imds_missing_access_token_rejected(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(200, json={"expires_in": 60})
            )
        ) as client:
            src = ManagedIdentityTokenSource()
            with pytest.raises(ValueError, match="missing access_token"):
                await src.acquire(client)

    async def test_imds_unparseable_expires_in_falls_back(self) -> None:
        # IMDS has been known to return non-numeric `expires_in` on
        # rare cluster bugs; fall back to a 1h default rather than
        # crashing the scan.
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "x",
                    "expires_in": "not-a-number",
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            src = ManagedIdentityTokenSource()
            tok = await src.acquire(client)
        # ~1 h ahead, allow 5 s skew.
        delta = (tok.expires_at - datetime.now(UTC)).total_seconds()
        assert 3500 < delta < 3700


# --- TokenCache ----------------------------------------------------


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
            # Cached token expires in 20s; safety margin is 30s →
            # next get() must refresh.
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


# --- SharedKeyCredential validation -------------------------------


class TestSharedKeyCredential:
    def test_valid_key_accepted(self) -> None:
        # 32 bytes of NUL → valid base64.
        key = base64.b64encode(b"\0" * 32).decode("ascii")
        SharedKeyCredential(account_name="acct", account_key_b64=key)

    def test_invalid_base64_rejected(self) -> None:
        with pytest.raises(ValueError, match="not valid base64"):
            SharedKeyCredential(
                account_name="acct", account_key_b64="not-base64!!!"
            )

    def test_empty_account_name_rejected(self) -> None:
        key = base64.b64encode(b"x" * 32).decode("ascii")
        with pytest.raises(ValueError, match="account_name"):
            SharedKeyCredential(account_name="", account_key_b64=key)


# --- Shared Key signing — golden vector ---------------------------


class TestSharedKeySignature:
    def test_golden_vector_listing(self) -> None:
        """Reproduce the official Microsoft docs example for Shared Key
        signing of `GET /?comp=list`.

        Request: `GET http://myaccount.blob.core.windows.net/?comp=list`
        with `x-ms-date: Fri, 26 Jun 2015 23:39:12 GMT` and
        `x-ms-version: 2015-02-21`.

        Expected `StringToSign`:

            GET\n\n\n\n\n\n\n\n\n\n\n\n
            x-ms-date:Fri, 26 Jun 2015 23:39:12 GMT\n
            x-ms-version:2015-02-21\n
            /myaccount/\n
            comp:list

        We compute the canonical signature ourselves from the same
        StringToSign, then compare against `sign_shared_key` to make
        sure the function emits byte-exactly the same bytes.
        """
        # Use a known non-trivial 32-byte key so the HMAC is not all
        # zeros (which could mask sign-of-data bugs).
        key_raw = b"\x01\x02\x03\x04\x05\x06\x07\x08" * 4
        key_b64 = base64.b64encode(key_raw).decode("ascii")
        credential = SharedKeyCredential(
            account_name="myaccount", account_key_b64=key_b64
        )
        url = httpx.URL(
            "http://myaccount.blob.core.windows.net/?comp=list"
        )
        headers = {
            "x-ms-date": "Fri, 26 Jun 2015 23:39:12 GMT",
            "x-ms-version": "2015-02-21",
        }
        # Hand-construct StringToSign per the spec.
        expected_sts = (
            "GET\n"      # VERB
            "\n"         # Content-Encoding
            "\n"         # Content-Language
            "\n"         # Content-Length (0 → empty)
            "\n"         # Content-MD5
            "\n"         # Content-Type
            "\n"         # Date (empty because x-ms-date is set)
            "\n"         # If-Modified-Since
            "\n"         # If-Match
            "\n"         # If-None-Match
            "\n"         # If-Unmodified-Since
            "\n"         # Range
            "x-ms-date:Fri, 26 Jun 2015 23:39:12 GMT\n"
            "x-ms-version:2015-02-21\n"
            "/myaccount/\n"
            "comp:list"
        )
        expected_sig = base64.b64encode(
            hmac.new(
                key_raw, expected_sts.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode("ascii")
        expected_header = f"SharedKey myaccount:{expected_sig}"
        actual = sign_shared_key(
            method="GET",
            url=url,
            headers=headers,
            credential=credential,
            content_length=0,
        )
        assert actual == expected_header

    def test_signature_includes_canonical_resource_path(self) -> None:
        # GET /container/blob — the path must appear in the canonical
        # resource exactly once, prefixed by the account name.
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        credential = SharedKeyCredential(
            account_name="acct", account_key_b64=key_b64
        )
        url = httpx.URL("https://acct.blob.core.windows.net/c/blob.txt")
        sig = sign_shared_key(
            method="GET",
            url=url,
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
            },
            credential=credential,
            content_length=0,
        )
        # Re-derive to confirm the function did the right thing.
        sts = (
            "GET\n\n\n\n\n\n\n\n\n\n\n\n"
            "x-ms-date:Wed, 01 Jan 2026 00:00:00 GMT\n"
            "x-ms-version:2023-11-03\n"
            "/acct/c/blob.txt"
        )
        expected_b64 = base64.b64encode(
            hmac.new(
                b"k" * 32, sts.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode("ascii")
        assert sig == f"SharedKey acct:{expected_b64}"

    def test_signature_with_query_params_grouped_and_sorted(self) -> None:
        # `comp=list&prefix=foo&restype=container` — names lowercased,
        # sorted, and joined with `\n`.
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        credential = SharedKeyCredential(
            account_name="acct", account_key_b64=key_b64
        )
        url = httpx.URL(
            "https://acct.blob.core.windows.net/c?restype=container&comp=list&prefix=foo"
        )
        sig = sign_shared_key(
            method="GET",
            url=url,
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
            },
            credential=credential,
            content_length=0,
        )
        # Canonical resource has the sorted query lines.
        sts = (
            "GET\n\n\n\n\n\n\n\n\n\n\n\n"
            "x-ms-date:Wed, 01 Jan 2026 00:00:00 GMT\n"
            "x-ms-version:2023-11-03\n"
            "/acct/c\n"
            "comp:list\n"
            "prefix:foo\n"
            "restype:container"
        )
        expected_b64 = base64.b64encode(
            hmac.new(
                b"k" * 32, sts.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode("ascii")
        assert sig == f"SharedKey acct:{expected_b64}"

    def test_repeated_query_param_values_comma_joined_sorted(self) -> None:
        # Spec example: `comp=metadata&comp=list` → `comp:list,metadata`.
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        credential = SharedKeyCredential(
            account_name="acct", account_key_b64=key_b64
        )
        url = httpx.URL(
            "https://acct.blob.core.windows.net/c?comp=metadata&comp=list"
        )
        sig = sign_shared_key(
            method="GET",
            url=url,
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
            },
            credential=credential,
            content_length=0,
        )
        sts = (
            "GET\n\n\n\n\n\n\n\n\n\n\n\n"
            "x-ms-date:Wed, 01 Jan 2026 00:00:00 GMT\n"
            "x-ms-version:2023-11-03\n"
            "/acct/c\n"
            "comp:list,metadata"
        )
        expected_b64 = base64.b64encode(
            hmac.new(
                b"k" * 32, sts.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode("ascii")
        assert sig == f"SharedKey acct:{expected_b64}"

    def test_falls_back_to_content_length_header_when_arg_omitted(self) -> None:
        # When the caller passes only headers (no explicit content_length),
        # the signer must read `content-length` from the headers map.
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        credential = SharedKeyCredential(
            account_name="acct", account_key_b64=key_b64
        )
        url = httpx.URL("https://acct.blob.core.windows.net/c/blob")
        sig_explicit = sign_shared_key(
            method="PUT",
            url=url,
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
                "Content-Length": "12",
            },
            credential=credential,
            content_length=12,
        )
        sig_implicit = sign_shared_key(
            method="PUT",
            url=url,
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
                "Content-Length": "12",
            },
            credential=credential,
        )
        assert sig_explicit == sig_implicit

    def test_invalid_content_length_header_treated_as_zero(self) -> None:
        # Defensive: malformed `Content-Length` (not an int) is treated
        # as 0 rather than raising — the signer must not crash on input
        # it cannot parse.
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        credential = SharedKeyCredential(
            account_name="acct", account_key_b64=key_b64
        )
        url = httpx.URL("https://acct.blob.core.windows.net/c")
        sig_bad = sign_shared_key(
            method="GET",
            url=url,
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
                "Content-Length": "garbage",
            },
            credential=credential,
        )
        sig_zero = sign_shared_key(
            method="GET",
            url=url,
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
            },
            credential=credential,
            content_length=0,
        )
        assert sig_bad == sig_zero

    def test_date_header_used_when_no_x_ms_date(self) -> None:
        # When `x-ms-date` is absent, the legacy `Date` header is what
        # the StringToSign references in slot 7.
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        credential = SharedKeyCredential(
            account_name="acct", account_key_b64=key_b64
        )
        url = httpx.URL("https://acct.blob.core.windows.net/c")
        sig = sign_shared_key(
            method="GET",
            url=url,
            headers={
                "Date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
            },
            credential=credential,
            content_length=0,
        )
        sts = (
            "GET\n\n\n\n\n\n"
            "Wed, 01 Jan 2026 00:00:00 GMT\n"
            "\n\n\n\n\n"
            "x-ms-version:2023-11-03\n"
            "/acct/c"
        )
        expected_b64 = base64.b64encode(
            hmac.new(
                b"k" * 32, sts.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode("ascii")
        assert sig == f"SharedKey acct:{expected_b64}"

    def test_path_without_leading_slash_normalized(self) -> None:
        # `httpx.URL` normally always begins paths with `/`, but a
        # constructed URL could lack it. The canonical resource must
        # still produce `/<account>/<path>`.
        key_b64 = base64.b64encode(b"k" * 32).decode("ascii")
        credential = SharedKeyCredential(
            account_name="acct", account_key_b64=key_b64
        )

        class _FakeURL:
            path = "container/blob"
            query = b""

        sig = sign_shared_key(
            method="GET",
            url=_FakeURL(),  # type: ignore[arg-type]
            headers={
                "x-ms-date": "Wed, 01 Jan 2026 00:00:00 GMT",
                "x-ms-version": "2023-11-03",
            },
            credential=credential,
            content_length=0,
        )
        sts = (
            "GET\n\n\n\n\n\n\n\n\n\n\n\n"
            "x-ms-date:Wed, 01 Jan 2026 00:00:00 GMT\n"
            "x-ms-version:2023-11-03\n"
            "/acct/container/blob"
        )
        expected_b64 = base64.b64encode(
            hmac.new(
                b"k" * 32, sts.encode("utf-8"), hashlib.sha256
            ).digest()
        ).decode("ascii")
        assert sig == f"SharedKey acct:{expected_b64}"
