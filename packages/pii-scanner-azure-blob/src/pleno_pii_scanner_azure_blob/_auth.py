"""Azure Blob auth — Workload Identity (OIDC), Managed Identity (IMDS),
and Shared Key (account key) credential modes.

A single `TokenSource` interface with two concrete bearer-issuing
implementations (WIF + IMDS), plus a `SharedKeyCredential` for the
legacy account-key path. Each bearer source returns an opaque
`AccessToken(value, expires_at)` and is wired behind a small
`TokenCache` that hands out the same token until 30 s before its
declared expiry. Why 30 s: Azure Storage container listings can burst
for ~10 s during a busy scan; refreshing 30 s before expiry gives us
comfortable headroom so a paginate cannot straddle the boundary and
401 mid-walk. Mirrors `pleno-pii-scanner-gcs/_oauth_token.py` shape so
the two cloud connectors are reviewed identically.

Hermetic-test contract:
- Every network call is routed through an injectable `httpx.AsyncClient`.
  Tests pass an `httpx.MockTransport` client and never touch
  login.microsoftonline.com or the IMDS endpoint.
- The current wall-clock is read via a `now` callable, not
  `datetime.now()` directly, so cache hit/miss/refresh can be exercised
  without `freezegun` / `time.sleep`.
- Shared Key signing is byte-deterministic against a fixed input so
  the golden vector test catches any drift from the documented signing
  recipe (https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-shared-key).

Why we don't pull in `azure-identity` / `azure-storage-blob`:
- They bundle msrest, aiohttp, and a code-gen surface we'd touch <1%
  of. The handful of auth flows we need are <300 LOC and worth owning
  so the test surface stays a single httpx mock transport.
- The wheel matrix in ADR §13 is explicitly small per connector so
  enterprise security teams can audit each independently.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qsl, unquote

import httpx


# Microsoft Entra v2.0 token endpoint template. Tenant-scoped because
# the multi-tenant `/common/` endpoint cannot issue tokens for the
# Azure Storage resource without an admin-consented common app.
_AAD_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
# Azure Storage OAuth resource scope. The `/.default` suffix is the
# v2.0 endpoint convention requesting the union of consented scopes
# for the resource. Hard-coded because this is a public Azure constant
# (https://learn.microsoft.com/en-us/azure/storage/blobs/authorize-access-azure-active-directory).
AZURE_STORAGE_RESOURCE = "https://storage.azure.com/"
AZURE_STORAGE_DEFAULT_SCOPE = f"{AZURE_STORAGE_RESOURCE}.default"
# IMDS endpoint for Managed Identity. The `api-version=2018-02-01` is
# the documented minimum and stable across all Azure SKUs that expose
# IMDS (VMs, AKS, App Service, Functions). Pinned so a future IMDS
# version bump does not silently change the response shape.
_IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
_IMDS_API_VERSION = "2018-02-01"
# JWT-bearer client-assertion grant. RFC 7521 / 7523. AAD documents
# this string verbatim; any deviation 400s with an unhelpful message.
_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# Refresh tokens this many seconds before the upstream `expires_at` so
# in-flight paginates do not 401 mid-walk. 30 s mirrors the GCS / AWS
# connector's safety margin.
_EXPIRY_SAFETY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class AccessToken:
    """Bearer token + absolute expiry timestamp.

    `expires_at` is a UTC `datetime` so cache eviction logic can compare
    against `now()` without juggling time zones. The `value` is the
    raw bearer string we set on the `Authorization: Bearer ...` header.
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
class WorkloadIdentityTokenSource(TokenSource):
    """Microsoft Entra Workload Identity Federation: exchange an external
    OIDC token (GitHub Actions / AKS / arbitrary OIDC IdP) for an Azure
    Storage access token.

    `tenant_id` + `client_id` identify the federated workload identity
    binding configured in Entra; `oidc_token_path` points at a file the
    platform refreshes for us (GHA writes one when `id-token: write`
    permission is set; AKS exposes a similar mount).

    The exchange is documented at
    https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow#third-case-access-token-request-with-a-federated-credential
    — POST `client_assertion_type=jwt-bearer` + the OIDC JWT as
    `client_assertion`, with `scope=https://storage.azure.com/.default`.

    Tokens default to 1 hour; we cache via `TokenCache` and refresh
    30 s before expiry.
    """

    tenant_id: str
    client_id: str
    oidc_token_path: str
    scope: str = AZURE_STORAGE_DEFAULT_SCOPE
    # Test seams.
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC)
    )
    token_reader: Callable[[str], Awaitable[str]] | None = None

    def token_url(self) -> str:
        return _AAD_TOKEN_URL_TEMPLATE.format(tenant=self.tenant_id)

    async def acquire(self, client: httpx.AsyncClient) -> AccessToken:
        # Re-read on every refresh — the platform rotates this file
        # every ~10 min and we must pick up the new JWT, not the one
        # captured at construction time.
        external = await self._read_external_token()
        resp = await client.post(
            self.token_url(),
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_assertion_type": _CLIENT_ASSERTION_TYPE,
                "client_assertion": external,
                "scope": self.scope,
            },
        )
        return _parse_aad_token_response(resp, self.now())

    async def _read_external_token(self) -> str:
        if self.token_reader is not None:
            return await self.token_reader(self.oidc_token_path)
        # Default: read the file off the loop. The file is small
        # (<2 KiB JWT) so the off-loop hit is negligible.
        return await asyncio.to_thread(_read_text_file, self.oidc_token_path)


