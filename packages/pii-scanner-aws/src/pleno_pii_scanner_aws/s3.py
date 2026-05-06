"""AWS S3 SourceConnector.

Implements the `pleno_pii_scanner.sources.base.SourceConnector` protocol
for AWS S3, end-to-end:

  * `discover()` — multi-account fan-out, paginated `ListObjectsV2`
    (or `ListObjectVersions` when `include_versions=True`), incremental
    cursor as JSON `(continuation_token, last_modified_floor)`, optional
    reservoir sampling for >10**6-key buckets, optional S3 Inventory
    manifest path for petabyte-scale buckets.
  * `fetch()` — small objects yield a single `Document`; large objects
    stream as `DocumentChunk` slices via ranged `GetObject`.
  * Rate limiting — `503 SlowDown` and `429` are surfaced as the core
    `RateLimited` exception so the AIMD bucket throttles the scheduler.
  * Glacier — Glacier-class objects are skipped by default. Restore
    workflow is intentionally out of scope (anti-requirement).

Architecture notes:
  * No boto3 sync — `aioboto3.Session` only.
  * No on-disk credential persistence — all secrets live in
    `AwsSessionFactory._cache` for the connector lifetime.
  * No full object reads into memory — DocumentChunk for >max_doc_bytes.
  * One `(account_id, bucket_name)` BucketKey per bucket so the AIMD
    limiter throttles per-bucket, not globally.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pleno_pii_scanner.scheduler.rate_limit import BucketKey, RateLimited
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Cursor,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceFilter,
)
from pleno_pii_scanner.sources.registry import ConnectorSpec

from pleno_pii_scanner_aws.auth import AccountSpec, AwsSessionFactory
from pleno_pii_scanner_aws.sampling import (
    DEFAULT_RESERVOIR_SIZE,
    ReservoirSampler,
    should_sample,
)

logger = logging.getLogger(__name__)


# WHY this kind string: matches the entry-point name in pyproject.toml so
# `pleno-pii-scanner scan aws-s3` and `pleno_pii_scanner.sources.create("aws-s3", ...)`
# resolve to the same connector. Hyphenated for CLI ergonomics.
KIND = "aws-s3"

# WHY 10MB default: presidio + spaCy NER throughput drops sharply above
# 10MB per document (one analyse() call materializes token offsets for
# the whole text). For larger objects we stream in 1MB chunks so each
# NER pass stays within the comfort window.
DEFAULT_MAX_DOC_BYTES = 10 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 1 * 1024 * 1024

# WHY this set: Glacier-tier storage classes require a restore request
# (hours-to-days) before GetObject works. ADR anti-requirement: do not
# kick off restore here. We just skip and log so operators see what was
# missed and can run a separate restore pipeline if they want them.
_GLACIER_STORAGE_CLASSES: frozenset[str] = frozenset(
    {"GLACIER", "DEEP_ARCHIVE", "GLACIER_IR"}
)

# S3 throttling signals. AWS docs: ServiceUnavailable / SlowDown is the
# canonical "back off" code, but 429 is also returned in some regions.
_THROTTLE_STATUS = (429, 503)
_THROTTLE_CODES = frozenset({"SlowDown", "ServiceUnavailable", "ThrottlingException"})


@dataclass(frozen=True, slots=True)
class BucketSpec:
    """One bucket (within an account) to scan.

    `prefix` narrows the listing server-side (cheap). `inventory_manifest_uri`
    points at an S3 Inventory manifest.json — when present we read the
    inventory CSV/parquet shards instead of paginating ListObjectsV2,
    which is the difference between 30 minutes and 30 hours on a 10**9-
    object bucket. `force_full` opts out of reservoir sampling even when
    the threshold says we should sample.
    """

    name: str
    prefix: str = ""
    inventory_manifest_uri: str | None = None
    force_full_scan: bool = False
    estimated_object_count: int | None = None


@dataclass(frozen=True, slots=True)
class S3Config:
    """Construction config for `S3Connector`.

    Multi-account fan-out lives in `accounts`; each account has its own
    assume-role chain so a single scan can hit hundreds of AWS
    Organizations members. `concurrency` bounds the per-account fan-out
    so we do not blow past the upstream STS / S3 quotas in parallel.
    """

    accounts: tuple[AccountSpec, ...]
    buckets: tuple[BucketSpec, ...]
    id: str = "aws-s3:default"
    max_doc_bytes: int = DEFAULT_MAX_DOC_BYTES
    chunk_bytes: int = DEFAULT_CHUNK_BYTES
    include_versions: bool = False
    sampling_threshold: int = 1_000_000
    reservoir_size: int = DEFAULT_RESERVOIR_SIZE
    sampling_seed: int | None = None
    restore_from_glacier: bool = False  # accepted for future symmetry; out-of-scope
    concurrency: int = 4

    def __post_init__(self) -> None:
        if not self.accounts:
            raise ValueError("S3Config.accounts must contain at least one AccountSpec")
        if not self.buckets:
            raise ValueError("S3Config.buckets must contain at least one BucketSpec")
        if self.max_doc_bytes < 1:
            raise ValueError("max_doc_bytes must be >= 1")
        if self.chunk_bytes < 1:
            raise ValueError("chunk_bytes must be >= 1")
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.restore_from_glacier:
            # Anti-requirement: do not initiate Glacier restore from this
            # wheel. Loud failure beats a silent no-op so operators do not
            # think they have restore working.
            raise ValueError(
                "restore_from_glacier=True is out of scope for this wheel; "
                "Glacier objects are skipped (see ADR-0007 §13)"
            )


@dataclass(frozen=True, slots=True)
class _DiscoverPlan:
    """Per-(account, bucket) plan derived from S3Config + filter.

    Held immutably so a discover restart from a checkpoint can rebuild
    the plan deterministically.
    """

    account: AccountSpec
    bucket: BucketSpec
    use_versions: bool
    use_inventory: bool
    sample: bool
    reservoir_size: int


@dataclass(frozen=True, slots=True)
class _Cursor:
    """Decoded scheduler cursor.

    Persisted as JSON via `dumps()`. The scheduler sees only the opaque
    Cursor (`str`) — only the connector decodes it. Fields:

      * `bucket_index` — which (account, bucket) pair we are mid-walk on.
      * `continuation_token` — opaque S3 ContinuationToken (or KeyMarker
        for ListObjectVersions).
      * `last_modified_floor` — ISO8601 timestamp; refs older than this
        are filtered client-side because S3 has no native server-side
        last_modified > since (Inventory diff + Object Lambda are the
        only server-side options and add config burden).
    """

    bucket_index: int = 0
    continuation_token: str | None = None
    version_id_marker: str | None = None
    last_modified_floor: str | None = None

    def dumps(self) -> Cursor:
        return json.dumps(
            {
                "b": self.bucket_index,
                "c": self.continuation_token,
                "v": self.version_id_marker,
                "lmf": self.last_modified_floor,
            },
            separators=(",", ":"),
        )

    @classmethod
    def loads(cls, raw: Cursor | None) -> "_Cursor":
        if raw is None:
            return cls()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unparseable cursor: {raw!r}") from exc
        return cls(
            bucket_index=int(data.get("b", 0)),
            continuation_token=data.get("c"),
            version_id_marker=data.get("v"),
            last_modified_floor=data.get("lmf"),
        )


class S3Connector:
    """SourceConnector for AWS S3.

    Holds an `AwsSessionFactory` (shared across all accounts) and a
    declarative list of `BucketSpec`s. Constructed by the registry
    factory in `__init__.py`; production callers go via
    `pleno_pii_scanner.sources.create("aws-s3", config)`.
    """

    kind = KIND

    def __init__(
        self,
        config: S3Config,
        session_factory: AwsSessionFactory,
        *,
        client_factory: "ClientFactory | None" = None,
    ) -> None:
        self._config = config
        self.id = config.id
        self._sessions = session_factory
        # Test seam: `client_factory(session, account, bucket)` returns an
        # async context manager that yields an aioboto3 S3 client. Default
        # implementation calls `session.client("s3", ...)` directly; tests
        # inject a fake to avoid a real AWS endpoint.
        self._client_factory = client_factory or _default_client_factory

    def capabilities(self) -> Capabilities:
        return Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=self._config.concurrency,
            streaming=True,
        )

    def bucket_key(self, account: AccountSpec, bucket: BucketSpec) -> BucketKey:
        """Per-bucket BucketKey for the AIMD rate limiter.

        ADR-0007 §16 + requirement #8: each (account_id, bucket_name) is
        its own throttle scope because S3 enforces request budgets per
        prefix per second, and bucket names are globally unique.
        """
        return BucketKey(
            connector_kind=self.kind,
            tenant_id=f"{account.account_id}:{bucket.name}",
        )

    async def discover(
        self, filter: SourceFilter, cursor: Cursor | None
    ) -> AsyncIterator[DocumentRef]:
        """Enumerate objects matching `filter`, resuming from `cursor`.

        Buckets are walked sequentially in the order configured; the
        cursor's `bucket_index` lets a resume skip already-finished
        buckets. Within a bucket the S3 ContinuationToken (or
        VersionIdMarker / KeyMarker for versioned listings) drives
        pagination.
        """
        decoded = _Cursor.loads(cursor)
        floor = _parse_iso(decoded.last_modified_floor) or filter.since
        for idx, bucket in enumerate(self._config.buckets):
            if idx < decoded.bucket_index:
                continue
            for account in self._config.accounts:
                plan = _plan_for(self._config, account, bucket, filter)
                async for ref in self._discover_bucket(
                    plan,
                    floor,
                    decoded
                    if idx == decoded.bucket_index
                    else _Cursor(bucket_index=idx),
                ):
                    yield ref

    async def _discover_bucket(
        self,
        plan: _DiscoverPlan,
        floor: datetime | None,
        cursor: _Cursor,
    ) -> AsyncIterator[DocumentRef]:
        """Drive one (account, bucket) listing pass.

        Branches between Inventory-based discovery (manifest CSV) and the
        live ListObjectsV2 / ListObjectVersions paths. Glacier objects
        are skipped uniformly across all branches.
        """
        if plan.use_inventory:
            async for ref in self._discover_inventory(plan, floor):
                yield ref
            return

        creds = await self._sessions.credentials_for(plan.account)
        session = self._sessions.base_session()
        async with self._client_factory(
            session, creds, plan.account, plan.bucket
        ) as s3:
            if plan.sample:
                async for ref in self._discover_sampled(s3, plan, floor, cursor):
                    yield ref
            elif plan.use_versions:
                async for ref in self._discover_versions(s3, plan, floor, cursor):
                    yield ref
            else:
                async for ref in self._discover_objects(s3, plan, floor, cursor):
                    yield ref

    async def _discover_objects(
        self,
        s3: Any,
        plan: _DiscoverPlan,
        floor: datetime | None,
        cursor: _Cursor,
    ) -> AsyncIterator[DocumentRef]:
        """ListObjectsV2 paginator with cursor-aware resume."""
        token = cursor.continuation_token
        page_index = 0
        while True:
            params: dict[str, Any] = {
                "Bucket": plan.bucket.name,
                "Prefix": plan.bucket.prefix,
            }
            if token:
                params["ContinuationToken"] = token
            try:
                resp = await s3.list_objects_v2(**params)
            except Exception as exc:  # noqa: BLE001
                _maybe_raise_rate_limited(exc)
                raise
            for entry in resp.get("Contents", ()):
                ref = _ref_from_listing_entry(self.id, plan, entry, version_id=None)
                if ref is None:
                    continue
                if (
                    floor is not None
                    and ref.last_modified is not None
                    and ref.last_modified <= floor
                ):
                    continue
                yield _attach_cursor(
                    ref,
                    _Cursor(
                        bucket_index=_bucket_index(self._config, plan.bucket),
                        continuation_token=token,
                        last_modified_floor=_iso(floor),
                    ),
                )
            if not resp.get("IsTruncated"):
                return
            token = resp.get("NextContinuationToken")
            page_index += 1

    async def _discover_versions(
        self,
        s3: Any,
        plan: _DiscoverPlan,
        floor: datetime | None,
        cursor: _Cursor,
    ) -> AsyncIterator[DocumentRef]:
        """ListObjectVersions paginator — emits one ref per (key, version)."""
        key_marker = cursor.continuation_token
        version_marker = cursor.version_id_marker
        while True:
            params: dict[str, Any] = {
                "Bucket": plan.bucket.name,
                "Prefix": plan.bucket.prefix,
            }
            if key_marker:
                params["KeyMarker"] = key_marker
            if version_marker:
                params["VersionIdMarker"] = version_marker
            try:
                resp = await s3.list_object_versions(**params)
            except Exception as exc:  # noqa: BLE001
                _maybe_raise_rate_limited(exc)
                raise
            for entry in resp.get("Versions", ()):
                ref = _ref_from_listing_entry(
                    self.id, plan, entry, version_id=entry.get("VersionId")
                )
                if ref is None:
                    continue
                if (
                    floor is not None
                    and ref.last_modified is not None
                    and ref.last_modified <= floor
                ):
                    continue
                yield _attach_cursor(
                    ref,
                    _Cursor(
                        bucket_index=_bucket_index(self._config, plan.bucket),
                        continuation_token=key_marker,
                        version_id_marker=version_marker,
                        last_modified_floor=_iso(floor),
                    ),
                )
            if not resp.get("IsTruncated"):
                return
            key_marker = resp.get("NextKeyMarker")
            version_marker = resp.get("NextVersionIdMarker")

    async def _discover_sampled(
        self,
        s3: Any,
        plan: _DiscoverPlan,
        floor: datetime | None,
        cursor: _Cursor,
    ) -> AsyncIterator[DocumentRef]:
        """Reservoir-sample over a full ListObjectsV2 walk.

        We still pay the full list cost (S3 has no random-access for keys),
        but we cap memory at `reservoir_size` and yield only the sample
        downstream. Cursor support is intentionally limited: a sampled
        scan is treated as atomic — restart re-samples — because mixing
        partial reservoirs across runs would bias the sample.
        """
        del cursor
        sampler: ReservoirSampler[DocumentRef] = ReservoirSampler.make(
            plan.reservoir_size, seed=self._config.sampling_seed
        )
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "Bucket": plan.bucket.name,
                "Prefix": plan.bucket.prefix,
            }
            if token:
                params["ContinuationToken"] = token
            try:
                resp = await s3.list_objects_v2(**params)
            except Exception as exc:  # noqa: BLE001
                _maybe_raise_rate_limited(exc)
                raise
            for entry in resp.get("Contents", ()):
                ref = _ref_from_listing_entry(self.id, plan, entry, version_id=None)
                if ref is None:
                    continue
                if (
                    floor is not None
                    and ref.last_modified is not None
                    and ref.last_modified <= floor
                ):
                    continue
                sampler.offer(ref)
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        for ref in sampler.samples:
            yield ref

    async def _discover_inventory(
        self, plan: _DiscoverPlan, floor: datetime | None
    ) -> AsyncIterator[DocumentRef]:
        """Read an S3 Inventory manifest instead of live ListObjectsV2.

        Inventory is the only realistic discovery path for buckets above
        ~10**8 objects. We expect `inventory_manifest_uri` to point at a
        manifest.json that lists CSV shards (gzipped). The manifest
        format is documented in the AWS S3 Inventory user guide; we
        only consume the `files[]` array to enumerate shards.
        """
        manifest_uri = plan.bucket.inventory_manifest_uri
        assert manifest_uri is not None  # _plan_for guards this
        creds = await self._sessions.credentials_for(plan.account)
        session = self._sessions.base_session()
        manifest_bucket, manifest_key = _parse_s3_uri(manifest_uri)
        async with self._client_factory(
            session, creds, plan.account, plan.bucket
        ) as s3:
            manifest = await _read_manifest(s3, manifest_bucket, manifest_key)
            for shard in manifest.get("files", ()):
                shard_key = shard["key"]
                shard_bytes = await _read_object_bytes(s3, manifest_bucket, shard_key)
                for row in _iter_inventory_rows(shard_bytes, manifest):
                    ref = _ref_from_inventory_row(self.id, plan, row)
                    if ref is None:
                        continue
                    if (
                        floor is not None
                        and ref.last_modified is not None
                        and ref.last_modified <= floor
                    ):
                        continue
                    yield ref

    async def fetch(self, ref: DocumentRef) -> AsyncIterator[Document | DocumentChunk]:
        """Retrieve `ref`'s payload as a Document or DocumentChunk stream.

        The (account, bucket) pair is rebuilt from `ref.metadata`; we put
        the account_id and bucket name there at discover time so fetch
        does not need to scan the config to find them. This keeps fetch
        O(1) regardless of how many accounts the connector manages.
        """
        account_id = ref.metadata.get("aws_account_id")
        bucket_name = ref.metadata.get("aws_bucket")
        key = ref.metadata.get("aws_key", ref.path)
        version_id = ref.metadata.get("aws_version_id")
        if account_id is None or bucket_name is None:
            raise ValueError(
                f"DocumentRef metadata missing aws_account_id / aws_bucket: {ref!r}"
            )
        account = _find_account(self._config, account_id)
        bucket = _find_bucket(self._config, bucket_name)
        creds = await self._sessions.credentials_for(account)
        session = self._sessions.base_session()
        size = ref.size or 0
        async with self._client_factory(session, creds, account, bucket) as s3:
            if size and size <= self._config.max_doc_bytes:
                async for piece in self._fetch_whole(
                    s3, bucket_name, key, version_id, ref
                ):
                    yield piece
            else:
                async for piece in self._fetch_chunked(
                    s3, bucket_name, key, version_id, ref, size
                ):
                    yield piece

    async def _fetch_whole(
        self,
        s3: Any,
        bucket_name: str,
        key: str,
        version_id: str | None,
        ref: DocumentRef,
    ) -> AsyncIterator[Document]:
        params: dict[str, Any] = {"Bucket": bucket_name, "Key": key}
        if version_id is not None:
            params["VersionId"] = version_id
        try:
            resp = await s3.get_object(**params)
        except Exception as exc:  # noqa: BLE001
            _maybe_raise_rate_limited(exc)
            raise
        body = resp["Body"]
        try:
            data = await body.read()
        finally:
            await _maybe_close(body)
        yield Document(
            ref=ref,
            binary=data,
            fetched_at=datetime.now(UTC),
            content_hash=resp.get("ETag", "").strip('"') or None,
        )

    async def _fetch_chunked(
        self,
        s3: Any,
        bucket_name: str,
        key: str,
        version_id: str | None,
        ref: DocumentRef,
        size: int,
    ) -> AsyncIterator[DocumentChunk]:
        # WHY explicit ranges instead of iter_chunks: ranged GET also
        # gives us deterministic byte_range tuples on the DocumentChunk,
        # which the regex cross-chunk overlap relies on. iter_chunks
        # offsets are an implementation detail of the body stream.
        chunk_size = self._config.chunk_bytes
        if size <= 0:
            # Unknown size — read the head to learn it via Content-Range.
            params: dict[str, Any] = {
                "Bucket": bucket_name,
                "Key": key,
                "Range": f"bytes=0-{chunk_size - 1}",
            }
            if version_id is not None:
                params["VersionId"] = version_id
            try:
                head_resp = await s3.get_object(**params)
            except Exception as exc:  # noqa: BLE001
                _maybe_raise_rate_limited(exc)
                raise
            content_range = head_resp.get("ContentRange", "")
            size = _parse_total_from_range(content_range) or chunk_size
            data = await head_resp["Body"].read()
            await _maybe_close(head_resp["Body"])
            end = min(chunk_size, size) - 1
            yield DocumentChunk(
                ref=ref,
                byte_range=(0, end),
                is_final=size <= chunk_size,
                binary=data,
                fetched_at=datetime.now(UTC),
            )
            start = chunk_size
        else:
            start = 0

        while start < size:
            end = min(start + chunk_size, size) - 1
            params = {
                "Bucket": bucket_name,
                "Key": key,
                "Range": f"bytes={start}-{end}",
            }
            if version_id is not None:
                params["VersionId"] = version_id
            try:
                resp = await s3.get_object(**params)
            except Exception as exc:  # noqa: BLE001
                _maybe_raise_rate_limited(exc)
                raise
            body = resp["Body"]
            try:
                data = await body.read()
            finally:
                await _maybe_close(body)
            yield DocumentChunk(
                ref=ref,
                byte_range=(start, end),
                is_final=(end + 1) >= size,
                binary=data,
                fetched_at=datetime.now(UTC),
            )
            start = end + 1

    async def close(self) -> None:
        # Aioboto3 sessions hold no persistent state; the per-call client
        # context managers tear down their HTTP pools. Method exists for
        # protocol symmetry with connectors that hold long-lived handles.
        return None


# --- helpers ---------------------------------------------------------------


# Test seam type — kept narrow so a fake can satisfy `__aenter__`/`__aexit__`
# without implementing every aioboto3 surface. We only call list/get on it.
class ClientFactory:  # pragma: no cover - structural typing alias
    def __call__(
        self,
        session: Any,
        creds: Any,
        account: AccountSpec,
        bucket: BucketSpec,
    ) -> Any: ...


def _default_client_factory(
    session: Any, creds: Any, account: AccountSpec, bucket: BucketSpec
) -> Any:
    """Yield an aioboto3 S3 client bound to the per-account credentials.

    We rebuild a Session from `creds` rather than reusing the base
    session because the assume-role chain may have rotated us into a
    different identity than the base. Region falls back to the account-
    specified region or the base region (`creds.region`).
    """
    import aioboto3

    region = account.region or creds.region
    rebuilt = aioboto3.Session(**creds.to_session_kwargs())
    del session, bucket
    return rebuilt.client("s3", region_name=region)


def _plan_for(
    config: S3Config,
    account: AccountSpec,
    bucket: BucketSpec,
    filter: SourceFilter,
) -> _DiscoverPlan:
    del filter  # currently unused beyond the discover-time floor
    decision = should_sample(
        bucket.estimated_object_count,
        threshold=config.sampling_threshold,
        reservoir_size=config.reservoir_size,
        forced=False if bucket.force_full_scan else None,
    )
    return _DiscoverPlan(
        account=account,
        bucket=bucket,
        use_versions=config.include_versions,
        use_inventory=bucket.inventory_manifest_uri is not None,
        sample=decision.enabled,
        reservoir_size=decision.reservoir_size,
    )


def _ref_from_listing_entry(
    source_id: str,
    plan: _DiscoverPlan,
    entry: Mapping[str, Any],
    version_id: str | None,
) -> DocumentRef | None:
    """Convert one ListObjectsV2 / ListObjectVersions entry into a DocumentRef.

    Returns None when the entry should be skipped — Glacier-class objects
    fall into that bucket. Carries the (account_id, bucket, key) trio in
    metadata so `fetch()` can rebuild context without re-walking config.
    """
    storage_class = entry.get("StorageClass", "STANDARD")
    if storage_class in _GLACIER_STORAGE_CLASSES:
        logger.debug(
            "skipping Glacier-class object: bucket=%s key=%s class=%s",
            plan.bucket.name,
            entry.get("Key"),
            storage_class,
        )
        return None
    key = entry["Key"]
    last_modified = entry.get("LastModified")
    if isinstance(last_modified, str):
        last_modified = _parse_iso(last_modified)
    metadata: dict[str, str] = {
        "aws_account_id": plan.account.account_id,
        "aws_bucket": plan.bucket.name,
        "aws_key": key,
        "aws_storage_class": storage_class,
    }
    if version_id is not None:
        metadata["aws_version_id"] = version_id
    return DocumentRef(
        source_id=source_id,
        source_kind=KIND,
        path=f"s3://{plan.bucket.name}/{key}",
        native_url=f"https://{plan.bucket.name}.s3.amazonaws.com/{key}",
        content_type=entry.get("ContentType", "application/octet-stream"),
        size=entry.get("Size"),
        etag=entry.get("ETag", "").strip('"') or None,
        last_modified=last_modified,
        metadata=metadata,
    )


def _ref_from_inventory_row(
    source_id: str, plan: _DiscoverPlan, row: Mapping[str, str]
) -> DocumentRef | None:
    """Convert one inventory CSV row into a DocumentRef.

    Inventory CSV columns are declared in the manifest's `fileSchema`; we
    look up by name so column reordering (which AWS allows) does not
    silently mis-map. Glacier-class rows are skipped same as live listings.
    """
    storage_class = row.get("StorageClass", "STANDARD")
    if storage_class in _GLACIER_STORAGE_CLASSES:
        return None
    key = row["Key"]
    size_raw = row.get("Size")
    last_modified_raw = row.get("LastModifiedDate")
    metadata: dict[str, str] = {
        "aws_account_id": plan.account.account_id,
        "aws_bucket": plan.bucket.name,
        "aws_key": key,
        "aws_storage_class": storage_class,
        "source": "inventory",
    }
    version_id = row.get("VersionId")
    if version_id:
        metadata["aws_version_id"] = version_id
    return DocumentRef(
        source_id=source_id,
        source_kind=KIND,
        path=f"s3://{plan.bucket.name}/{key}",
        native_url=f"https://{plan.bucket.name}.s3.amazonaws.com/{key}",
        size=int(size_raw) if size_raw else None,
        etag=row.get("ETag") or None,
        last_modified=_parse_iso(last_modified_raw) if last_modified_raw else None,
        metadata=metadata,
    )


def _attach_cursor(ref: DocumentRef, cursor: _Cursor) -> DocumentRef:
    """Embed `cursor.dumps()` in `_cursor` metadata for the scheduler."""
    metadata = dict(ref.metadata)
    metadata["_cursor"] = cursor.dumps()
    return DocumentRef(
        source_id=ref.source_id,
        source_kind=ref.source_kind,
        path=ref.path,
        native_url=ref.native_url,
        parent_chain=ref.parent_chain,
        content_type=ref.content_type,
        size=ref.size,
        etag=ref.etag,
        last_modified=ref.last_modified,
        metadata=metadata,
    )


def _bucket_index(config: S3Config, bucket: BucketSpec) -> int:
    return config.buckets.index(bucket)


def _find_account(config: S3Config, account_id: str) -> AccountSpec:
    for a in config.accounts:
        if a.account_id == account_id:
            return a
    raise KeyError(
        f"account_id={account_id!r} not in S3Config.accounts; "
        f"DocumentRef came from a different connector?"
    )


def _find_bucket(config: S3Config, bucket_name: str) -> BucketSpec:
    for b in config.buckets:
        if b.name == bucket_name:
            return b
    raise KeyError(
        f"bucket={bucket_name!r} not in S3Config.buckets; "
        f"DocumentRef came from a different connector?"
    )


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"expected s3://bucket/key URI, got {uri!r}")
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise ValueError(f"malformed s3 URI: {uri!r}")
    return bucket, key


async def _read_manifest(s3: Any, bucket: str, key: str) -> Mapping[str, Any]:
    raw = await _read_object_bytes(s3, bucket, key)
    return json.loads(raw.decode("utf-8"))


async def _read_object_bytes(s3: Any, bucket: str, key: str) -> bytes:
    resp = await s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"]
    try:
        return await body.read()
    finally:
        await _maybe_close(body)


def _iter_inventory_rows(
    raw: bytes, manifest: Mapping[str, Any]
) -> Iterable[Mapping[str, str]]:
    """Yield CSV rows from one inventory shard.

    Inventory CSV is gzipped per the manifest format, but we accept plain
    text too so test fixtures can avoid the gzip dance. The column names
    come from `manifest["fileSchema"]` (a comma-separated string).
    """
    schema = [
        c.strip() for c in str(manifest.get("fileSchema", "")).split(",") if c.strip()
    ]
    text: str
    if raw[:2] == b"\x1f\x8b":  # gzip magic
        import gzip

        text = gzip.decompress(raw).decode("utf-8")
    else:
        text = raw.decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) != len(schema):
            continue
        yield dict(zip(schema, row, strict=True))


def _parse_total_from_range(header: str) -> int | None:
    """Parse `bytes start-end/total` Content-Range header → total."""
    if "/" not in header:
        return None
    try:
        return int(header.rsplit("/", 1)[-1])
    except ValueError:
        return None


async def _maybe_close(body: Any) -> None:
    """Best-effort close on aioboto3 streaming bodies.

    `StreamingBody.close()` is sync in real aioboto3 but tests stub with
    AsyncMock that exposes async close. We tolerate both shapes so the
    test seam stays simple.
    """
    closer = getattr(body, "close", None)
    if closer is None:
        return
    result = closer()
    if hasattr(result, "__await__"):
        await result


def _maybe_raise_rate_limited(exc: BaseException) -> None:
    """Translate S3 throttle signals into the scheduler's RateLimited.

    aioboto3 surfaces 503 SlowDown / 429 ThrottlingException as
    `botocore.exceptions.ClientError` with a structured response dict.
    We pattern-match on both HTTP status and the error code so AIMD
    feedback is uniform regardless of which path AWS chose.
    """
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return
    error = response.get("Error", {})
    code = error.get("Code") if isinstance(error, Mapping) else None
    metadata = response.get("ResponseMetadata", {})
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    if code in _THROTTLE_CODES or status in _THROTTLE_STATUS:
        raise RateLimited(f"S3 throttled (code={code} status={status})") from exc


# --- factory + spec --------------------------------------------------------


def _factory(config: Mapping[str, Any]) -> S3Connector:
    """Registry factory: build an S3Connector from a config Mapping.

    Accepts either a fully-typed `S3Config` (when an in-process caller
    constructs the connector) or a TOML/YAML-shaped dict with the keys
    documented in README.md. Credentials live in CredentialBroker, not
    here — the connector resolves base identity + per-account hops at
    discover time.
    """
    if isinstance(config.get("_config"), S3Config) and isinstance(
        config.get("_session_factory"), AwsSessionFactory
    ):
        return S3Connector(
            config["_config"],
            config["_session_factory"],
            client_factory=config.get("_client_factory"),
        )
    raise ValueError(
        "aws-s3 connector requires structured S3Config + AwsSessionFactory; "
        "TOML/YAML loading is delegated to the CLI layer (Task #18 / ADR-0007 §14)"
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
        streaming=True,
    ),
    required_scopes=("s3:ListBucket", "s3:GetObject", "sts:AssumeRole"),
    description=(
        "AWS S3 multi-account connector. Walks AssumeRole chains for "
        "Organizations fan-out, reservoir-samples buckets >10**6 objects "
        "(ADR-0007 §16), prefers S3 Inventory over live ListObjectsV2 "
        "when configured, streams TB-scale objects as DocumentChunks via "
        "ranged GET. Glacier-class objects are skipped."
    ),
)


__all__ = [
    "BucketSpec",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_MAX_DOC_BYTES",
    "KIND",
    "S3Config",
    "S3Connector",
    "SPEC",
]
