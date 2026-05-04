"""Bearer-token auth negotiation for OCI registries.

The OCI Distribution Spec uses an opaque token-server flow: a request
to `/v2/<repo>/manifests/<ref>` returns 401 with `WWW-Authenticate:
Bearer realm="...",service="...",scope="..."`, and the client must
exchange a follow-up GET against the realm for a short-lived bearer
token. Each registry implements the realm differently:

  * **Docker Hub**: anonymous tokens for public repos at
    `auth.docker.io/token`; user/pass via Basic to the same URL.
  * **GHCR**: `ghcr.io/token` returns anonymous tokens for public
    images, and PAT-authenticated tokens scoped to the repo.
  * **Quay**: uses the same flow with `quay.io/v2/auth`.
  * **ECR**: tokens come from `aws ecr get-authorization-token`
    instead of the realm — we don't handle that here; operators
    pre-provision the token and pass it via static auth.

This module handles the realm flow uniformly. Static (pre-provisioned)
tokens skip realm negotiation entirely via `StaticAuth`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True, slots=True)
class StaticAuth:
    """Fixed bearer token — for ECR / pre-issued enterprise tokens."""

    token: str

    def headers(self, _scope: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class _ChallengeAuth(Protocol):
    """Pluggable per-realm token exchange."""

    async def fetch_token(
        self, client: httpx.AsyncClient, realm: str, scope: str, service: str
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class BasicAuth:
    """Username + password (or PAT) submitted to the realm via HTTP Basic."""

    username: str
    password: str

    async def fetch_token(
        self, client: httpx.AsyncClient, realm: str, scope: str, service: str
    ) -> str:
        params = {"service": service, "scope": scope}
        resp = await client.get(
            realm, params=params, auth=(self.username, self.password)
        )
        resp.raise_for_status()
        body = resp.json()
        # Docker Hub returns `token`; other registries occasionally
        # return `access_token` (RFC 6750). Both are bearer tokens; we
        # accept either to stay portable across the ecosystem.
        token = body.get("token") or body.get("access_token")
        if not token:
            raise ValueError(
                "registry token-realm response had neither 'token' nor "
                "'access_token'"
            )
        return token


@dataclass(frozen=True, slots=True)
class AnonymousAuth:
    """Public-repo bearer token — no credentials sent to the realm."""

    async def fetch_token(
        self, client: httpx.AsyncClient, realm: str, scope: str, service: str
    ) -> str:
        params = {"service": service, "scope": scope}
        resp = await client.get(realm, params=params)
        resp.raise_for_status()
        body = resp.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            raise ValueError(
                "anonymous token request returned neither 'token' nor "
                "'access_token'"
            )
        return token


def parse_challenge(www_authenticate: str) -> dict[str, str]:
    """Pull realm/service/scope out of a `Bearer realm="...",...` header.

    Forgiving on whitespace and key order. Rejects non-Bearer schemes
    explicitly so a Basic challenge on a misconfigured registry
    surfaces with a clear error rather than an empty params dict.
    """
    if not www_authenticate.lower().startswith("bearer "):
        raise ValueError(
            f"only Bearer challenges supported, got: {www_authenticate!r}"
        )
    rest = www_authenticate[len("Bearer "):]
    out: dict[str, str] = {}
    for pair in _split_header(rest):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        out[key.strip().lower()] = value.strip().strip('"')
    return out


def _split_header(header: str) -> list[str]:
    """Split a header value on commas, honoring quoted values."""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    for ch in header:
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
        elif ch == "," and not in_quotes:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf).strip())
    return parts


__all__ = [
    "AnonymousAuth",
    "BasicAuth",
    "StaticAuth",
    "parse_challenge",
]