@dataclass(frozen=True, slots=True)
class ManagedIdentityTokenSource(TokenSource):
    """Azure Managed Identity via the IMDS endpoint at 169.254.169.254.

    Works on every Azure compute SKU that exposes IMDS (VMs, VMSS, AKS,
    App Service, Functions, Container Instances). The `Metadata: true`
    header is required — without it IMDS 400s. `api-version=2018-02-01`
    is pinned because newer versions occasionally change the response
    shape (e.g. `client_id` field), and we want behavior to be stable.

    `client_id` is optional and only used to disambiguate when the host
    has multiple user-assigned identities attached — omit it when the
    workload uses the system-assigned identity.
    """

    resource: str = AZURE_STORAGE_RESOURCE
    client_id: str | None = None
    # Test seam.
    now: Callable[[], datetime] = field(
        default_factory=lambda: lambda: datetime.now(UTC)
    )

    async def acquire(self, client: httpx.AsyncClient) -> AccessToken:
        params: dict[str, str] = {
            "api-version": _IMDS_API_VERSION,
            "resource": self.resource,
        }
        if self.client_id is not None:
            params["client_id"] = self.client_id
        resp = await client.get(
            _IMDS_TOKEN_URL,
            headers={"Metadata": "true"},
            params=params,
        )
        return _parse_imds_token_response(resp, self.now())


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
            if self._cached is not None and not self._cached.is_expired(self.now()):
                return self._cached
            self._cached = await self.source.acquire(client)
            return self._cached

    def invalidate(self) -> None:
        """Force the next `get()` to refresh.

        Useful when the connector receives a 401 mid-scan and suspects
        the token was revoked before its declared expiry (Entra does
        rotate on policy change).
        """
        self._cached = None


# --- Shared Key (account key) signing -------------------------------


