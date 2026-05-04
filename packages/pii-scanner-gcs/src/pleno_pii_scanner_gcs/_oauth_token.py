"""OAuth2 token acquisition for GCP — service-account JWT, Workload
Identity Federation, and Application Default Credentials.

A single `TokenSource` interface with three concrete implementations.
Each returns an opaque `AccessToken(value, expires_at)` and is wired
behind a small `TokenCache` that hands out the same token until 30 s
before its declared expiry. Why 30 s: GCP `objects.list` paginators can
burst for ~10 s during a busy scan; refreshing 30 s before expiry gives
us comfortable headroom so a paginate cannot straddle the boundary and
401 mid-walk.

Hermetic-test contract:
- Every network call is routed through an injectable `httpx.AsyncClient`.
  Tests pass an `httpx.MockTransport` client and never touch
  oauth2.googleapis.com or the GCE metadata server.
- The current wall-clock is read via a `now` callable, not
  `datetime.now()` directly, so cache hit/miss/refresh can be exercised
  without `freezegun` / `time.sleep`.
- RS256 signing uses `cryptography` so a test fixture can mint its own
  PEM and verify the resulting JWT structurally (header/payload/sig)
  without standing up a real Google STS endpoint.

Why we don't pull in `google-auth`:
- google-auth bundles a full HTTP layer (httplib2, requests) and a
  credentials lifecycle that fights with our scheduler's. The handful
  of OAuth flows we need are <200 LOC and worth owning so the test
  surface stays a single httpx mock transport.
- The wheel matrix in ADR §13 is explicitly small per connector so
  enterprise security teams can audit each independently.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

# WHY this URL: Google's documented OAuth2 token endpoint. Hard-coded
# because there is no per-tenant override — even Workload Identity
# Federation funnels through `sts.googleapis.com` then this same
# endpoint for the impersonation step.
_GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
# Application Default Credentials on GCE / GKE / Cloud Run reach the
# metadata server here; off-platform this hostname does not resolve.
_GCE_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/token"
)
# Default OAuth scope for read-only GCS + Cloud Asset Inventory access.
# Callers can override per `TokenSource` via `scopes=`.
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform.read-only",
)
# Refresh tokens this many seconds before the upstream `expires_at` so
# in-flight paginates do not 401 mid-walk. 30 s mirrors the AWS S3
# connector's safety margin.
_EXPIRY_SAFETY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class AccessToken:
    """Bearer token + absolute expiry timestamp.

    `expires_at` is a UTC `datetime` so cache eviction logic can compare
    against `now()` without juggling time zones. The `value` is the
    raw bearer string we set on the `Authorization: Bearer …` header.
    """

    value: str
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        """True when within `_EXPIRY_SAFETY_SECONDS` of expiry."""
        return now + timedelta(seconds=_EXPIRY_SAFETY_SECONDS) >= self.expires_at


class TokenSource(Protocol):
    """One-shot acquisition of a fresh `AccessToken`.

    Implementations are expected to round-trip exactly one HTTP exchange
    (or zero for static tokens). Caching belongs to `TokenCache` so a
    source can be swapped without losing the cache.
    """

    async def acquire(self, client: httpx.AsyncClient) -> AccessToken: ...


@dataclass(frozen=True, slots=True)
class ServiceAccountKeyTokenSource(TokenSource):
    """Mint a JWT signed with a service-account private key, exchange for
    a Google OAuth2 access token.

    The JSON key file format is the one Google emits from the IAM
    console: a JSON object containing `client_email`, `private_key`
    (PEM PKCS8 RSA), `private_key_id`, etc. We accept a parsed dict
    (not a path) so the caller decides how the secret is retrieved
    (SOPS-decrypted on-disk, HashiCorp Vault, Kubernetes Secret, …).
    Never log this dict — it contains the private key.
    """

    key_data: dict[str, Any]
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    audience: str = _GOOGLE_OAUTH_TOKEN_URL
    # Test seam: override `now`/`signer` so the resulting JWT can be
    # verified deterministically.
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC)
    )

    async def acquire(self, client: httpx.AsyncClient) -> AccessToken:
        assertion = _sign_service_account_jwt(
            self.key_data, self.scopes, self.audience, self.now()
        )
        resp = await client.post(
            _GOOGLE_OAUTH_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        return _parse_token_response(resp, self.now())


@dataclass(frozen=True, slots=True)
class WorkloadIdentityTokenSource(TokenSource):
    """Workload Identity Federation: exchange an external OIDC token
    (GitHub Actions / EKS / Azure / arbitrary OIDC IdP) for a Google
    access token via STS, then optionally impersonate a service account.

    `audience` is the WIF provider audience (e.g.
    `//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/p/providers/q`).
    `token_path` points at a file the platform refreshes for us
    (GitHub Actions writes one when `id-token: write` permission is
    set; EKS IRSA exposes `AWS_WEB_IDENTITY_TOKEN_FILE` analogue;
    arbitrary OIDC providers similarly).

    `service_account_email` triggers the impersonation step — STS
    returns a federated token, then we POST to
    `iamcredentials.googleapis.com:generateAccessToken` to assume the
    SA. When unset (rare in enterprise) we return the federated token
    directly, which only works against APIs that accept it.

    Why two steps: WIF federated tokens carry `principal://` identities
    that most Google APIs (including GCS) refuse. Impersonation is the
    documented pattern.
    """

    audience: str
    token_path: str
    service_account_email: str | None = None
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    # Test seams.
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC)
    )
    token_reader: Callable[[str], Awaitable[str]] | None = None

    async def acquire(self, client: httpx.AsyncClient) -> AccessToken:
        external = await self._read_external_token()
        # Step 1: STS exchange. The federated token is short-lived and
        # carries the WIF principal — not directly useful for GCS.
        sts_resp = await client.post(
            _GOOGLE_STS_TOKEN_URL,
            json={
                "audience": self.audience,
                "grantType": (
                    "urn:ietf:params:oauth:grant-type:token-exchange"
                ),
                "requestedTokenType": (
                    "urn:ietf:params:oauth:token-type:access_token"
                ),
                "scope": " ".join(self.scopes),
                "subjectTokenType": (
                    "urn:ietf:params:oauth:token-type:jwt"
                ),
                "subjectToken": external,
            },
        )
        sts_resp.raise_for_status()
        federated = sts_resp.json().get("access_token")
        if not federated:
            raise ValueError(
                "STS token exchange returned no access_token"
            )
        if self.service_account_email is None:
            # Operator opted out of impersonation; trust them. Expiry
            # comes from STS `expires_in`.
            ttl = int(sts_resp.json().get("expires_in", 3600))
            return AccessToken(
                value=federated,
                expires_at=self.now() + timedelta(seconds=ttl),
            )
        # Step 2: impersonate a service account.
        impersonate_url = (
            "https://iamcredentials.googleapis.com/v1/"
            f"projects/-/serviceAccounts/{self.service_account_email}"
            ":generateAccessToken"
        )
        impersonate_resp = await client.post(
            impersonate_url,
            headers={"Authorization": f"Bearer {federated}"},
            json={"scope": list(self.scopes)},
        )
        impersonate_resp.raise_for_status()
        body = impersonate_resp.json()
        token = body.get("accessToken")
        expire_time = body.get("expireTime")
        if not token or not expire_time:
            raise ValueError(
                "iamcredentials.generateAccessToken returned malformed body"
            )
        return AccessToken(
            value=token,
            expires_at=_parse_iso_utc(expire_time),
        )

    async def _read_external_token(self) -> str:
        if self.token_reader is not None:
            return await self.token_reader(self.token_path)
        # Default: read the file synchronously off the loop. The file
        # is small (<2 KiB JWT) so the off-loop hit is negligible.
        return await asyncio.to_thread(_read_text_file, self.token_path)


@dataclass(frozen=True, slots=True)
class ApplicationDefaultTokenSource(TokenSource):
    """Application Default Credentials.

    Two resolution paths in priority order:

    1. `GOOGLE_APPLICATION_CREDENTIALS` env var → JSON key file path →
       delegate to `ServiceAccountKeyTokenSource`. This is the dev /
       laptop path.
    2. GCE / GKE / Cloud Run metadata server → bearer token directly.
       This is the production path on Google infrastructure.

    `env_get` and `file_reader` are test seams so we can simulate both
    paths without writing to disk or standing up a metadata server.
    """

    scopes: tuple[str, ...] = DEFAULT_SCOPES
    env_get: Callable[[str], str | None] = field(
        default_factory=lambda: _default_env_get
    )
    file_reader: Callable[[str], str] = field(
        default_factory=lambda: _read_text_file
    )
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC)
    )

    async def acquire(self, client: httpx.AsyncClient) -> AccessToken:
        cred_path = self.env_get("GOOGLE_APPLICATION_CREDENTIALS")
        if cred_path:
            raw = await asyncio.to_thread(self.file_reader, cred_path)
            key_data = json.loads(raw)
            sub = ServiceAccountKeyTokenSource(
                key_data=key_data, scopes=self.scopes, now=self.now
            )
            return await sub.acquire(client)
        # Metadata server path. The `Metadata-Flavor: Google` header is
        # required — without it the server 403s. `scopes=` query param
        # is honored on most metadata servers; we pass it for parity
        # with the SA path even though the metadata server may ignore
        # narrower scopes than the instance was provisioned with.
        resp = await client.get(
            _GCE_METADATA_TOKEN_URL,
            headers={"Metadata-Flavor": "Google"},
            params={"scopes": ",".join(self.scopes)},
        )
        resp.raise_for_status()
        body = resp.json()
        ttl = int(body.get("expires_in", 3600))
        token = body.get("access_token")
        if not token:
            raise ValueError(
                "GCE metadata server returned no access_token"
            )
        return AccessToken(
            value=token,
            expires_at=self.now() + timedelta(seconds=ttl),
        )


@dataclass(slots=True)
class TokenCache:
    """Hold a single token; refresh when it is within the safety
    margin of expiry. One in-flight refresh shared across coroutines
    via `_lock`.

    Mutable on purpose — the cached token rotates in place so external
    references (e.g. a long-lived `Authorization` header captured at
    construction) would be wrong; callers must always go through
    `get()` per request.
    """

    source: TokenSource
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC)
    )
    _cached: AccessToken | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self, client: httpx.AsyncClient) -> AccessToken:
        async with self._lock:
            if self._cached is not None and not self._cached.is_expired(
                self.now()
            ):
                return self._cached
            self._cached = await self.source.acquire(client)
            return self._cached

    def invalidate(self) -> None:
        """Force the next `get()` to refresh.

        Useful when the connector receives a 401 mid-scan and suspects
        the token was revoked before its declared expiry (Google does
        rotate on policy change).
        """
        self._cached = None


# --- helpers ---------------------------------------------------------


def _sign_service_account_jwt(
    key_data: dict[str, Any],
    scopes: tuple[str, ...],
    audience: str,
    now: datetime,
) -> str:
    """Build a signed RS256 JWT for the OAuth2 jwt-bearer grant.

    `key_data` is the parsed JSON service-account key file. We extract
    only the fields we need; logging/raising keeps `private_key` out
    of error messages — never include the dict in an error.
    """
    try:
        client_email = key_data["client_email"]
        private_key_pem = key_data["private_key"]
        key_id = key_data.get("private_key_id")
    except KeyError as exc:
        raise ValueError(
            f"service-account key missing required field: {exc.args[0]!r}"
        ) from None
    if not isinstance(private_key_pem, str):
        raise ValueError("service-account private_key must be PEM string")
    issued_at = int(now.timestamp())
    # 1 hour is the max Google accepts for `exp - iat`. Shorter lifetime
    # bounds blast radius if the JWT leaks in a network capture.
    payload = {
        "iss": client_email,
        "scope": " ".join(scopes),
        "aud": audience,
        "iat": issued_at,
        "exp": issued_at + 3600,
    }
    header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if key_id:
        header["kid"] = key_id
    signing_input = (
        _b64url_json(header) + b"." + _b64url_json(payload)
    )
    signature = _rs256_sign(private_key_pem, signing_input)
    return (signing_input + b"." + _b64url(signature)).decode("ascii")


def _rs256_sign(private_key_pem: str, data: bytes) -> bytes:
    """RS256 sign `data` with the PEM-encoded private key.

    Imported lazily so importing this module on a constrained env
    (offline test discovery) does not fail when `cryptography` is
    not yet installed during early bootstrap.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def _parse_token_response(
    resp: httpx.Response, now: datetime
) -> AccessToken:
    """Convert a Google OAuth2 token response into our AccessToken.

    Google returns `access_token` + `expires_in` (seconds). We compute
    the absolute expiry up front so the cache check is a single
    comparison rather than tracking issuance time separately.
    """
    if resp.status_code != 200:
        # Do NOT include `resp.text` — token-endpoint error bodies
        # sometimes echo back the assertion (signed JWT) on debug
        # responses. Keep the message structural only.
        raise httpx.HTTPStatusError(
            f"OAuth token endpoint returned status={resp.status_code}",
            request=resp.request,
            response=resp,
        )
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise ValueError(
            "OAuth token response missing access_token"
        )
    ttl = int(body.get("expires_in", 3600))
    return AccessToken(value=token, expires_at=now + timedelta(seconds=ttl))


def _b64url(data: bytes) -> bytes:
    """URL-safe base64 without trailing `=` padding (JWT spec)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _b64url_json(obj: Any) -> bytes:
    return _b64url(
        json.dumps(obj, separators=(",", ":")).encode("utf-8")
    )


def _parse_iso_utc(value: str) -> datetime:
    """Parse RFC3339 / ISO-8601 → UTC datetime.

    Google emits `expireTime` as `YYYY-MM-DDTHH:MM:SS[.fff]Z`.
    `fromisoformat` accepts the trailing `Z` only on 3.11+; we run on
    3.12 per `requires-python` so the swap is safe.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _default_env_get(name: str) -> str | None:  # pragma: no cover
    # Trivial production-default seam — every test injects a fake
    # `env_get` so this branch never runs under pytest. Documented
    # uncovered defensive shim.
    import os

    return os.environ.get(name)


def _read_text_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


__all__ = [
    "AccessToken",
    "ApplicationDefaultTokenSource",
    "DEFAULT_SCOPES",
    "ServiceAccountKeyTokenSource",
    "TokenCache",
    "TokenSource",
    "WorkloadIdentityTokenSource",
]
