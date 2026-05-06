"""BigQuery SourceConnector — TABLESAMPLE + dry_run cost cap + WIF.

Pipeline per scan:

  1. Acquire OAuth bearer token. Two modes:
     - `service_account_json`: sign a JWT with the SA private key, POST to
       https://oauth2.googleapis.com/token (urn:ietf:params:oauth:grant-type:jwt-bearer)
       and pull `access_token`.
     - `federated_token`: a WIF-exchanged access token supplied by the
       caller — used directly as the bearer. Lets ops keep SA key
       material out of the scanner entirely.
  2. List datasets in `project` via the BigQuery REST `datasets.list`
     endpoint, OR honour an explicit `datasets` allowlist.
  3. Per dataset, list tables via `tables.list`.
  4. For each table, build a sampling SQL:
       SELECT * FROM `project.dataset.table` TABLESAMPLE SYSTEM (n PERCENT)
     The TABLESAMPLE clause is omitted when `sample_percent=100` so the
     query stays partition-pruning-friendly for small / clustered tables.
  5. Dry-run the SQL via `jobs.insert?dryRun=true`. Read
     `totalBytesProcessed` from the response and raise
     `BigQueryCostCapExceeded` when it exceeds `max_bytes_billed`.
  6. Execute the query via `jobs.query`. Pass `maximumBytesBilled` so
     the warehouse will also refuse the job server-side if the dry-run
     estimate slipped past the cap (defence in depth).
  7. Paginate through `jobs.getQueryResults` until `pageToken` runs out.
  8. Yield one DocumentRef per row, path = "<dataset>/<table>/<row-index>".

`fetch()` returns a Document whose text body is the row dict serialised
as JSON keyed by the schema column names — keeps the body
PII-detector-friendly while preserving column boundaries.

Tests inject the bearer token via monkeypatching `_acquire_token` so we
never have to construct a real RS256 JWT in unit tests.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import Any

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


KIND = "bigquery"
SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
_API_BASE = "https://bigquery.googleapis.com/bigquery/v2"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DEFAULT_MAX_BYTES = 100 * 1024**3  # 100 GiB

# JWT lifetime fed into the SA-key exchange. Google caps this at 1h;
# 50 minutes leaves headroom against clock skew.
_JWT_LIFETIME_SECS = 3000


class BigQueryCostCapExceeded(RuntimeError):
    """Raised when a query's dry-run cost estimate exceeds the cap.

    Carries the query and the byte counts so operators can decide whether
    to raise the cap, narrow the projection, or skip the table.
    """

    def __init__(
        self,
        *,
        sql: str,
        total_bytes_processed: int,
        cap: int,
    ) -> None:
        super().__init__(
            f"BigQuery dry-run estimate {total_bytes_processed:,}B exceeds "
            f"cap {cap:,}B for query: {sql[:200]}"
        )
        self.sql = sql
        self.total_bytes_processed = total_bytes_processed
        self.cap = cap


@dataclass(frozen=True, slots=True)
class BigQueryConfig:
    """Construction config for `BigQueryConnector`.

    Exactly one of `service_account_json` / `federated_token` must be set.

    `sample_percent` is the TABLESAMPLE percentage in the open-ended
    interval (0, 100]. `max_bytes_billed` is the cost cap enforced both
    via dry-run pre-check AND server-side `maximumBytesBilled`.
    """

    project: str
    datasets: tuple[str, ...] = ()
    service_account_json: str | None = None
    federated_token: str | None = None
    sample_percent: float = 1.0
    max_bytes_billed: int = _DEFAULT_MAX_BYTES
    page_size: int = 1000
    location: str = "US"
    id: str | None = None

    def __post_init__(self) -> None:
        if not self.project:
            raise ValueError("project must be non-empty")
        # Exactly-one auth mode — both is ambiguous, neither is unauthenticated.
        modes = sum(1 for v in (self.service_account_json, self.federated_token) if v)
        if modes == 0:
            raise ValueError(
                "exactly one of service_account_json or federated_token "
                "must be provided"
            )
        if modes > 1:
            raise ValueError(
                "service_account_json and federated_token are mutually exclusive"
            )
        if not 0 < self.sample_percent <= 100:
            raise ValueError("sample_percent must be in (0, 100]")
        if self.max_bytes_billed < 1:
            raise ValueError("max_bytes_billed must be >= 1")
        if self.page_size < 1:
            raise ValueError("page_size must be >= 1")

    def resolved_id(self) -> str:
        if self.id is not None:
            return self.id
        # Hash project + dataset allowlist so we get a stable id without
        # leaking SA key material into ops-visible identifiers.
        import hashlib

        h = hashlib.sha256()
        h.update(self.project.encode())
        for ds in sorted(self.datasets):
            h.update(b"\0")
            h.update(ds.encode())
        return f"bigquery:{h.hexdigest()[:16]}"


class BigQueryConnector:
    """Read-only SourceConnector for Google BigQuery."""

    kind = KIND

    def __init__(
        self,
        config: BigQueryConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self.id = config.resolved_id()
        if client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
            self._owns_client = True
        else:
            self._client = client
            self._owns_client = False
        # Cached bearer token — refreshed lazily when expired.
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        # Per-row text cache so fetch() does not re-issue a query.
        self._documents: dict[str, str] = {}

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=False,
            binary=False,
            content_hash_delta=False,
            max_concurrent_fetches=2,
            streaming=False,
        )

    async def discover(
        self,
        filter: SourceFilter,
        cursor: Cursor | None,
    ) -> AsyncIterator[DocumentRef]:
        del cursor  # incremental=False; cursor is informational only
        token = await self._acquire_token()
        datasets = await self._list_datasets(token)
        for dataset in datasets:
            tables = await self._list_tables(token, dataset)
            for table in tables:
                async for ref in self._scan_table(
                    token=token,
                    dataset=dataset,
                    table=table,
                    filter=filter,
                ):
                    yield ref

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
            extra=dict(ref.metadata),
        )

    async def close(self) -> None:
        self._documents.clear()
        if self._owns_client:
            await self._client.aclose()

    # --- discovery internals ----------------------------------------

    async def _list_datasets(self, token: str) -> list[str]:
        # Allowlist short-circuits the API call — also lets operators
        # scan a project they don't have datasets.list permission on.
        if self._config.datasets:
            return list(self._config.datasets)
        url = f"{_API_BASE}/projects/{self._config.project}/datasets"
        out: list[str] = []
        page_token: str | None = None
        while True:
            params: dict[str, str] = {}
            if page_token:
                params["pageToken"] = page_token
            body = await self._authed_get_json(token, url, params=params)
            for item in body.get("datasets", []) or []:
                ref = item.get("datasetReference") or {}
                ds_id = ref.get("datasetId")
                if ds_id:
                    out.append(ds_id)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out

    async def _list_tables(self, token: str, dataset: str) -> list[str]:
        url = f"{_API_BASE}/projects/{self._config.project}/datasets/{dataset}/tables"
        out: list[str] = []
        page_token: str | None = None
        while True:
            params: dict[str, str] = {}
            if page_token:
                params["pageToken"] = page_token
            body = await self._authed_get_json(token, url, params=params)
            for item in body.get("tables", []) or []:
                ref = item.get("tableReference") or {}
                t_id = ref.get("tableId")
                if t_id:
                    out.append(t_id)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return out

    def _build_sql(self, dataset: str, table: str) -> str:
        # 100% sampling defeats the purpose of TABLESAMPLE and prevents
        # partition-pruning. Same for the historical 1.0 ratio (some
        # callers pass a fraction by accident — treat it as 1%).
        fqn = f"`{self._config.project}.{dataset}.{table}`"
        if self._config.sample_percent >= 100.0:
            return f"SELECT * FROM {fqn}"
        return (
            f"SELECT * FROM {fqn} "
            f"TABLESAMPLE SYSTEM ({self._config.sample_percent} PERCENT)"
        )

    async def _scan_table(
        self,
        *,
        token: str,
        dataset: str,
        table: str,
        filter: SourceFilter,
    ) -> AsyncIterator[DocumentRef]:
        sql = self._build_sql(dataset, table)
        # Cost cap — pre-flight dry-run.
        await self._dry_run_or_raise(token, sql)
        # Execute query (jobs.query) and paginate through getQueryResults.
        first_url = f"{_API_BASE}/projects/{self._config.project}/queries"
        page = await self._authed_post_json(
            token,
            first_url,
            json_body={
                "query": sql,
                "maxResults": self._config.page_size,
                "useLegacySql": False,
                "maximumBytesBilled": str(self._config.max_bytes_billed),
                "location": self._config.location,
            },
        )
        # Schema is returned only on the first page — cache it so
        # subsequent getQueryResults calls (which sometimes elide the
        # schema) can still project rows by column name.
        schema = page.get("schema") or {}
        job_ref = page.get("jobReference") or {}
        job_id = job_ref.get("jobId")
        row_index = 0
        while True:
            async for ref in self._yield_page(
                dataset=dataset,
                table=table,
                page=page,
                filter=filter,
                start_index=row_index,
                schema=schema,
            ):
                yield ref
            row_index += self._page_row_count(page)
            page_token = page.get("pageToken")
            if not page_token or not job_id:
                return
            page_url = f"{_API_BASE}/projects/{self._config.project}/queries/{job_id}"
            params: dict[str, str] = {
                "pageToken": page_token,
                "maxResults": str(self._config.page_size),
            }
            location = job_ref.get("location") or self._config.location
            if location:
                params["location"] = location
            page = await self._authed_get_json(token, page_url, params=params)

    @staticmethod
    def _page_row_count(page: Mapping[str, Any]) -> int:
        rows = page.get("rows") or []
        return len(rows)

    async def _yield_page(
        self,
        *,
        dataset: str,
        table: str,
        page: Mapping[str, Any],
        filter: SourceFilter,
        start_index: int,
        schema: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[DocumentRef]:
        # Prefer the page's own schema when present (first page); fall
        # back to the cached schema captured at scan start.
        page_schema = page.get("schema") or schema or {}
        fields = page_schema.get("fields") or []
        column_names = [f.get("name", f"col_{i}") for i, f in enumerate(fields)]
        rows = page.get("rows") or []
        for offset, row in enumerate(rows):
            idx = start_index + offset
            full = f"{dataset}/{table}/{idx}"
            if filter.include and not _matches_any(full, filter.include):
                continue
            if filter.exclude and _matches_any(full, filter.exclude):
                continue
            row_dict = _project_row(row, column_names)
            text = json.dumps(row_dict, ensure_ascii=False, default=str)
            self._documents[full] = text
            yield DocumentRef(
                source_id=self.id,
                source_kind=self.kind,
                path=full,
                content_type="application/json",
                size=len(text),
                metadata={
                    "bq_project": self._config.project,
                    "bq_dataset": dataset,
                    "bq_table": table,
                    "row_index": str(idx),
                },
            )

    async def _dry_run_or_raise(self, token: str, sql: str) -> None:
        url = f"{_API_BASE}/projects/{self._config.project}/jobs?dryRun=true"
        # `jobs.insert` (not jobs.query) is the dry-run-supporting endpoint.
        body = await self._authed_post_json(
            token,
            url,
            json_body={
                "configuration": {
                    "query": {
                        "query": sql,
                        "useLegacySql": False,
                    },
                    "dryRun": True,
                }
            },
        )
        # Response shape: {"statistics": {"totalBytesProcessed": "1234", ...}}
        # Fall through silently when the API returns a number directly
        # (some unit-test mocks do this).
        stats = body.get("statistics") or {}
        raw = stats.get("totalBytesProcessed", body.get("totalBytesProcessed", 0))
        try:
            total = int(raw)
        except (TypeError, ValueError):
            total = 0
        if total > self._config.max_bytes_billed:
            raise BigQueryCostCapExceeded(
                sql=sql,
                total_bytes_processed=total,
                cap=self._config.max_bytes_billed,
            )

    # --- token internals --------------------------------------------

    async def _acquire_token(self) -> str:
        """Return a usable bearer token, refreshing when expired.

        Two paths:
          * federated_token shortcut — already-exchanged WIF token.
            We treat it as long-lived enough for one scan; rotation is
            the caller's problem (typical WIF lifetime is 1h).
          * service_account_json — sign + exchange a JWT.

        Tests monkeypatch this entire method to skip JWT signing.
        """
        now = time.time()
        if self._token and now < self._token_expires_at - 30:
            return self._token
        if self._config.federated_token:
            self._token = self._config.federated_token
            # Federated tokens don't expose lifetime in the wire here;
            # cache for an hour, which is the GCP STS default.
            self._token_expires_at = now + 3600
            return self._token
        # SA key flow.
        assert self._config.service_account_json is not None
        sa_data = json.loads(self._config.service_account_json)
        jwt = _sign_sa_jwt(sa_data, scope=SCOPE, lifetime_secs=_JWT_LIFETIME_SECS)
        resp = await self._client.post(
            _TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt,
            },
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("token endpoint returned no access_token")
        self._token = token
        self._token_expires_at = now + int(body.get("expires_in", 3600))
        return token

    # --- HTTP internals ---------------------------------------------

    async def _authed_get_json(
        self,
        token: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def _authed_post_json(
        self,
        token: str,
        url: str,
        *,
        json_body: Any,
    ) -> dict[str, Any]:
        resp = await self._client.post(
            url,
            json=json_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()


# --- helpers --------------------------------------------------------


def _matches_any(s: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(s, p) for p in patterns)


def _project_row(row: Mapping[str, Any], column_names: list[str]) -> dict[str, Any]:
    """Project a BigQuery row (`{"f": [{"v": ...}, ...]}`) to a dict.

    BigQuery's REST API returns rows in a column-positional shape; we
    re-assemble them into a name->value dict so the JSON body the
    scanner sees is column-aware.
    """
    cells = row.get("f") if isinstance(row, Mapping) else None
    if not isinstance(cells, list):
        return {}
    out: dict[str, Any] = {}
    for i, cell in enumerate(cells):
        if i >= len(column_names):
            break
        if isinstance(cell, Mapping):
            out[column_names[i]] = cell.get("v")
        else:
            out[column_names[i]] = None
    return out


def _sign_sa_jwt(sa_data: Mapping[str, Any], *, scope: str, lifetime_secs: int) -> str:
    """Sign a Google service-account JWT (RS256).

    Keeps the dependency surface to stdlib + cryptography (which httpx
    already pulls transitively via certifi-of-something? — no, we need
    to import it lazily to avoid forcing the dep when only WIF is
    used).
    """
    # Lazy import so federated-token-only deployments don't require
    # `cryptography` at install time.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    iat = int(time.time())
    exp = iat + lifetime_secs
    header = {"alg": "RS256", "typ": "JWT", "kid": sa_data.get("private_key_id")}
    payload = {
        "iss": sa_data.get("client_email"),
        "scope": scope,
        "aud": _TOKEN_URL,
        "iat": iat,
        "exp": exp,
    }
    seg_h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    seg_p = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{seg_h}.{seg_p}".encode()
    private_key_pem = sa_data["private_key"].encode()
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{seg_h}.{seg_p}.{_b64url(signature)}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# --- factory / spec -------------------------------------------------


def _factory(config: Mapping[str, Any]) -> SourceConnector:
    if "project" not in config:
        raise ValueError("bigquery connector config requires 'project'")
    return BigQueryConnector(
        BigQueryConfig(
            project=str(config["project"]),
            datasets=tuple(str(d) for d in config.get("datasets", ()) or ()),
            service_account_json=_opt_str(config, "service_account_json"),
            federated_token=_opt_str(config, "federated_token"),
            sample_percent=float(config.get("sample_percent", 1.0)),
            max_bytes_billed=int(config.get("max_bytes_billed", _DEFAULT_MAX_BYTES)),
            page_size=int(config.get("page_size", 1000)),
            location=str(config.get("location", "US")),
            id=_opt_str(config, "id"),
        )
    )


def _opt_str(config: Mapping[str, Any], key: str) -> str | None:
    value = config.get(key)
    return str(value) if value is not None else None


SPEC = ConnectorSpec(
    kind=KIND,
    version="0.1.0",
    factory=_factory,
    capabilities=Capabilities(
        incremental=False,
        binary=False,
        content_hash_delta=False,
        max_concurrent_fetches=2,
        streaming=False,
    ),
    required_scopes=(SCOPE,),
    description=(
        "Google BigQuery SourceConnector. Lists datasets + tables, "
        "samples each table via TABLESAMPLE SYSTEM, refuses queries "
        "whose dry-run cost estimate exceeds max_bytes_billed, and "
        "supports both service-account JSON keys and pre-exchanged "
        "Workload Identity Federation tokens."
    ),
)


__all__ = [
    "BigQueryConfig",
    "BigQueryConnector",
    "BigQueryCostCapExceeded",
    "KIND",
    "SCOPE",
    "SPEC",
]
