"""Thin httpx wrapper for the Azure DevOps REST endpoints we use.

We hit exactly two endpoint families:

  * ``GET _apis/projects?api-version=7.1`` — paginated by an opaque
    *continuation token returned in the **header*** ``x-ms-continuationtoken``,
    not in the response body. This is the single most-confused part of
    the Azure DevOps API; a generic JSON-body paginator silently
    truncates at page 1.
  * ``GET {project}/_apis/git/repositories?api-version=7.1`` — single
    page (Azure caps it at the org-wide repo limit which is far
    smaller than the project pagination ceiling).

Rate limiting — Azure DevOps signals throttle with 429 + ``Retry-After``
(seconds) per
https://learn.microsoft.com/en-us/azure/devops/integrate/concepts/rate-limits.
We surface this to the scheduler's AIMD bucket through `RateLimited`
the same way the GitHub wrapper does.

CA bundles — for self-hosted Server installs operators ship a private
CA. We accept the bundle path through the constructor and forward it
to httpx's `verify=` parameter (a string path activates
SSLContext.load_verify_locations under the hood).
"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from pleno_pii_scanner.scheduler.rate_limit import RateLimited

from pleno_pii_scanner_azure_devops.auth import AzureDevOpsAuth


# Azure DevOps REST API version. 7.1 is GA on Services and Server 2022+;
# older Server (TFS 2018) tops out at 5.0 but the project / repo
# enumeration shapes are byte-compatible.
DEFAULT_API_VERSION = "7.1"

# Default base URL for Azure DevOps Services. Server installs replace
# this with `https://<host>/<collection>` (no `_apis` suffix — that's
# applied per request).
SERVICES_DEFAULT_HOST = "https://dev.azure.com"

# Continuation-token header name. Azure DevOps lowercases response
# headers but httpx normalises to title-case on read; we lowercase on
# lookup so both compare cleanly.
CONTINUATION_TOKEN_HEADER = "x-ms-continuationtoken"

_USER_AGENT = "pleno-pii-scanner-azure-devops"


class AzureDevOpsApiError(Exception):
    """Non-retryable upstream failure from Azure DevOps.

    Distinct from `RateLimited` (which is retryable) so the scheduler
    can treat it as a permanent failure for this ref.
    """


class AzureDevOpsApi:
    """Owns one httpx client + one AzureDevOpsAuth for the connector.

    The client is built lazily on first use so a connector that is
    constructed but never `.discover()`d (validation-only path) does
    not allocate sockets. `aclose()` is idempotent.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth: AzureDevOpsAuth,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        ca_bundle_path: Path | None = None,
        api_version: str = DEFAULT_API_VERSION,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._transport = transport
        self._timeout = timeout
        self._ca_bundle_path = ca_bundle_path
        self._api_version = api_version
        self._client: httpx.AsyncClient | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def api_version(self) -> str:
        return self._api_version

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._transport is not None:
            # Mock transport in tests bypasses the verify path entirely;
            # we still accept ca_bundle_path for symmetry but it is a
            # no-op when transport is supplied.
            kwargs["transport"] = self._transport
        elif self._ca_bundle_path is not None:
            # Build the SSLContext ourselves so a missing bundle file
            # surfaces immediately as FileNotFoundError instead of as a
            # cryptic httpx ConnectError on first request.
            ctx = ssl.create_default_context(
                cafile=str(self._ca_bundle_path)
            )
            kwargs["verify"] = ctx
        self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        await self._auth.aclose()

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """REST GET with Authorization + api-version stamped in.

        Honors `Retry-After` on 429 by raising `RateLimited`; the
        scheduler's retry decorator turns that into an actual sleep.
        """
        client = self._ensure_client()
        merged_params: dict[str, Any] = {"api-version": self._api_version}
        if params is not None:
            merged_params.update(params)
        url = path if path.startswith("http") else f"{self._base_url}{path}"
        headers = await self._headers()
        response = await client.get(url, params=merged_params, headers=headers)
        _raise_for_rate_limit(response)
        return response

    async def get_paginated(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Iterate pages, threading `x-ms-continuationtoken` through queries.

        Yields `httpx.Response` per page (the caller decodes JSON
        because some endpoints embed paginated lists under different
        top-level keys — projects vs repositories vs commits).
        """
        token: str | None = None
        while True:
            page_params: dict[str, Any] = {}
            if params is not None:
                page_params.update(params)
            if token is not None:
                # Azure DevOps accepts the continuation token as a
                # query-string param (`continuationToken=...`). The
                # documentation also shows it as a header on the
                # *response*; only the response side carries it.
                page_params["continuationToken"] = token
            response = await self.get(path, params=page_params)
            yield response
            # Header keys come back lowercased on the wire but httpx
            # exposes them via case-insensitive dict; explicit lower
            # avoids any cross-version surprises.
            next_token: str | None = None
            for key, value in response.headers.items():
                if key.lower() == CONTINUATION_TOKEN_HEADER:
                    next_token = value
                    break
            if not next_token:
                return
            token = next_token

    async def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
            "Authorization": await self._auth.authorization_header(),
        }


def _raise_for_rate_limit(response: httpx.Response) -> None:
    """Convert Azure DevOps throttling signals to `RateLimited`.

    Azure DevOps returns ``429 Too Many Requests`` with a
    ``Retry-After`` header (seconds) when the per-org / per-token TSTUs
    bucket is empty. There is no documented secondary 403 form (unlike
    GitHub abuse-detect), so we only handle 429.
    """
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        raise RateLimited(
            f"azure-devops 429; retry_after={retry_after!r}"
        )


__all__ = [
    "CONTINUATION_TOKEN_HEADER",
    "DEFAULT_API_VERSION",
    "SERVICES_DEFAULT_HOST",
    "AzureDevOpsApi",
    "AzureDevOpsApiError",
]
