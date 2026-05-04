"""GitHub App auth: JWT minting + installation-token cache.

GitHub App auth is a two-step dance:

1. The App owner has a private RSA key. We sign a short-lived JWT
   (`iss=app_id`, `iat`/`exp`, RS256) and present it as a Bearer to
   ``POST /app/installations/{installation_id}/access_tokens``.
2. That endpoint returns an *installation token* (1h TTL, looks like
   ``ghs_…``) which is the bearer for every actual repo / org / search
   call.

We do step 1 at most once per `_token_skew_seconds` before the cached
installation token expires, then keep using the installation token for
the rest of its hour. Tokens never touch disk — they live in a single
in-process dict guarded by an asyncio.Lock.

We sign with `cryptography.hazmat.primitives.asymmetric.padding.PKCS1v15`
+ SHA256 directly rather than pulling in PyJWT. The ~30 lines saved by
PyJWT are not worth a transitive dependency, and `cryptography` is
already a core dep (FindingsStore envelope encryption, ADR §11).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pleno_pii_scanner_github.api import GithubApi


# JWT lifetime. GitHub rejects JWTs with `exp - iat > 600`; we use 9
# minutes to leave a comfortable margin against client clock skew.
_JWT_TTL_SECONDS = 9 * 60

# How early to refresh an installation token before its `expires_at`.
# 5 minutes is enough to cover scheduler back-pressure + a clock-skew
# token-rejection retry without risking mid-request expiry.
_TOKEN_SKEW_SECONDS = 5 * 60


def _b64url(data: bytes) -> str:
    """Standard JWT base64url-without-padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_app_jwt(
    *,
    app_id: str | int,
    private_key_pem: str | bytes,
    now: float | None = None,
    ttl_seconds: int = _JWT_TTL_SECONDS,
) -> str:
    """Sign and return an RS256 JWT for the named App.

    Pure function; no I/O, no globals — fully deterministic given
    `now`. Tests pin `now` to assert byte-for-byte JWT equality.
    """
    issued = int(now if now is not None else time.time())
    # `iat -= 30` defends against client clock that runs *ahead* of
    # GitHub's: a JWT with future iat is rejected as `iat is in the
    # future`. The 30-second grace is what GitHub's docs recommend.
    payload = {
        "iat": issued - 30,
        "exp": issued + ttl_seconds,
        "iss": str(app_id),
    }
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    pem_bytes = (
        private_key_pem.encode("utf-8")
        if isinstance(private_key_pem, str)
        else private_key_pem
    )
    key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(
            "GitHub App private key must be RSA; got "
            f"{type(key).__name__}"
        )
    signature = key.sign(
        signing_input.encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    return signing_input + "." + _b64url(signature)


@dataclass(frozen=True, slots=True)
class InstallationToken:
    """Cached installation token + its absolute expiry timestamp.

    `expires_at` is a unix timestamp (seconds). We compare it against
    `time.time() + _TOKEN_SKEW_SECONDS` so refresh fires early enough
    that the next request never carries an almost-expired token.
    """

    token: str
    expires_at: float


# Test seam: lets tests inject a clock without monkey-patching `time`.
_NowFn = Callable[[], float]


@dataclass(slots=True)
class AppAuth:
    """Mints + caches installation tokens for one (app_id, installation).

    The cache is per-AppAuth instance, scoped to one connector. A scan
    that targets two different App installations creates two AppAuth
    instances; each owns its own asyncio.Lock so refresh cannot
    serialize across tenants.
    """

    app_id: str
    installation_id: str
    private_key_pem: str
    api: GithubApi
    skew_seconds: int = _TOKEN_SKEW_SECONDS
    now: _NowFn = field(default=time.time)
    _cached: InstallationToken | None = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def get_installation_token(self) -> str:
        """Return a fresh-or-still-valid installation token.

        Holds an asyncio.Lock so that N concurrent fetches racing on
        token expiry only mint one new JWT and only call the App
        installation endpoint once.
        """
        cached = self._cached
        if cached is not None and cached.expires_at - self.skew_seconds > self.now():
            return cached.token
        async with self._lock:
            cached = self._cached
            if cached is not None and cached.expires_at - self.skew_seconds > self.now():
                return cached.token
            fresh = await self._mint_installation_token()
            self._cached = fresh
            return fresh.token

    async def _mint_installation_token(self) -> InstallationToken:
        jwt = mint_app_jwt(
            app_id=self.app_id,
            private_key_pem=self.private_key_pem,
            now=self.now(),
        )
        # `token=jwt` overrides the api's default bearer for this single
        # call; the `Bearer <jwt>` is required only for the
        # access_tokens endpoint.
        response = await self.api.post(
            f"/app/installations/{self.installation_id}/access_tokens",
            token=jwt,
        )
        if response.status_code not in (200, 201):
            raise PermissionError(
                f"github App access_tokens exchange failed: "
                f"{response.status_code} {response.text[:200]}"
            )
        payload = response.json()
        token = payload["token"]
        # `expires_at` is ISO 8601 — parse to unix timestamp. We accept
        # both the documented `2024-01-01T00:00:00Z` form and a numeric
        # `expires_in` fallback (GHES <3.10 returned the latter).
        expires_at = _parse_expiry(payload, now=self.now())
        return InstallationToken(token=token, expires_at=expires_at)

    def install_token(self, token: str, expires_at: float) -> None:
        """Test-only: pre-seed the cache."""
        self._cached = InstallationToken(token=token, expires_at=expires_at)


def _parse_expiry(payload: dict[str, object], *, now: float) -> float:
    """Convert App access-token response expiry into a unix timestamp."""
    # Prefer the absolute `expires_at` (ISO 8601). Fall back to the
    # relative `expires_in` (seconds) which GHES <3.10 still emits.
    raw = payload.get("expires_at")
    if isinstance(raw, str):
        # GitHub uses `Z` suffix; datetime.fromisoformat in 3.11+ handles
        # it natively but we keep the fallback for paranoia.
        from datetime import datetime
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise PermissionError(
                f"could not parse access-token expires_at={raw!r}: {exc}"
            ) from exc
    relative = payload.get("expires_in")
    if isinstance(relative, (int, float)):
        return float(now) + float(relative)
    # If neither field is present treat as 1h, the documented default.
    return float(now) + 3600.0


# Exported as the type tests assert against; keeps the module's surface
# explicit without exposing the cache dataclass fields.
__all__ = [
    "AppAuth",
    "InstallationToken",
    "_parse_expiry",  # tested directly to lock the GHES fallback
    "mint_app_jwt",
]
# `Awaitable` re-export keeps mypy happy when AppAuth callers ascribe
# its return type explicitly.
_AwaitableStr = Awaitable[str]
