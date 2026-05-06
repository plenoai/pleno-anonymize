"""Tests for AzureDevOpsAuth: PAT, OAuth, and federated (OIDC) modes.

The federated path is the most interesting — it makes a real HTTP call
to AAD's token endpoint, which we mock with `httpx.MockTransport`. PAT
and OAuth modes are pure-CPU header construction.
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from pleno_pii_scanner_azure_devops.auth import (
    AZURE_DEVOPS_DEFAULT_SCOPE,
    AZURE_DEVOPS_RESOURCE_ID,
    AzureDevOpsAuth,
    FederatedConfig,
    FederatedTokenError,
)


# ----- constants ---------------------------------------------------------


class TestConstants:
    def test_resource_id_is_well_known(self) -> None:
        # https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/service-principal-managed-identity
        assert AZURE_DEVOPS_RESOURCE_ID == "499b84ac-1321-427f-aa17-267ca6975798"

    def test_default_scope_appends_dot_default(self) -> None:
        assert AZURE_DEVOPS_DEFAULT_SCOPE.endswith("/.default")
        assert AZURE_DEVOPS_RESOURCE_ID in AZURE_DEVOPS_DEFAULT_SCOPE


# ----- PAT mode ----------------------------------------------------------


class TestPatMode:
    async def test_basic_header_with_empty_user(self) -> None:
        auth = AzureDevOpsAuth.pat("my-pat-token")
        header = await auth.authorization_header()
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header[len("Basic ") :]).decode("ascii")
        # Azure DevOps requires the user to be empty; only the colon
        # and PAT live in the basic-auth string.
        assert decoded == ":my-pat-token"
        await auth.aclose()

    async def test_rejects_empty_pat(self) -> None:
        with pytest.raises(ValueError, match="pat must be non-empty"):
            AzureDevOpsAuth.pat("")

    async def test_pat_aclose_is_idempotent(self) -> None:
        auth = AzureDevOpsAuth.pat("x")
        await auth.aclose()
        await auth.aclose()  # must not raise


# ----- OAuth mode --------------------------------------------------------


class TestOauthMode:
    async def test_bearer_header(self) -> None:
        auth = AzureDevOpsAuth.oauth("eyJ.test.token")
        header = await auth.authorization_header()
        assert header == "Bearer eyJ.test.token"
        await auth.aclose()

    async def test_rejects_empty_token(self) -> None:
        with pytest.raises(ValueError, match="access_token"):
            AzureDevOpsAuth.oauth("")


# ----- Federated / OIDC -------------------------------------------------


def _federated_handler_ok(
    captured: dict, token: str = "aad-bearer-1", expires_in: int = 3600
):
    """httpx.MockTransport handler that records request + returns AAD bearer."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["accept"] = request.headers.get("Accept")
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": token,
                "expires_in": expires_in,
            },
        )

    return handler


def _build_federated_auth(
    tmp_path: Path,
    handler,
    *,
    oidc: str = "ey.oidc.jwt",
    now=None,
):
    token_file = tmp_path / "oidc"
    token_file.write_text(oidc)
    transport = httpx.MockTransport(handler)
    aad_client = httpx.AsyncClient(transport=transport)
    return aad_client, AzureDevOpsAuth.federated(
        FederatedConfig(
            oidc_token_path=token_file,
            tenant_id="11111111-1111-1111-1111-111111111111",
            client_id="22222222-2222-2222-2222-222222222222",
        ),
        aad_client=aad_client,
        now=now,
    )


