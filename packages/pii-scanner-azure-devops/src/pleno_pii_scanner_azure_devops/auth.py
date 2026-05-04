"""Azure DevOps auth: PAT, OAuth bearer, and federated (OIDC) modes.

Three credential shapes are supported, picked by `mode`:

  * ``pat`` — Personal Access Token. Sent as HTTP Basic auth with an
    empty username (Azure DevOps requires the empty-user form; a
    populated username silently 401s on Services and breaks audit on
    Server).
  * ``oauth`` — pre-acquired OAuth 2 / Entra ID access token. Sent as a
    bearer header verbatim. Refresh is the caller's responsibility.
  * ``federated`` — OIDC workload identity. We read a JWT from
    `oidc_token_path` (refreshed by the platform; GHA and AKS rotate
    every ~10 min), POST it as the `assertion` parameter to the
    Microsoft Entra `/oauth2/v2.0/token` endpoint with
    ``grant_type=client_credentials`` +
    ``client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer``
    + the Azure DevOps resource scope
    ``499b84ac-1321-427f-aa17-267ca6975798/.default``, and cache the
    returned bearer until 5 minutes before ``expires_in``. The OIDC
    file is re-read on every exchange so platform rotation is picked
    up without process restart.

All three return ``(authorization_header_value, expires_at_or_None)``
through `AzureDevOpsAuth.authorization_header()`. The connector never
touches the raw secret material — it only calls `authorization_header()`
and stamps the result onto each request. This keeps the secret out of
log-formatted dataclasses and out of dict-style ``__repr__``.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx


# Azure DevOps OAuth resource ID (constant across tenants). Documented
# at https://learn.microsoft.com/en-us/azure/devops/integrate/get-started/authentication/service-principal-managed-identity
# The `/.default` suffix is the v2.0 endpoint convention requesting the
# union of consented scopes for the resource.
AZURE_DEVOPS_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
AZURE_DEVOPS_DEFAULT_SCOPE = f"{AZURE_DEVOPS_RESOURCE_ID}/.default"

# Microsoft Entra v2.0 token endpoint template. Tenant-scoped because
# the multi-tenant `/common/` endpoint cannot issue tokens for the
# Azure DevOps resource without an admin-consented common app.
_AAD_TOKEN_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)

# Re-mint the federated bearer 5 minutes before expiry so an in-flight
# request never carries a token that expires mid-flight. AAD issues
# 1h tokens by default; this skew is generous against scheduler jitter.
_TOKEN_SKEW_SECONDS = 5 * 60

# JWT-bearer client-assertion grant. RFC 7521 / 7523. AAD documents
# this string verbatim; any deviation 400s with an unhelpful message.
_CLIENT_ASSERTION_TYPE = (
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
)


AuthMode = Literal["pat", "oauth", "federated"]


class FederatedTokenError(RuntimeError):
    """AAD token exchange failed (network error or non-2xx response).

    Surfaced so the scheduler can distinguish auth misconfig from a
    transient upstream 5xx — only the latter should retry.
    """


@dataclass(frozen=True, slots=True)
class FederatedConfig:
    """Inputs for the OIDC → AAD bearer exchange.

    `oidc_token_path` is read on every refresh: the platform (GHA,
    AKS, Cloud Run) rotates the file. `tenant_id` and `client_id` are
    static per workload identity binding.
    """

    oidc_token_path: Path
    tenant_id: str
    client_id: str
    scope: str = AZURE_DEVOPS_DEFAULT_SCOPE

    def token_url(self) -> str:
        return _AAD_TOKEN_URL_TEMPLATE.format(tenant=self.tenant_id)


@dataclass(frozen=True, slots=True)
class _CachedBearer:
    """One AAD-exchanged bearer + its absolute expiry timestamp."""

    token: str
    expires_at: float


@dataclass(slots=True)
class AzureDevOpsAuth:
    """Materialises an `Authorization` header for one credential.

    Construct via the `pat()`, `oauth()`, or `federated()` classmethods
    so the mode is a closed enum at the type level. The connector calls
    `authorization_header()` per request — for PAT/OAuth this is a
    constant; for federated it lazy-mints (and caches) a bearer.
    """

    mode: AuthMode
    _pat: str | None = None
    _bearer: str | None = None
    _federated: FederatedConfig | None = None
    _aad_client: httpx.AsyncClient | None = None
    _now: "callable[[], float]" = field(default=time.time)
    _cached: _CachedBearer | None = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    # `_owned_aad_client` distinguishes the lazy-created client (we own
    # it and aclose() must close it) from a caller-supplied one (the
    # caller's responsibility). Pre-declared here so slots=True allows
    # assignment in `_ensure_aad_client`.
    _owned_aad_client: bool = field(default=False, init=False)

    # ----- factories ------------------------------------------------

    @classmethod
    def pat(cls, pat: str) -> "AzureDevOpsAuth":
        if not pat:
            # An empty PAT would still build a valid Basic header
            # (`Basic Og==`) which Azure DevOps accepts as anonymous —
            # we fail loudly instead of letting the scan return zero
            # repos with no diagnostic.
            raise ValueError("pat must be non-empty")
        return cls(mode="pat", _pat=pat)

    @classmethod
    def oauth(cls, access_token: str) -> "AzureDevOpsAuth":
        if not access_token:
            raise ValueError("access_token must be non-empty")
        return cls(mode="oauth", _bearer=access_token)

    @classmethod
    def federated(
        cls,
        config: FederatedConfig,
        *,
        aad_client: httpx.AsyncClient | None = None,
        now: "callable[[], float] | None" = None,
    ) -> "AzureDevOpsAuth":
        return cls(
            mode="federated",
            _federated=config,
            _aad_client=aad_client,
            _now=now or time.time,
        )

    # ----- API ------------------------------------------------------

    async def authorization_header(self) -> str:
        """Return the raw `Authorization` header value for this credential.

        Format:

          * pat       -> ``Basic <b64(:pat)>``
          * oauth     -> ``Bearer <access_token>``
          * federated -> ``Bearer <aad-exchanged token>``  (lazy + cached)
        """
        if self.mode == "pat":
            assert self._pat is not None
            encoded = base64.b64encode(f":{self._pat}".encode("ascii")).decode(
                "ascii"
            )
            return f"Basic {encoded}"
        if self.mode == "oauth":
            assert self._bearer is not None
            return f"Bearer {self._bearer}"
        # federated
        token = await self._get_federated_bearer()
        return f"Bearer {token}"

    async def aclose(self) -> None:
        """Release the AAD HTTP client if we own one.

        We only own the client when the caller didn't pass one; that
        case is detected by looking at `_owned_aad_client` which is set
        in `_ensure_aad_client`.
        """
        if self._aad_client is not None and self._owned_aad_client:
            await self._aad_client.aclose()

    # ----- internals ------------------------------------------------

    async def _get_federated_bearer(self) -> str:
        cached = self._cached
        if cached is not None and cached.expires_at - _TOKEN_SKEW_SECONDS > self._now():
            return cached.token
        async with self._lock:
            cached = self._cached
            # Re-check inside the lock so two coroutines racing on a
            # cold cache do not both POST to AAD.
            if (
                cached is not None
                and cached.expires_at - _TOKEN_SKEW_SECONDS > self._now()
            ):
                return cached.token
            fresh = await self._exchange_federated()
            self._cached = fresh
            return fresh.token

    async def _exchange_federated(self) -> _CachedBearer:
        config = self._federated
        assert config is not None  # constructed via .federated()
        try:
            assertion = config.oidc_token_path.read_text().strip()
        except OSError as exc:
            raise FederatedTokenError(
                f"could not read OIDC token at {config.oidc_token_path!r}: {exc}"
            ) from exc
        if not assertion:
            raise FederatedTokenError(
                f"OIDC token file {config.oidc_token_path!r} is empty"
            )
        client = self._ensure_aad_client()
        # AAD requires `application/x-www-form-urlencoded`; httpx encodes
        # `data=dict(...)` that way automatically. Field order is
        # immaterial per RFC 6749.
        try:
            response = await client.post(
                config.token_url(),
                data={
                    "client_id": config.client_id,
                    "scope": config.scope,
                    "client_assertion_type": _CLIENT_ASSERTION_TYPE,
                    "client_assertion": assertion,
                    "grant_type": "client_credentials",
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise FederatedTokenError(
                f"AAD token exchange transport error: {exc}"
            ) from exc
        if response.status_code != 200:
            # Strip body to avoid logging whatever PII / hint AAD may
            # echo. The status code alone is enough for triage.
            raise FederatedTokenError(
                f"AAD token exchange failed: {response.status_code}"
            )
        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise FederatedTokenError(
                "AAD response missing 'access_token'"
            )
        # `expires_in` is seconds-from-now per RFC 6749 §5.1. AAD always
        # emits it; default to 1h if it is somehow missing so the cache
        # is never poisoned with a never-expiring token.
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at = self._now() + float(expires_in)
        else:
            expires_at = self._now() + 3600.0
        return _CachedBearer(token=token, expires_at=expires_at)

    def _ensure_aad_client(self) -> httpx.AsyncClient:
        if self._aad_client is not None:
            return self._aad_client
        # We own this client and must close it in `aclose()`.
        self._aad_client = httpx.AsyncClient(timeout=30.0)
        self._owned_aad_client = True
        return self._aad_client


__all__ = [
    "AZURE_DEVOPS_DEFAULT_SCOPE",
    "AZURE_DEVOPS_RESOURCE_ID",
    "AuthMode",
    "AzureDevOpsAuth",
    "FederatedConfig",
    "FederatedTokenError",
]