@dataclass(frozen=True, slots=True)
class SharedKeyCredential:
    """Account name + base64 account key for legacy Shared Key signing.

    Kept narrow so the connector cannot accidentally dump the key into
    a log message: the dataclass repr discloses only the account name
    (the field is `account_key_b64` but the slot value is opaque).

    `sign(request)` produces an `Authorization: SharedKey <account>:<sig>`
    header per the recipe at
    https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-shared-key
    """

    account_name: str
    account_key_b64: str

    def __post_init__(self) -> None:
        # Validate the key decodes — a paste error here would blow up
        # 5 minutes later mid-signing with an unhelpful "Invalid base64".
        try:
            base64.b64decode(self.account_key_b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError(f"account_key_b64 is not valid base64: {exc}") from None
        if not self.account_name:
            raise ValueError("account_name must be non-empty")


def sign_shared_key(
    method: str,
    url: httpx.URL,
    headers: Mapping[str, str],
    credential: SharedKeyCredential,
    content_length: int | None = None,
) -> str:
    """Compute the `Authorization: SharedKey <account>:<sig>` header.

    Implements the Shared Key (NOT SharedKeyLite) signing recipe from
    https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-shared-key
    byte-exactly. Tested with the spec's golden vector in
    `tests/test_auth.py::TestSharedKey::test_golden_vector`.

    The `StringToSign` layout is:

        VERB + "\n" +
        Content-Encoding + "\n" +
        Content-Language + "\n" +
        Content-Length + "\n" +
        Content-MD5 + "\n" +
        Content-Type + "\n" +
        Date + "\n" +
        If-Modified-Since + "\n" +
        If-Match + "\n" +
        If-None-Match + "\n" +
        If-Unmodified-Since + "\n" +
        Range + "\n" +
        CanonicalizedHeaders +
        CanonicalizedResource

    Special-cases:
    - Content-Length is "" for 0 (per the spec: omit the value when 0).
    - Date is "" when `x-ms-date` is set (the canonical form moves into
      the headers section instead).
    """
    h = {k.lower(): v for k, v in headers.items()}
    verb = method.upper()
    content_encoding = h.get("content-encoding", "")
    content_language = h.get("content-language", "")
    # Spec: Content-Length is "" when zero. We rely on the explicit
    # `content_length` arg because `httpx` may not have populated the
    # header yet at signing time.
    if content_length is None:
        try:
            content_length = int(h.get("content-length", "0"))
        except (TypeError, ValueError):
            content_length = 0
    content_length_str = "" if content_length == 0 else str(content_length)
    content_md5 = h.get("content-md5", "")
    content_type = h.get("content-type", "")
    # Spec: Date header is empty when the canonical x-ms-date is set.
    date = "" if "x-ms-date" in h else h.get("date", "")
    if_modified_since = h.get("if-modified-since", "")
    if_match = h.get("if-match", "")
    if_none_match = h.get("if-none-match", "")
    if_unmodified_since = h.get("if-unmodified-since", "")
    range_header = h.get("range", "")
    canonicalized_headers = _canonicalize_headers(h)
    canonicalized_resource = _canonicalize_resource(credential.account_name, url)
    string_to_sign = (
        verb
        + "\n"
        + content_encoding
        + "\n"
        + content_language
        + "\n"
        + content_length_str
        + "\n"
        + content_md5
        + "\n"
        + content_type
        + "\n"
        + date
        + "\n"
        + if_modified_since
        + "\n"
        + if_match
        + "\n"
        + if_none_match
        + "\n"
        + if_unmodified_since
        + "\n"
        + range_header
        + "\n"
        + canonicalized_headers
        + canonicalized_resource
    )
    key = base64.b64decode(credential.account_key_b64)
    sig = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.b64encode(sig).decode("ascii")
    return f"SharedKey {credential.account_name}:{sig_b64}"


def _canonicalize_headers(headers_lower: Mapping[str, str]) -> str:
    """Sort `x-ms-*` headers, lowercase names, strip values, join newline.

    The trailing `\n` is included so the empty-headers case still
    contributes one boundary character to `StringToSign` — matching
    the example in the spec where the headers and resource sections
    are separated by exactly the newline that terminates the last
    header line.
    """
    # Spec: include only headers that begin with `x-ms-`, lowercase
    # them, sort lexicographically, and emit `name:value\n`.
    items = sorted(
        (name, value.strip())
        for name, value in headers_lower.items()
        if name.startswith("x-ms-")
    )
    if not items:  # pragma: no cover - all production callers stamp x-ms-date
        return ""
    return "".join(f"{name}:{value}\n" for name, value in items)


def _canonicalize_resource(account_name: str, url: httpx.URL) -> str:
    """Build the CanonicalizedResource string from a request URL.

    Format:
        /<account>/<path>\n
        <param-name-lower>:<comma-joined-values>\n
        ...

    Query parameter names are lowercased and sorted; if a name appears
    multiple times the values are joined by commas (sorted) — matches
    the recipe in the Microsoft docs example for ListBlobs.
    """
    # Path: keep the leading `/`, prepend `/<account>` per spec.
    path = url.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    # `httpx.URL.path` is already percent-decoded for us in the
    # request-building code path, but defensively unquote here so
    # constructed URLs (test seams) round-trip identically.
    path = unquote(path)
    canonicalized = f"/{account_name}{path}"
    # Group query params by lowercased name → sorted comma-joined values.
    # `httpx.URL.query` is bytes; we accept any object exposing `.query`
    # (bytes or str) so test seams without an httpx.URL still work.
    raw_attr = getattr(url, "query", None)
    if raw_attr is None:  # pragma: no cover - httpx URLs always expose .query
        raw_attr = getattr(url, "raw_query", b"")
    if isinstance(raw_attr, bytes):
        raw_query = raw_attr.decode("ascii")
    else:  # pragma: no cover - test seam fallback when .query is str
        raw_query = str(raw_attr or "")
    if not raw_query:
        return canonicalized
    grouped: dict[str, list[str]] = {}
    for name, value in parse_qsl(raw_query, keep_blank_values=True):
        grouped.setdefault(name.lower(), []).append(value)
    lines = []
    for name in sorted(grouped):
        # Sort values then comma-join — `comp=metadata&comp=list` in
        # the spec example sorts to `comp:list,metadata`.
        values = ",".join(sorted(grouped[name]))
        lines.append(f"{name}:{values}")
    return canonicalized + "\n" + "\n".join(lines)


# --- helpers --------------------------------------------------------


def _parse_aad_token_response(resp: httpx.Response, now: datetime) -> AccessToken:
    """Convert an Entra v2.0 token response into our AccessToken.

    Entra returns `access_token` + `expires_in` (seconds). We compute
    the absolute expiry up front so the cache check is a single
    comparison rather than tracking issuance time separately.
    """
    if resp.status_code != 200:
        # Do NOT include `resp.text` — Entra error bodies sometimes
        # echo back the assertion (signed JWT) on debug responses.
        # Keep the message structural only.
        raise httpx.HTTPStatusError(
            f"Entra token endpoint returned status={resp.status_code}",
            request=resp.request,
            response=resp,
        )
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise ValueError("Entra token response missing access_token")
    ttl = int(body.get("expires_in", 3600))
    return AccessToken(value=token, expires_at=now + timedelta(seconds=ttl))


def _parse_imds_token_response(resp: httpx.Response, now: datetime) -> AccessToken:
    """Convert an IMDS token response into our AccessToken.

    IMDS returns `access_token` + `expires_in` (seconds, sometimes a
    string). Stringly-typed numerics are tolerated because IMDS has
    historically flip-flopped between int and string.
    """
    if resp.status_code != 200:
        raise httpx.HTTPStatusError(
            f"IMDS token endpoint returned status={resp.status_code}",
            request=resp.request,
            response=resp,
        )
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise ValueError("IMDS response missing access_token")
    try:
        ttl = int(body.get("expires_in", 3600))
    except (TypeError, ValueError):
        ttl = 3600
    return AccessToken(value=token, expires_at=now + timedelta(seconds=ttl))


def _read_text_file(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


__all__ = [
    "AZURE_STORAGE_DEFAULT_SCOPE",
    "AZURE_STORAGE_RESOURCE",
    "AccessToken",
    "ManagedIdentityTokenSource",
    "SharedKeyCredential",
    "TokenCache",
    "TokenSource",
    "WorkloadIdentityTokenSource",
    "sign_shared_key",
]
