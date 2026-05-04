"""Salesforce SourceConnector — Cases / Accounts / Opportunities / Users via SOQL.

Pipeline:

  1. Acquire an OAuth2 access token via the JWT Bearer flow:
       - Build an RS256 JWT (`iss=client_id`, `sub=username`,
         `aud=login.salesforce.com`, `exp=now+3min`).
       - POST `/services/oauth2/token` with
         `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`.
       - Cache the bearer until 30 s before its declared expiry.
  2. For each configured sObject (default: Case, Account,
     Opportunity, User):
       - GET `/services/data/<api>/sobjects/<sobject>/describe` to
         enumerate field names. Building the field list from describe
         (rather than `SELECT FIELDS(ALL)`) keeps the query under
         Salesforce's 200-field FIELDS() cap and avoids accidentally
         pulling huge `Body__c` blobs we don't intend to scan.
       - GET `/services/data/<api>/query?q=SELECT Id, ... FROM <obj>`
         on the first page; on resume, GET the stored `nextRecordsUrl`
         directly (Salesforce's REST paginator is a server-side cursor).
       - Yield one DocumentRef per record (path = `<sobject>/<Id>`).
  3. Capture each `nextRecordsUrl` into the per-sobject cursor map so
     the scheduler can resume mid-walk on next run.

Cursor: JSON-encoded `{sobject: nextRecordsUrl}` so a multi-sobject
scan can resume each leg independently. `Cursor` is a `str` type
alias in the core API, so we serialise the map ourselves rather than
inventing a new shape.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from typing import Any
from urllib.parse import quote

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


# Default sObjects most likely to contain customer-supplied PII.
_DEFAULT_SOBJECTS: tuple[str, ...] = (
    "Case",
    "Account",
    "Opportunity",
    "User",
)
# Salesforce login endpoint for the JWT bearer flow. Sandbox orgs use
# `test.salesforce.com`; the JWT `aud` claim must match the endpoint we
# POST to. We expose the audience as a class attribute (not config) so
# operators don't accidentally swap one without the other.
_LOGIN_HOST = "https://login.salesforce.com"
_TOKEN_PATH = "/services/oauth2/token"
# Refresh tokens this many seconds before declared expiry so an
# in-flight paginate cannot 401 mid-walk.
_EXPIRY_SAFETY_SECONDS = 30
# JWT lifetime; Salesforce caps `exp - iat` at 5 minutes per docs.
# 3 min leaves headroom for clock skew while bounding leak blast radius.
_JWT_LIFETIME_SECONDS = 180
# Defensive cap against a server returning the same nextRecordsUrl
# indefinitely. 10k pages × 200 records = 2M rows per sobject — well
# above any realistic scan budget but keeps a misconfigured loop
# from hanging forever.
_MAX_PAGES = 10_000


@dataclass(frozen=True, slots=True)
class SalesforceConfig:
    """Construction config for `SalesforceConnector`."""

    instance_url: str
    client_id: str
    username: str
    private_key_pem: str
    sobjects: tuple[str, ...] = _DEFAULT_SOBJECTS
    api_version: str = "v60.0"
    page_size: int = 200
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.instance_url:
            raise ValueError("instance_url must be non-empty")
        if not self.client_id:
            raise ValueError("client_id must be non-empty")
        if not self.username:
            raise ValueError("username must be non-empty")
        if not self.private_key_pem:
            raise ValueError("private_key_pem must be non-empty")
        if not self.sobjects:
            raise ValueError("sobjects must be a non-empty tuple")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Identify by instance + client_id (a Connected App key is not
        # secret on its own — pairs with the private key). Never include
        # the PEM in the id.
        import hashlib

        h = hashlib.sha256()
        h.update(self.instance_url.encode())
        h.update(b"\0")
        h.update(self.client_id.encode())
        h.update(b"\0")
        h.update(self.username.encode())
        return f"salesforce:{h.hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class _CachedToken:
    value: str
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now + timedelta(seconds=_EXPIRY_SAFETY_SECONDS) >= self.expires_at


class SalesforceConnector:
    """Read-only SourceConnector for Salesforce (Cases / Accounts /
    Opportunities / Users via SOQL)."""

    kind = "salesforce"

    def __init__(
        self,
        config: SalesforceConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=config.instance_url.rstrip("/"),
                timeout=30.0,
            )
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        self._token: _CachedToken | None = None
        self._token_lock = asyncio.Lock()
        # Cache of pre-rendered bodies keyed by ref.path so fetch()
        # doesn't re-hit the Salesforce API.
        self._documents: dict[str, str] = {}
        self._extras: dict[str, dict[str, str]] = {}
        # Running per-sobject cursor map: { sobject: nextRecordsUrl }.
        self._cursor_map: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=4,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        await self._acquire_token()
        resume_map = _decode_cursor(cursor)
        for sobject in self._config.sobjects:
            resume_url = resume_map.get(sobject)
            async for record in self._iter_records(sobject, resume_url):
                record_id = str(record.get("Id", ""))
                if not record_id:
                    # Salesforce always returns Id when we ask for it;
                    # a record without one indicates malformed wire
                    # data we'd rather skip than crash on.
                    continue
                full = f"{sobject}/{record_id}"
                if filter.include and not _matches_any(full, filter.include):
                    continue
                if filter.exclude and _matches_any(full, filter.exclude):
                    continue
                # Strip the Salesforce REST envelope (`attributes`)
                # before serialising — it's metadata about the record
                # endpoint, not the record's own content.
                payload = {k: v for k, v in record.items() if k != "attributes"}
                text = json.dumps(payload, sort_keys=True, default=str)
                self._documents[full] = text
                last_modified = record.get("LastModifiedDate")
                metadata: dict[str, str] = {
                    "sobject": sobject,
                    "record_id": record_id,
                }
                if isinstance(last_modified, str) and last_modified:
                    metadata["lastModifiedDate"] = last_modified
                # Embed the up-to-date cursor on every ref so the
                # scheduler can checkpoint after each yield, matching
                # the pattern used by Jira/Postman.
                metadata["_cursor"] = _encode_cursor(self._cursor_map)
                extra: dict[str, str] = dict(metadata)
                self._extras[full] = extra
                yield DocumentRef(
                    source_id=self.id,
                    source_kind=self.kind,
                    path=full,
                    native_url=_record_url(
                        self._config.instance_url, sobject, record_id
                    ),
                    content_type="application/json",
                    size=len(text),
                    etag=record_id,
                    last_modified=_parse_iso(last_modified),
                    metadata=metadata,
                )

    async def fetch(
        self,
        ref: DocumentRef,
    ) -> AsyncIterator[Document | DocumentChunk]:
        text = self._documents.get(ref.path)
        if text is None:
            return
        yield Document(
            ref=ref,
            text=text,
            fetched_at=datetime.now(UTC),
            extra=self._extras.get(ref.path, {}),
        )

    def cursor_after_run(self) -> Cursor | None:
        """JSON-encoded `{sobject: nextRecordsUrl}` map, or None."""
        if not self._cursor_map:
            return None
        return _encode_cursor(self._cursor_map)

    async def close(self) -> None:
        self._documents.clear()
        self._extras.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- internals ------------------------------------------------

    async def _iter_records(
        self, sobject: str, resume_url: str | None
    ) -> AsyncIterator[dict[str, Any]]:
        if resume_url:
            # Resume directly at the persisted nextRecordsUrl — skip
            # describe + initial query because Salesforce's paginator
            # is server-side stateful.
            next_path: str | None = resume_url
        else:
            fields = await self._describe_fields(sobject)
            soql = _build_soql(sobject, fields)
            next_path = (
                f"/services/data/{self._config.api_version}/query"
                f"?q={quote(soql, safe='')}"
            )
        pages = 0
        while next_path is not None and pages < _MAX_PAGES:  # pragma: no branch
            pages += 1
            body = await self._get_json(next_path)
            np = body.get("nextRecordsUrl")
            # Update the cursor map BEFORE yielding records on this
            # page so a consumer that stops mid-page leaves a cursor
            # pointing at the next page (we already saw the records on
            # this page; the next run should pick up from `np`).
            if isinstance(np, str) and np:
                self._cursor_map[sobject] = np
            else:
                self._cursor_map.pop(sobject, None)
            for record in body.get("records", []) or []:
                if isinstance(record, Mapping):
                    yield dict(record)
            if isinstance(np, str) and np:
                next_path = np
            else:
                next_path = None

    async def _describe_fields(self, sobject: str) -> tuple[str, ...]:
        body = await self._get_json(
            f"/services/data/{self._config.api_version}"
            f"/sobjects/{sobject}/describe"
        )
        out: list[str] = []
        for entry in body.get("fields", []) or []:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("name")
            if isinstance(name, str) and name:
                out.append(name)
        return tuple(out)

    async def _get_json(self, path: str) -> dict[str, Any]:
        token = await self._acquire_token()
        resp = await self._client.get(
            path,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def _acquire_token(self) -> str:
        async with self._token_lock:
            now = datetime.now(UTC)
            if self._token is not None and not self._token.is_expired(now):
                return self._token.value
            assertion = _sign_jwt_bearer_assertion(
                client_id=self._config.client_id,
                username=self._config.username,
                private_key_pem=self._config.private_key_pem,
                audience=_LOGIN_HOST,
                now=now,
            )
            resp = await self._client.post(
                f"{_LOGIN_HOST}{_TOKEN_PATH}",
                data={
                    "grant_type": (
                        "urn:ietf:params:oauth:grant-type:jwt-bearer"
                    ),
                    "assertion": assertion,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            access = body.get("access_token")
            if not isinstance(access, str) or not access:
                raise ValueError(
                    "Salesforce token endpoint returned no access_token"
                )
            # Salesforce does not return `expires_in` on the JWT bearer
            # response; tokens default to the org's session timeout
            # (typically 2 h). Cache for 1 h to bound staleness.
            ttl = int(body.get("expires_in", 3600))
            self._token = _CachedToken(
                value=access,
                expires_at=now + timedelta(seconds=ttl),
            )
            return access


# --- JWT signing --------------------------------------------------


def _sign_jwt_bearer_assertion(
    *,
    client_id: str,
    username: str,
    private_key_pem: str,
    audience: str,
    now: datetime,
) -> str:
    """Build a signed RS256 JWT for the Salesforce JWT bearer flow.

    Salesforce requires `iss=client_id` (Connected App key),
    `sub=username` (the integration user), and `aud=login.salesforce.com`
    (or `test.salesforce.com` for sandboxes — we standardise on
    production). `exp` is bounded at `iat + 5 minutes` per docs; we use
    3 min to leave headroom for clock skew.
    """
    issued_at = int(now.timestamp())
    payload = {
        "iss": client_id,
        "sub": username,
        "aud": audience,
        "exp": issued_at + _JWT_LIFETIME_SECONDS,
    }
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = _b64url_json(header) + b"." + _b64url_json(payload)
    signature = _rs256_sign(private_key_pem, signing_input)
    return (signing_input + b"." + _b64url(signature)).decode("ascii")


def _rs256_sign(private_key_pem: str, data: bytes) -> bytes:
    """RS256 sign `data` with the PEM-encoded private key."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    private_key = serialization.load_pem_private_key(
        private_key_pem.encode("utf-8"), password=None
    )
    return private_key.sign(data, padding.PKCS1v15(), hashes.SHA256())


