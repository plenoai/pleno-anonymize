"""Shared httpx.AsyncClient builder for provider tests + production.

Tests inject an httpx.MockTransport via VerifyContext.extra["transport"]
so providers can be exercised without touching the network. Production
callers leave it unset and get a real default transport.
"""

from __future__ import annotations

import ssl
from typing import cast

import httpx

from ..base import VerifyContext


def build_client(ctx: VerifyContext) -> httpx.AsyncClient:
    """Build an AsyncClient honouring ctx (timeout / proxy / CA / mock)."""
    transport = ctx.extra.get("transport") if ctx.extra else None
    kwargs: dict[str, object] = {"timeout": ctx.timeout_seconds}
    if transport is not None:
        kwargs["transport"] = cast(httpx.AsyncBaseTransport, transport)
    if ctx.proxy_url is not None:
        kwargs["proxy"] = ctx.proxy_url
    if ctx.ca_bundle is not None:
        # ssl.create_default_context loads the CA at first request,
        # not at SSLContext construction, so a missing/empty file does
        # not blow up the client builder. The previous str-form path
        # also emits a DeprecationWarning in httpx 0.28+.
        kwargs["verify"] = ssl.create_default_context(cafile=str(ctx.ca_bundle))
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]
