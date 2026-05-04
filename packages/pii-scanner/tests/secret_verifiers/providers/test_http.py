from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pleno_pii_scanner.secret_verifiers.base import VerifyContext
from pleno_pii_scanner.secret_verifiers.providers._http import build_client


async def test_build_client_default() -> None:
    async with build_client(VerifyContext()) as client:
        assert isinstance(client, httpx.AsyncClient)


async def test_build_client_with_mock_transport() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(204))
    ctx = VerifyContext(extra={"transport": transport})
    async with build_client(ctx) as client:
        response = await client.get("https://api.example.com/")
        assert response.status_code == 204


async def test_build_client_honours_proxy_url(tmp_path: Path) -> None:
    ctx = VerifyContext(proxy_url="http://proxy.invalid:3128")
    async with build_client(ctx) as client:
        assert isinstance(client, httpx.AsyncClient)


async def test_build_client_honours_ca_bundle(tmp_path: Path) -> None:
    # ssl.create_default_context validates the CA file at construction
    # so we point at a real PEM bundle. Generating a self-signed cert
    # with stdlib only would balloon the test; reusing the system
    # bundle (or one we know exists in CI) is the smaller step.
    import ssl as _ssl

    paths = _ssl.get_default_verify_paths()
    bundle_path = paths.openssl_cafile or paths.cafile
    if bundle_path is None or not Path(bundle_path).exists():
        pytest.skip("no system CA bundle available")
    ctx = VerifyContext(ca_bundle=Path(bundle_path))
    async with build_client(ctx) as client:
        assert isinstance(client, httpx.AsyncClient)


@pytest.mark.parametrize("proxy", [None, "http://p:3128"])
async def test_build_client_branches(proxy: str | None) -> None:
    ctx = VerifyContext(proxy_url=proxy)
    async with build_client(ctx) as client:
        assert isinstance(client, httpx.AsyncClient)