def _b64url(data: bytes) -> bytes:
    """URL-safe base64 without trailing `=` padding (JWT spec)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _b64url_json(obj: Any) -> bytes:
    return _b64url(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


# --- SOQL ---------------------------------------------------------


def _build_soql(sobject: str, fields: tuple[str, ...]) -> str:
    """Build a SOQL query selecting Id and every described field.

    Always include `Id` first so the connector has a stable record
    identifier even if the describe response has been overridden by an
    operator-installed field-level security rule that hides Id from the
    fields list.
    """
    seen: set[str] = set()
    ordered: list[str] = ["Id"]
    seen.add("id")
    for f in fields:
        key = f.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(f)
    columns = ", ".join(ordered)
    return f"SELECT {columns} FROM {sobject}"


# --- cursor codec -------------------------------------------------


def _encode_cursor(cursor_map: Mapping[str, str]) -> str:
    """JSON-encode the per-sobject `nextRecordsUrl` map."""
    # Sort keys so the same logical map round-trips to the same string
    # — the scheduler's CheckpointStore dedups on the cursor value.
    return json.dumps(dict(cursor_map), sort_keys=True)


def _decode_cursor(cursor: Cursor | None) -> dict[str, str]:
    if not cursor:
        return {}
    try:
        decoded = json.loads(cursor)
    except (ValueError, TypeError):
        return {}
    if not isinstance(decoded, Mapping):
        return {}
    return {
        str(k): str(v)
        for k, v in decoded.items()
        if isinstance(k, str) and isinstance(v, str)
    }


# --- helpers ------------------------------------------------------


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(s, p) for p in patterns)


def _record_url(instance_url: str, sobject: str, record_id: str) -> str:
    return f"{instance_url.rstrip('/')}/lightning/r/{sobject}/{record_id}/view"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- factory / spec -----------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    for required in ("instance_url", "client_id", "username", "private_key_pem"):
        if required not in config:
            raise ValueError(
                f"salesforce connector config requires {required!r}"
            )
    sobjects_raw = config.get("sobjects", _DEFAULT_SOBJECTS)
    sobjects = tuple(str(s) for s in sobjects_raw)
    return SalesforceConnector(
        SalesforceConfig(
            instance_url=str(config["instance_url"]),
            client_id=str(config["client_id"]),
            username=str(config["username"]),
            private_key_pem=str(config["private_key_pem"]),
            sobjects=sobjects,
            api_version=str(config.get("api_version", "v60.0")),
            page_size=int(config.get("page_size", 200)),
            id=str(config["id"]) if config.get("id") is not None else None,
        )
    )


SPEC = ConnectorSpec(
    kind="salesforce",
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=True,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=4,
        streaming=False,
    ),
    required_scopes=("api refresh_token",),
    description=(
        "Salesforce SourceConnector. JWT bearer flow against a Connected "
        "App; SOQL `SELECT * FROM <sobject>` over Cases, Accounts, "
        "Opportunities, and Users by default, paginated via "
        "`nextRecordsUrl`."
    ),
)


__all__ = [
    "SPEC",
    "SalesforceConfig",
    "SalesforceConnector",
]