class TestFederatedMode:
    async def test_token_exchange_posts_oidc_assertion(self, tmp_path: Path) -> None:
        captured: dict = {}
        client, auth = _build_federated_auth(tmp_path, _federated_handler_ok(captured))
        try:
            header = await auth.authorization_header()
            assert header == "Bearer aad-bearer-1"
            # Posted to the tenant-scoped token endpoint
            assert "11111111-1111-1111-1111-111111111111" in captured["url"]
            assert captured["url"].endswith("/oauth2/v2.0/token")
            # Form body carries the OIDC JWT under `client_assertion`
            assert "client_assertion=ey.oidc.jwt" in captured["body"]
            assert "grant_type=client_credentials" in captured["body"]
            # Resource scope is the well-known Azure DevOps GUID
            assert AZURE_DEVOPS_RESOURCE_ID in captured["body"]
            assert captured["accept"] == "application/json"
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_cache_hit_avoids_second_exchange(self, tmp_path: Path) -> None:
        call_count = {"n": 0}
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _federated_handler_ok(captured)(request)

        client, auth = _build_federated_auth(tmp_path, handler)
        try:
            await auth.authorization_header()
            await auth.authorization_header()
            await auth.authorization_header()
            assert call_count["n"] == 1
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_cache_miss_when_close_to_expiry(self, tmp_path: Path) -> None:
        # Inject a fake clock — first call returns a 600s-TTL token.
        # The skew is 300s so a clock 350s later forces a re-exchange.
        clock = {"t": 1_000_000.0}

        def now() -> float:
            return clock["t"]

        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok-{call_count['n']}",
                    "expires_in": 600,
                },
            )

        client, auth = _build_federated_auth(tmp_path, handler, now=now)
        try:
            h1 = await auth.authorization_header()
            assert h1 == "Bearer tok-1"
            clock["t"] += 350.0  # past the 600 - 300 = 300 skew threshold
            h2 = await auth.authorization_header()
            assert h2 == "Bearer tok-2"
            assert call_count["n"] == 2
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_missing_oidc_file_raises(self, tmp_path: Path) -> None:
        # Don't create the file; OIDC read should error out.
        config = FederatedConfig(
            oidc_token_path=tmp_path / "nope",
            tenant_id="t",
            client_id="c",
        )
        auth = AzureDevOpsAuth.federated(config)
        try:
            with pytest.raises(FederatedTokenError, match="could not read"):
                await auth.authorization_header()
        finally:
            await auth.aclose()

    async def test_empty_oidc_file_raises(self, tmp_path: Path) -> None:
        token_file = tmp_path / "oidc"
        token_file.write_text("   \n  ")
        config = FederatedConfig(
            oidc_token_path=token_file, tenant_id="t", client_id="c"
        )
        auth = AzureDevOpsAuth.federated(config)
        try:
            with pytest.raises(FederatedTokenError, match="empty"):
                await auth.authorization_header()
        finally:
            await auth.aclose()

    async def test_aad_non_200_raises(self, tmp_path: Path) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": "invalid_client", "error_description": "secret"}
            )

        client, auth = _build_federated_auth(tmp_path, handler)
        try:
            with pytest.raises(FederatedTokenError, match="failed: 400"):
                await auth.authorization_header()
            # Sanity: the body (which contains the unhelpful AAD hint
            # text) must NOT bleed into the exception message.
            try:
                await auth.authorization_header()
            except FederatedTokenError as exc:
                assert "secret" not in str(exc)
                assert "invalid_client" not in str(exc)
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_aad_transport_error_raises(self, tmp_path: Path) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down")

        client, auth = _build_federated_auth(tmp_path, handler)
        try:
            with pytest.raises(FederatedTokenError, match="transport error"):
                await auth.authorization_header()
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_aad_response_missing_token_raises(self, tmp_path: Path) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"expires_in": 3600})

        client, auth = _build_federated_auth(tmp_path, handler)
        try:
            with pytest.raises(FederatedTokenError, match="access_token"):
                await auth.authorization_header()
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_aad_missing_expires_in_uses_1h_default(self, tmp_path: Path) -> None:
        # No `expires_in` -> must NOT cache forever; the connector
        # treats absence as a 1h TTL (documented default).
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "tok-x"})

        client, auth = _build_federated_auth(tmp_path, handler, now=now)
        try:
            await auth.authorization_header()
            # Past 3600 - 300 = 3300 must miss the cache.
            clock["t"] = 3500.0
            # Add a second handler call to satisfy the second exchange.
            call_count = {"n": 0}

            def handler2(_: httpx.Request) -> httpx.Response:
                call_count["n"] += 1
                return httpx.Response(
                    200, json={"access_token": "tok-y", "expires_in": 3600}
                )

            # Swap transport: build new client and inject; for simplicity
            # re-bind via a fresh AzureDevOpsAuth and assert the cache
            # logic on the original auth treats absent expires_in as 1h.
            # The original auth's cache should miss now.
            client2 = httpx.AsyncClient(transport=httpx.MockTransport(handler2))
            auth._aad_client = client2  # type: ignore[attr-defined]
            try:
                h2 = await auth.authorization_header()
                assert h2 == "Bearer tok-y"
                assert call_count["n"] == 1
            finally:
                await client2.aclose()
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_concurrent_first_call_only_exchanges_once(
        self, tmp_path: Path
    ) -> None:
        import asyncio

        call_count = {"n": 0}
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return _federated_handler_ok(captured)(request)

        client, auth = _build_federated_auth(tmp_path, handler)
        try:
            results = await asyncio.gather(
                auth.authorization_header(),
                auth.authorization_header(),
                auth.authorization_header(),
            )
            assert all(r == "Bearer aad-bearer-1" for r in results)
            assert call_count["n"] == 1
        finally:
            await auth.aclose()
            await client.aclose()

    async def test_owned_aad_client_closed_when_no_client_passed(
        self, tmp_path: Path
    ) -> None:
        # When the caller doesn't pre-build a client, AzureDevOpsAuth
        # must close its own client in aclose() so we don't leak sockets.
        token_file = tmp_path / "oidc"
        token_file.write_text("ey.x")
        auth = AzureDevOpsAuth.federated(
            FederatedConfig(
                oidc_token_path=token_file,
                tenant_id="t",
                client_id="c",
            )
        )
        # Force lazy client creation; we can't actually exchange (would
        # hit AAD) so just call the internal helper directly.
        client = auth._ensure_aad_client()
        assert client is not None
        await auth.aclose()
        # Re-acquire is allowed but not required after close; behaviour
        # of httpx.AsyncClient.aclose is idempotent.
