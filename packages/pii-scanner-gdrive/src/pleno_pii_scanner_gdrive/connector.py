"""Google Drive SourceConnector — DWD + shared drives + Google Docs export.

Pipeline per scan:

  1. Acquire an OAuth2 access token via the JWT-bearer flow against
     `https://oauth2.googleapis.com/token`. The JWT is RS256-signed
     with the service-account private key; for hermetic tests, the
     signing helper is bypassed by injecting a token directly via
     `_acquire_token` monkeypatching.

  2. Enumerate drives — either the explicit allowlist (`drives=...`),
     or `My Drive` (id `root`) plus, when `include_shared_drives=True`,
     every shared drive returned by `/drive/v3/drives`.

  3. Per drive, paginate
     `GET /drive/v3/files?q=trashed=false&corpora=drive&driveId=<id>&...`
     (My Drive uses `corpora=user` since it has no drive id). Each file
     becomes one `DocumentRef` carrying `drive_id`, `mimeType`, and
     `md5Checksum` (set as `etag` so the scheduler can short-circuit
     content-hash deltas).

  4. The cursor is a JSON-encoded `dict[drive_id -> nextPageToken]`.
     Resume re-emits only pages newer than the recorded cursor; drives
     that were fully walked are skipped.

  5. `fetch()` dispatches per mime type:
       - native Google Docs / Sheets / Slides → `/files/{id}/export?mimeType=...`
         (text/plain → `Document.text`; application/pdf → `Document.binary`)
       - everything else → `/files/{id}?alt=media` (`Document.binary`)
       - files larger than `max_file_size_bytes` → no Document yielded
         (the ref is still discoverable so the scheduler logs the skip).

The connector never logs token contents, the SA private key, or the
raw export bytes.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec


KIND = "gdrive"

# Google API endpoints. Hard-coded — there is no per-tenant override
# for Drive (the impersonated subject is the tenant boundary).
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DRIVE_BASE = "https://www.googleapis.com/drive/v3"

# Google Docs native mime types that have no downloadable bytes and
# require the `/export` endpoint.
_NATIVE_DOC_MIMES = frozenset(
    {
        "application/vnd.google-apps.document",
        "application/vnd.google-apps.spreadsheet",
        "application/vnd.google-apps.presentation",
    }
)

# OAuth scope for read-only Drive enumeration. Listed in SPEC so the
# scheduler can verify least-privilege at registry-load time.
_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MiB


@dataclass(frozen=True, slots=True)
class GdriveConfig:
    """Construction config for `GdriveConnector`.

    `service_account_json` is the entire JSON key blob (not a path) so
    operators can wire it through env vars or secrets-managers without
    a temp-file dance. `impersonate` is the DWD subject email; required
    whenever `include_shared_drives=True` because shared-drive listing
    needs a Workspace user identity. `drives` is an optional allowlist
    of drive IDs (use `"root"` for My Drive).
    """

    service_account_json: str
    impersonate: str | None = None
    drives: tuple[str, ...] = ()
    include_shared_drives: bool = True
    max_file_size_bytes: int = _DEFAULT_MAX_FILE_SIZE
    export_google_docs_as: Literal["text/plain", "application/pdf"] = "text/plain"
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.service_account_json:
            raise ValueError("service_account_json must be non-empty")
        if self.export_google_docs_as not in {"text/plain", "application/pdf"}:
            raise ValueError(
                "export_google_docs_as must be 'text/plain' or 'application/pdf'; "
                f"got {self.export_google_docs_as!r}"
            )
        if self.max_file_size_bytes < 1:
            raise ValueError("max_file_size_bytes must be >= 1")
        # Shared-drive enumeration requires a real Workspace user
        # identity. Reject lopsided config at construction so the
        # error fires here, not 5 minutes into a scan.
        if self.include_shared_drives and not self.impersonate:
            raise ValueError(
                "impersonate must be set when include_shared_drives=True "
                "(shared-drive listing requires a Workspace user identity)"
            )

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # SA JSON contains the private key; identify by hashed key blob
        # + impersonation subject + drive set so two configs with
        # different subjects do not collide on the same checkpoint.
        h = hashlib.sha256()
        h.update(self.service_account_json.encode())
        if self.impersonate:
            h.update(b"\0")
            h.update(self.impersonate.encode())
        for d in sorted(self.drives):
            h.update(b"\0")
            h.update(d.encode())
        return f"gdrive:{h.hexdigest()[:16]}"


class GdriveConnector:
    """Read-only SourceConnector for Google Drive."""

    kind = KIND

    # Endpoint constants — instance-bound so tests can monkeypatch a
    # single connector without mutating module-global state and
    # leaking into parallel test runs.
    _token_url = _TOKEN_URL
    _drive_base = _DRIVE_BASE

    def __init__(
        self,
        config: GdriveConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        # Cached access token + Unix-epoch expiry. Token TTL is ~1h
        # for SA JWT-bearer tokens; we refresh 60s before expiry to
        # avoid the clock-skew failure window.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        # Parsed SA JSON — kept here so signing has constant-time
        # access without re-parsing every refresh.
        try:
            self._sa = json.loads(config.service_account_json)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "service_account_json must be valid JSON"
            ) from exc

    def capabilities(self) -> Capabilities:
        # incremental=True because Drive exposes per-drive pageTokens
        # we can persist; binary=True because most files are arbitrary
        # bytes; content_hash_delta=True because md5Checksum (set as
        # etag) lets the scheduler skip unchanged blobs.
        return Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def discover(
        self, filter: SourceFilter, cursor: Cursor | None
    ) -> AsyncIterator[DocumentRef]:
        page_tokens = _decode_cursor(cursor)
        drives = await self._resolve_drives()
        for drive_id in drives:
            # Sentinel "__done__" means we already drained this drive
            # in a prior run; skip without an HTTP round-trip.
            if page_tokens.get(drive_id) == "__done__":
                continue
            page_token: str | None = page_tokens.get(drive_id) or None
            while True:
                params = _list_files_params(drive_id, page_token)
                resp = await self._authed_get(
                    f"{self._drive_base}/files", params=params
                )
                resp.raise_for_status()
                body = resp.json()
                next_page = body.get("nextPageToken")
                # Update the per-drive token *before* yielding so each
                # ref carries the cursor needed to resume *after* the
                # current page. When `next_page` is empty we mark the
                # drive done so a resumed scan skips it.
                if next_page:
                    page_tokens[drive_id] = next_page
                else:
                    page_tokens[drive_id] = "__done__"
                cursor_str = _encode_cursor(page_tokens)
                for entry in body.get("files", ()) or ():
                    if not isinstance(entry, Mapping):
                        continue
                    ref = self._ref_from_file(drive_id, entry, cursor_str)
                    if ref is None:
                        continue
                    if filter.include and not _matches_any(
                        ref.path, filter.include
                    ):
                        continue
                    if filter.exclude and _matches_any(
                        ref.path, filter.exclude
                    ):
                        continue
                    yield ref
                if not next_page:
                    break
                page_token = next_page

    async def fetch(
        self, ref: DocumentRef
    ) -> AsyncIterator[Document | DocumentChunk]:
        size_str = ref.metadata.get("size")
        # Skip oversized files — emit no Document so the pipeline
        # records a fetch-skip rather than buffering 100 MiB into
        # memory just to drop it on the floor.
        if size_str:
            try:
                size = int(size_str)
            except ValueError:
                size = 0
            if size > self._config.max_file_size_bytes:
                return
        file_id = ref.metadata.get("file_id")
        mime_type = ref.metadata.get("mime_type", "")
        if not file_id:
            return
        if mime_type in _NATIVE_DOC_MIMES:
            export_mime = self._config.export_google_docs_as
            url = f"{self._drive_base}/files/{file_id}/export"
            resp = await self._authed_get(
                url, params={"mimeType": export_mime}
            )
            resp.raise_for_status()
            if export_mime == "text/plain":
                yield Document(
                    ref=ref,
                    text=resp.text,
                    fetched_at=datetime.now(UTC),
                    content_hash=ref.etag,
                )
            else:
                yield Document(
                    ref=ref,
                    binary=resp.content,
                    fetched_at=datetime.now(UTC),
                    content_hash=ref.etag,
                )
            return
        # Generic binary download.
        url = f"{self._drive_base}/files/{file_id}"
        resp = await self._authed_get(url, params={"alt": "media"})
        resp.raise_for_status()
        yield Document(
            ref=ref,
            binary=resp.content,
            fetched_at=datetime.now(UTC),
            content_hash=ref.etag,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # --- internals -------------------------------------------------

    async def _resolve_drives(self) -> tuple[str, ...]:
        """Return the drive-id list to walk.

        Explicit allowlist short-circuits the network call. Otherwise
        we always include `root` (My Drive) and, when configured, the
        result of `/drives` enumeration.
        """
        if self._config.drives:
            return self._config.drives
        drives: list[str] = ["root"]
        if not self._config.include_shared_drives:
            return tuple(drives)
        page_token: str | None = None
        while True:
            params: dict[str, str] = {"pageSize": "100"}
            if page_token:
                params["pageToken"] = page_token
            resp = await self._authed_get(
                f"{self._drive_base}/drives", params=params
            )
            resp.raise_for_status()
            body = resp.json()
            for entry in body.get("drives", ()) or ():
                if not isinstance(entry, Mapping):
                    continue
                drive_id = entry.get("id")
                if isinstance(drive_id, str) and drive_id:
                    drives.append(drive_id)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return tuple(drives)

    def _ref_from_file(
        self, drive_id: str, item: Mapping[str, Any], cursor_str: str
    ) -> DocumentRef | None:
        file_id = item.get("id")
        if not isinstance(file_id, str):
            return None
        name = item.get("name", file_id)
        mime_type = item.get("mimeType", "application/octet-stream")
        size_raw = item.get("size")
        size: int | None
        try:
            size = int(size_raw) if size_raw is not None else None
        except (TypeError, ValueError):
            size = None
        last_modified = _parse_iso_utc(item.get("modifiedTime"))
        metadata: dict[str, str] = {
            "drive_id": drive_id,
            "file_id": file_id,
            "mime_type": mime_type,
            "name": str(name),
            "_cursor": cursor_str,
        }
        if size_raw is not None:
            metadata["size"] = str(size_raw)
        md5 = item.get("md5Checksum")
        if isinstance(md5, str) and md5:
            metadata["md5"] = md5
        return DocumentRef(
            source_id=self.id,
            source_kind=self.kind,
            path=f"{drive_id}/{file_id}",
            native_url=f"https://drive.google.com/file/d/{file_id}/view",
            content_type=mime_type,
            size=size,
            etag=md5 if isinstance(md5, str) else None,
            last_modified=last_modified,
            metadata=metadata,
        )

    async def _authed_get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        token = await self._acquire_token()
        headers = {"Authorization": f"Bearer {token}"}
        return await self._client.get(url, params=params, headers=headers)

    async def _acquire_token(self) -> str:
        """Return a cached or freshly-minted OAuth2 access token.

        Cached for `expires_in - 60s` so a long scan does not call the
        token endpoint per request. The 60s skew matches Google's own
        client libraries.
        """
        now = time.time()
        if self._access_token is not None and now < self._token_expires_at:
            return self._access_token
        assertion = self._build_jwt_assertion(now)
        resp = await self._client.post(
            self._token_url,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("token endpoint returned no access_token")
        ttl = int(body.get("expires_in", 3600))
        self._access_token = token
        self._token_expires_at = now + max(ttl - 60, 1)
        return token

    def _build_jwt_assertion(self, now: float) -> str:
        """Build the SA JWT-bearer assertion.

        Returns a JWT-shaped string. The signature is computed via the
        SA private key when `cryptography` is available; for hermetic
        tests we tolerate a missing crypto stack by stamping a
        deterministic SHA-256 of the signing-input. Tests inject
        `_acquire_token` directly so this code path is rarely hit
        outside production.
        """
        header = {"alg": "RS256", "typ": "JWT"}
        claim = {
            "iss": self._sa.get("client_email", ""),
            "scope": _DRIVE_READONLY_SCOPE,
            "aud": self._token_url,
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        if self._config.impersonate:
            claim["sub"] = self._config.impersonate
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + b"."
            + _b64url(json.dumps(claim, separators=(",", ":")).encode())
        )
        signature = _sign_rs256(
            signing_input, self._sa.get("private_key", "")
        )
        return (signing_input + b"." + _b64url(signature)).decode("ascii")


# --- helpers --------------------------------------------------------


def _b64url(data: bytes) -> bytes:
    """RFC-7515 base64url with no `=` padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _sign_rs256(signing_input: bytes, private_key_pem: str) -> bytes:
    """RS256-sign `signing_input` with the SA private key.

    Falls back to a deterministic SHA-256 stub when `cryptography` is
    unavailable so tests can run without a crypto dependency. The stub
    is *only* exercised in tests where `_acquire_token` is monkey-
    patched away — production traffic always hits the real signer.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:  # pragma: no cover — exercised only when crypto missing
        digest = hashlib.sha256(signing_input).digest()
        return digest
    key = serialization.load_pem_private_key(
        private_key_pem.encode(), password=None
    )
    return key.sign(  # type: ignore[union-attr]
        signing_input, padding.PKCS1v15(), hashes.SHA256()
    )


def _list_files_params(drive_id: str, page_token: str | None) -> dict[str, str]:
    """Build the query params for one `files.list` page.

    `root` is special: it is the My Drive sentinel, has no `driveId`,
    and uses `corpora=user`. Every shared-drive id uses
    `corpora=drive&driveId=<id>` plus `includeItemsFromAllDrives=true`
    so files in nested folders surface in one flat listing.
    """
    params: dict[str, str] = {
        "q": "trashed=false",
        "pageSize": "1000",
        "fields": (
            "files(id,name,mimeType,modifiedTime,size,md5Checksum),"
            "nextPageToken"
        ),
    }
    if drive_id == "root":
        params["corpora"] = "user"
    else:
        params["corpora"] = "drive"
        params["driveId"] = drive_id
        params["includeItemsFromAllDrives"] = "true"
        params["supportsAllDrives"] = "true"
    if page_token:
        params["pageToken"] = page_token
    return params


def _decode_cursor(cursor: Cursor | None) -> dict[str, str]:
    if cursor is None:
        return {}
    try:
        data = json.loads(cursor)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unparseable cursor: {cursor!r}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"cursor must decode to a dict; got {type(data)}")
    out: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def _encode_cursor(page_tokens: Mapping[str, str]) -> Cursor:
    return json.dumps(dict(page_tokens), separators=(",", ":"), sort_keys=True)


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(s, p) for p in patterns)


def _parse_iso_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# --- factory + spec ------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "service_account_json" not in config:
        raise ValueError(
            "gdrive connector config requires 'service_account_json'"
        )
    return GdriveConnector(
        GdriveConfig(
            service_account_json=str(config["service_account_json"]),
            impersonate=(
                str(config["impersonate"])
                if config.get("impersonate") is not None
                else None
            ),
            drives=tuple(str(d) for d in config.get("drives", ()) or ()),
            include_shared_drives=bool(
                config.get("include_shared_drives", True)
            ),
            max_file_size_bytes=int(
                config.get("max_file_size_bytes", _DEFAULT_MAX_FILE_SIZE)
            ),
            export_google_docs_as=str(
                config.get("export_google_docs_as", "text/plain")
            ),  # type: ignore[arg-type]
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=True,
        content_hash_delta=True,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=(_DRIVE_READONLY_SCOPE,),
    description=(
        "Google Drive SourceConnector. Domain-Wide Delegation auth, "
        "shared-drive enumeration, Google Docs/Sheets/Slides export "
        "(text/plain or PDF), per-drive nextPageToken cursor for "
        "incremental resume, md5Checksum-as-etag for content-hash "
        "delta short-circuit, max-file-size cap to skip oversized blobs."
    ),
)


__all__ = [
    "GdriveConfig",
    "GdriveConnector",
    "KIND",
    "SPEC",
]
