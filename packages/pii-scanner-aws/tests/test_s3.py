"""Unit tests for the S3 SourceConnector."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pleno_pii_scanner.scheduler.rate_limit import BucketKey, RateLimited
from pleno_pii_scanner.sources.base import (
    Capabilities,
    Document,
    DocumentChunk,
    DocumentRef,
    SourceConnector,
    SourceFilter,
)

from pleno_pii_scanner_aws.auth import (
    AccountSpec,
    AwsBaseIdentity,
    AwsCredentials,
    AwsSessionFactory,
    StubHopRunner,
)
from pleno_pii_scanner_aws.s3 import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_MAX_DOC_BYTES,
    KIND,
    SPEC,
    BucketSpec,
    S3Config,
    S3Connector,
    _Cursor,
    _factory,
    _maybe_close,
    _maybe_raise_rate_limited,
    _parse_iso,
    _parse_s3_uri,
    _parse_total_from_range,
)

from tests._fakes import FakeObject, FakeS3Client, _ClientError, make_client_factory


def _factory_creds() -> AwsSessionFactory:
    return AwsSessionFactory(
        base=AwsBaseIdentity(access_key_id="A", secret_access_key="B"),
        _hop_runner=StubHopRunner(
            seed=AwsCredentials(
                access_key_id="A",
                secret_access_key="B",
                region="us-east-1",
            )
        ),
    )


def _config(
    buckets: list[BucketSpec],
    accounts: list[AccountSpec] | None = None,
    **overrides: Any,
) -> S3Config:
    accs = tuple(accounts or [AccountSpec(account_id="111111111111")])
    return S3Config(accounts=accs, buckets=tuple(buckets), **overrides)


class TestS3Config:
    def test_defaults(self) -> None:
        c = _config([BucketSpec(name="b")])
        assert c.max_doc_bytes == DEFAULT_MAX_DOC_BYTES
        assert c.chunk_bytes == DEFAULT_CHUNK_BYTES
        assert c.include_versions is False

    def test_requires_accounts(self) -> None:
        with pytest.raises(ValueError, match="accounts"):
            S3Config(accounts=(), buckets=(BucketSpec(name="b"),))

    def test_requires_buckets(self) -> None:
        with pytest.raises(ValueError, match="buckets"):
            S3Config(accounts=(AccountSpec(account_id="1"),), buckets=())

    def test_invalid_max_doc_bytes(self) -> None:
        with pytest.raises(ValueError):
            _config([BucketSpec(name="b")], max_doc_bytes=0)

    def test_invalid_chunk_bytes(self) -> None:
        with pytest.raises(ValueError):
            _config([BucketSpec(name="b")], chunk_bytes=0)

    def test_invalid_concurrency(self) -> None:
        with pytest.raises(ValueError):
            _config([BucketSpec(name="b")], concurrency=0)

    def test_glacier_restore_rejected(self) -> None:
        with pytest.raises(ValueError, match="restore_from_glacier"):
            _config([BucketSpec(name="b")], restore_from_glacier=True)


class TestCursor:
    def test_round_trip(self) -> None:
        c = _Cursor(
            bucket_index=2,
            continuation_token="t",
            last_modified_floor="2024-01-01T00:00:00+00:00",
        )
        again = _Cursor.loads(c.dumps())
        assert again == c

    def test_none_loads_default(self) -> None:
        assert _Cursor.loads(None) == _Cursor()

    def test_garbage_raises(self) -> None:
        with pytest.raises(ValueError):
            _Cursor.loads("not-json")


class TestProtocolCompliance:
    def test_runtime_isinstance(self) -> None:
        c = S3Connector(_config([BucketSpec(name="b")]), _factory_creds())
        assert isinstance(c, SourceConnector)

    def test_capabilities(self) -> None:
        c = S3Connector(
            _config([BucketSpec(name="b")], concurrency=8), _factory_creds()
        )
        caps = c.capabilities()
        assert caps == Capabilities(
            incremental=True,
            binary=True,
            content_hash_delta=True,
            max_concurrent_fetches=8,
            streaming=True,
        )

    def test_id_default(self) -> None:
        c = S3Connector(_config([BucketSpec(name="b")]), _factory_creds())
        assert c.id == "aws-s3:default"

    def test_bucket_key_includes_account(self) -> None:
        c = S3Connector(_config([BucketSpec(name="b")]), _factory_creds())
        key = c.bucket_key(c._config.accounts[0], c._config.buckets[0])
        assert key == BucketKey(connector_kind=KIND, tenant_id="111111111111:b")

    async def test_close_is_noop(self) -> None:
        c = S3Connector(_config([BucketSpec(name="b")]), _factory_creds())
        await c.close()


def _client_with(objects: dict[str, list[FakeObject]], **kw: Any) -> FakeS3Client:
    return FakeS3Client(objects, **kw)


def _connector(config: S3Config, client: FakeS3Client | None = None) -> S3Connector:
    factory = make_client_factory(lambda a, b: client or _client_with({}))
    return S3Connector(config, _factory_creds(), client_factory=factory)


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


async def _drain_refs(c: S3Connector, cursor: str | None = None) -> list[DocumentRef]:
    return [r async for r in c.discover(SourceFilter(), cursor)]


class TestDiscoverObjects:
    async def test_lists_and_paginates(self) -> None:
        objs = [
            FakeObject(key=f"a/{i}.txt", body=b"x", last_modified=_now())
            for i in range(5)
        ]
        client = _client_with({"b": objs}, page_size=2)
        c = _connector(_config([BucketSpec(name="b")]), client)
        refs = await _drain_refs(c)
        assert len(refs) == 5
        assert {r.path for r in refs} == {f"s3://b/a/{i}.txt" for i in range(5)}

    async def test_metadata_carries_account_and_key(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"hello", last_modified=_now())]}
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = (await _drain_refs(c))[0]
        assert ref.metadata["aws_account_id"] == "111111111111"
        assert ref.metadata["aws_bucket"] == "b"
        assert ref.metadata["aws_key"] == "x"
        assert ref.metadata["aws_storage_class"] == "STANDARD"
        assert "_cursor" in ref.metadata

    async def test_glacier_objects_skipped(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key="a",
                        body=b"x",
                        storage_class="STANDARD",
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="b",
                        body=b"y",
                        storage_class="GLACIER",
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="c",
                        body=b"z",
                        storage_class="DEEP_ARCHIVE",
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="d",
                        body=b"w",
                        storage_class="GLACIER_IR",
                        last_modified=_now(),
                    ),
                ]
            },
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        refs = await _drain_refs(c)
        assert {r.metadata["aws_key"] for r in refs} == {"a"}

    async def test_filter_since_drops_old(self) -> None:
        old = _now() - timedelta(days=10)
        new = _now()
        client = _client_with(
            {
                "b": [
                    FakeObject(key="old", body=b"x", last_modified=old),
                    FakeObject(key="new", body=b"y", last_modified=new),
                ]
            },
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        refs = [
            r
            async for r in c.discover(
                SourceFilter(since=_now() - timedelta(days=1)), None
            )
        ]
        assert {r.metadata["aws_key"] for r in refs} == {"new"}

    async def test_cursor_skips_finished_buckets(self) -> None:
        client = _client_with(
            {
                "b1": [FakeObject(key="x", body=b"a", last_modified=_now())],
                "b2": [FakeObject(key="y", body=b"b", last_modified=_now())],
            },
            page_size=10,
        )
        cfg = _config([BucketSpec(name="b1"), BucketSpec(name="b2")])
        # Same client services both buckets — fine for the fake.
        factory = make_client_factory(lambda a, b: client)
        c = S3Connector(cfg, _factory_creds(), client_factory=factory)
        cursor = _Cursor(bucket_index=1).dumps()
        refs = await _drain_refs(c, cursor)
        assert {r.metadata["aws_bucket"] for r in refs} == {"b2"}

    async def test_throttle_translated_to_rate_limited(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"x", last_modified=_now())]},
            throttle_after_calls=0,
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        with pytest.raises(RateLimited):
            await _drain_refs(c)

    async def test_non_throttle_error_propagates(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"x", last_modified=_now())]}
        )

        async def boom(*a, **k):  # noqa: ARG001
            raise _ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                }
            )

        client.list_objects_v2 = boom  # type: ignore[assignment]
        c = _connector(_config([BucketSpec(name="b")]), client)
        with pytest.raises(_ClientError):
            await _drain_refs(c)


class TestDiscoverVersions:
    async def test_lists_versions(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key="x", body=b"v1", version_id="v1", last_modified=_now()
                    ),
                    FakeObject(
                        key="x", body=b"v2", version_id="v2", last_modified=_now()
                    ),
                ]
            },
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        refs = await _drain_refs(c)
        assert {r.metadata["aws_version_id"] for r in refs} == {"v1", "v2"}

    async def test_versions_paginate(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key=f"k{i}", body=b"x", version_id="v", last_modified=_now()
                    )
                    for i in range(5)
                ]
            },
            page_size=2,
        )
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        refs = await _drain_refs(c)
        assert len(refs) == 5

    async def test_versions_throttle_translated(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(key="x", body=b"x", version_id="v", last_modified=_now())
                ]
            },
            throttle_after_calls=0,
        )
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        with pytest.raises(RateLimited):
            await _drain_refs(c)

    async def test_versions_filter_since(self) -> None:
        old = _now() - timedelta(days=10)
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"x", version_id="v", last_modified=old)]},
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        refs = [
            r
            async for r in c.discover(
                SourceFilter(since=_now() - timedelta(days=1)), None
            )
        ]
        assert refs == []


class TestSamplingPath:
    async def test_reservoir_caps_results(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(key=f"k{i}", body=b"x", last_modified=_now())
                    for i in range(100)
                ]
            },
            page_size=10,
        )
        cfg = _config(
            [BucketSpec(name="b", estimated_object_count=10**7)],
            reservoir_size=5,
            sampling_seed=42,
        )
        c = _connector(cfg, client)
        refs = await _drain_refs(c)
        assert len(refs) == 5

    async def test_force_full_scan_disables_sampling(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(key=f"k{i}", body=b"x", last_modified=_now())
                    for i in range(50)
                ]
            },
            page_size=20,
        )
        cfg = _config(
            [BucketSpec(name="b", estimated_object_count=10**7, force_full_scan=True)],
            reservoir_size=5,
        )
        c = _connector(cfg, client)
        refs = await _drain_refs(c)
        assert len(refs) == 50

    async def test_sampled_path_throttle_translated(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"x", last_modified=_now())]},
            throttle_after_calls=0,
        )
        cfg = _config(
            [BucketSpec(name="b", estimated_object_count=10**7)],
            reservoir_size=5,
            sampling_seed=1,
        )
        c = _connector(cfg, client)
        with pytest.raises(RateLimited):
            await _drain_refs(c)

    async def test_sampled_path_filter_since(self) -> None:
        old = _now() - timedelta(days=10)
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"x", last_modified=old)]},
            page_size=10,
        )
        cfg = _config(
            [BucketSpec(name="b", estimated_object_count=10**7)],
            reservoir_size=5,
            sampling_seed=1,
        )
        c = _connector(cfg, client)
        refs = [
            r
            async for r in c.discover(
                SourceFilter(since=_now() - timedelta(days=1)), None
            )
        ]
        assert refs == []


class TestInventoryPath:
    async def test_reads_inventory_csv(self) -> None:
        manifest = {
            "fileSchema": "Bucket, Key, Size, LastModifiedDate, ETag, StorageClass",
            "files": [{"key": "shard-1.csv"}],
        }
        rows = [
            "b,prod/key1,100,2025-01-01T00:00:00Z,etag1,STANDARD",
            "b,prod/key2,200,2025-01-02T00:00:00Z,etag2,GLACIER",  # skipped
            "b,prod/key3,300,2025-01-03T00:00:00Z,etag3,STANDARD",
        ]
        objects = {
            "manifest-bucket": [
                FakeObject(
                    key="inv/manifest.json",
                    body=json.dumps(manifest).encode(),
                    last_modified=_now(),
                ),
                FakeObject(
                    key="shard-1.csv",
                    body="\n".join(rows).encode(),
                    last_modified=_now(),
                ),
            ]
        }
        client = _client_with(objects, page_size=10)
        bucket = BucketSpec(
            name="b",
            inventory_manifest_uri="s3://manifest-bucket/inv/manifest.json",
        )
        c = _connector(_config([bucket]), client)
        refs = await _drain_refs(c)
        keys = {r.metadata["aws_key"] for r in refs}
        assert keys == {"prod/key1", "prod/key3"}
        # source flag set to inventory
        assert all(r.metadata["source"] == "inventory" for r in refs)

    async def test_inventory_filter_since(self) -> None:
        manifest = {
            "fileSchema": "Bucket, Key, Size, LastModifiedDate, ETag, StorageClass",
            "files": [{"key": "shard.csv"}],
        }
        rows = ["b,old,1,2020-01-01T00:00:00Z,etag,STANDARD"]
        client = _client_with(
            {
                "manifest-bucket": [
                    FakeObject(
                        key="inv/manifest.json",
                        body=json.dumps(manifest).encode(),
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="shard.csv",
                        body="\n".join(rows).encode(),
                        last_modified=_now(),
                    ),
                ]
            },
            page_size=10,
        )
        bucket = BucketSpec(
            name="b",
            inventory_manifest_uri="s3://manifest-bucket/inv/manifest.json",
        )
        c = _connector(_config([bucket]), client)
        refs = [
            r
            async for r in c.discover(
                SourceFilter(since=_now() - timedelta(days=1)), None
            )
        ]
        assert refs == []

    async def test_inventory_gzipped(self) -> None:
        manifest = {
            "fileSchema": "Bucket, Key, Size, LastModifiedDate, ETag, StorageClass",
            "files": [{"key": "shard.csv.gz"}],
        }
        rows = "b,gz/key,10,2025-01-01T00:00:00Z,e,STANDARD\n"
        client = _client_with(
            {
                "manifest-bucket": [
                    FakeObject(
                        key="inv/manifest.json",
                        body=json.dumps(manifest).encode(),
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="shard.csv.gz",
                        body=gzip.compress(rows.encode()),
                        last_modified=_now(),
                    ),
                ]
            },
            page_size=10,
        )
        bucket = BucketSpec(
            name="b", inventory_manifest_uri="s3://manifest-bucket/inv/manifest.json"
        )
        c = _connector(_config([bucket]), client)
        refs = await _drain_refs(c)
        assert {r.metadata["aws_key"] for r in refs} == {"gz/key"}

    async def test_inventory_skips_short_rows(self) -> None:
        # A row whose column count does not match the schema is skipped
        # rather than raising — protects against malformed legacy shards.
        manifest = {
            "fileSchema": "Bucket, Key, Size, LastModifiedDate, ETag, StorageClass",
            "files": [{"key": "shard.csv"}],
        }
        rows = [
            "b,short,42,2025-01-01T00:00:00Z",  # only 4 columns
            "b,full,100,2025-01-01T00:00:00Z,e,STANDARD",
        ]
        client = _client_with(
            {
                "manifest-bucket": [
                    FakeObject(
                        key="inv/manifest.json",
                        body=json.dumps(manifest).encode(),
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="shard.csv",
                        body="\n".join(rows).encode(),
                        last_modified=_now(),
                    ),
                ]
            },
            page_size=10,
        )
        bucket = BucketSpec(
            name="b", inventory_manifest_uri="s3://manifest-bucket/inv/manifest.json"
        )
        c = _connector(_config([bucket]), client)
        refs = await _drain_refs(c)
        assert {r.metadata["aws_key"] for r in refs} == {"full"}


class TestFetchSmall:
    async def test_yields_single_document(self) -> None:
        body = b"hello world"
        client = _client_with(
            {"b": [FakeObject(key="x", body=body, last_modified=_now())]},
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = (await _drain_refs(c))[0]
        chunks = [d async for d in c.fetch(ref)]
        assert len(chunks) == 1
        assert isinstance(chunks[0], Document)
        assert chunks[0].binary == body

    async def test_fetch_throttle_translated(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"hi", last_modified=_now())]},
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = (await _drain_refs(c))[0]

        async def boom(*a, **k):  # noqa: ARG001
            raise _ClientError(
                {
                    "Error": {"Code": "ThrottlingException"},
                    "ResponseMetadata": {"HTTPStatusCode": 429},
                }
            )

        client.get_object = boom  # type: ignore[assignment]
        with pytest.raises(RateLimited):
            [d async for d in c.fetch(ref)]

    async def test_fetch_versioned(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key="x", body=b"v1-body", version_id="v1", last_modified=_now()
                    )
                ]
            },
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        ref = (await _drain_refs(c))[0]
        out = [d async for d in c.fetch(ref)]
        assert isinstance(out[0], Document)
        assert out[0].binary == b"v1-body"

    async def test_fetch_missing_metadata_raises(self) -> None:
        c = _connector(_config([BucketSpec(name="b")]), _client_with({}))
        bare = DocumentRef(source_id="x", source_kind=KIND, path="s3://b/k")
        with pytest.raises(ValueError, match="metadata missing"):
            [d async for d in c.fetch(bare)]

    async def test_fetch_unknown_account_raises(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"y", last_modified=_now())]}
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = DocumentRef(
            source_id="x",
            source_kind=KIND,
            path="s3://b/x",
            metadata={"aws_account_id": "nope", "aws_bucket": "b", "aws_key": "x"},
            size=1,
        )
        with pytest.raises(KeyError, match="account_id"):
            [d async for d in c.fetch(ref)]

    async def test_fetch_unknown_bucket_raises(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"y", last_modified=_now())]}
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = DocumentRef(
            source_id="x",
            source_kind=KIND,
            path="s3://b/x",
            metadata={
                "aws_account_id": "111111111111",
                "aws_bucket": "ghost",
                "aws_key": "x",
            },
            size=1,
        )
        with pytest.raises(KeyError, match="bucket"):
            [d async for d in c.fetch(ref)]


class TestFetchChunked:
    async def test_streams_in_chunks(self) -> None:
        body = b"A" * 5_000
        client = _client_with(
            {"b": [FakeObject(key="x", body=body, last_modified=_now())]},
            page_size=10,
        )
        cfg = _config([BucketSpec(name="b")], chunk_bytes=1_000, max_doc_bytes=2_000)
        c = _connector(cfg, client)
        ref = (await _drain_refs(c))[0]
        out = [d async for d in c.fetch(ref)]
        assert all(isinstance(p, DocumentChunk) for p in out)
        # Concatenating chunks must reproduce body
        assert b"".join(p.binary for p in out) == body
        # Last chunk flagged
        assert out[-1].is_final is True
        assert all(not p.is_final for p in out[:-1])
        # byte_range tuples are contiguous
        for prev, nxt in zip(out, out[1:]):
            assert nxt.byte_range[0] == prev.byte_range[1] + 1

    async def test_chunked_throttle_translated(self) -> None:
        body = b"A" * 5_000
        client = _client_with(
            {"b": [FakeObject(key="x", body=body, last_modified=_now())]},
            page_size=10,
        )
        cfg = _config([BucketSpec(name="b")], chunk_bytes=1_000, max_doc_bytes=2_000)
        c = _connector(cfg, client)
        ref = (await _drain_refs(c))[0]

        async def boom(*a, **k):  # noqa: ARG001
            raise _ClientError(
                {
                    "Error": {"Code": "SlowDown"},
                    "ResponseMetadata": {"HTTPStatusCode": 503},
                }
            )

        client.get_object = boom  # type: ignore[assignment]
        with pytest.raises(RateLimited):
            [d async for d in c.fetch(ref)]

    async def test_chunked_unknown_size_uses_head(self) -> None:
        body = b"B" * 3_500
        client = _client_with(
            {"b": [FakeObject(key="x", body=body, last_modified=_now())]},
            page_size=10,
        )
        cfg = _config([BucketSpec(name="b")], chunk_bytes=1_000, max_doc_bytes=500)
        c = _connector(cfg, client)
        # Forge a ref without size to exercise the discovery-head path.
        ref = DocumentRef(
            source_id=c.id,
            source_kind=KIND,
            path="s3://b/x",
            metadata={
                "aws_account_id": "111111111111",
                "aws_bucket": "b",
                "aws_key": "x",
            },
            size=None,
        )
        out = [d async for d in c.fetch(ref)]
        assert b"".join(p.binary for p in out) == body
        assert out[-1].is_final is True

    async def test_chunked_head_throttle_translated(self) -> None:
        body = b"x" * 1024
        client = _client_with(
            {"b": [FakeObject(key="x", body=body, last_modified=_now())]},
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = DocumentRef(
            source_id=c.id,
            source_kind=KIND,
            path="s3://b/x",
            metadata={
                "aws_account_id": "111111111111",
                "aws_bucket": "b",
                "aws_key": "x",
            },
            size=None,
        )

        async def boom(*a, **k):  # noqa: ARG001
            raise _ClientError(
                {
                    "Error": {"Code": "SlowDown"},
                    "ResponseMetadata": {"HTTPStatusCode": 503},
                }
            )

        client.get_object = boom  # type: ignore[assignment]
        with pytest.raises(RateLimited):
            [d async for d in c.fetch(ref)]


class TestFactory:
    def test_rejects_loose_dict(self) -> None:
        with pytest.raises(ValueError, match="structured S3Config"):
            _factory({"foo": "bar"})

    def test_accepts_structured_dict(self) -> None:
        cfg = _config([BucketSpec(name="b")])
        sf = _factory_creds()
        connector = _factory({"_config": cfg, "_session_factory": sf})
        assert isinstance(connector, S3Connector)

    def test_spec_metadata(self) -> None:
        assert SPEC.kind == KIND
        assert SPEC.version == "0.1.0"
        assert "s3:GetObject" in SPEC.required_scopes


class TestHelperFunctions:
    def test_parse_iso_handles_z_suffix(self) -> None:
        dt = _parse_iso("2025-01-02T03:04:05Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_parse_iso_naive_gets_utc(self) -> None:
        dt = _parse_iso("2025-01-02T03:04:05")
        assert dt is not None
        assert dt.tzinfo is UTC

    def test_parse_iso_garbage(self) -> None:
        assert _parse_iso("not-a-date") is None

    def test_parse_iso_none(self) -> None:
        assert _parse_iso(None) is None

    def test_parse_total_from_range(self) -> None:
        assert _parse_total_from_range("bytes=0-99/1000") == 1000

    def test_parse_total_from_range_no_total(self) -> None:
        assert _parse_total_from_range("garbage") is None

    def test_parse_total_from_range_unparseable(self) -> None:
        assert _parse_total_from_range("bytes=0-9/abc") is None

    def test_parse_s3_uri(self) -> None:
        assert _parse_s3_uri("s3://b/k/p") == ("b", "k/p")

    def test_parse_s3_uri_rejects_other_scheme(self) -> None:
        with pytest.raises(ValueError):
            _parse_s3_uri("https://b/k")

    def test_parse_s3_uri_rejects_missing_key(self) -> None:
        with pytest.raises(ValueError):
            _parse_s3_uri("s3://only-bucket")

    async def test_maybe_close_handles_sync(self) -> None:
        class B:
            def close(self):
                self.closed = True

        b = B()
        await _maybe_close(b)
        assert b.closed is True

    async def test_maybe_close_handles_async(self) -> None:
        class B:
            async def close(self):
                self.closed = True

        b = B()
        await _maybe_close(b)
        assert b.closed is True

    async def test_maybe_close_handles_no_close(self) -> None:
        await _maybe_close(object())  # no-op

    def test_maybe_raise_rate_limited_on_slowdown(self) -> None:
        exc = _ClientError(
            {
                "Error": {"Code": "SlowDown"},
                "ResponseMetadata": {"HTTPStatusCode": 503},
            }
        )
        with pytest.raises(RateLimited):
            _maybe_raise_rate_limited(exc)

    def test_maybe_raise_rate_limited_on_429(self) -> None:
        exc = _ClientError(
            {
                "Error": {"Code": "Other"},
                "ResponseMetadata": {"HTTPStatusCode": 429},
            }
        )
        with pytest.raises(RateLimited):
            _maybe_raise_rate_limited(exc)

    def test_maybe_raise_rate_limited_passes_403(self) -> None:
        exc = _ClientError(
            {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            }
        )
        # Should not raise — caller re-raises original
        _maybe_raise_rate_limited(exc)

    def test_maybe_raise_rate_limited_no_response_attr(self) -> None:
        _maybe_raise_rate_limited(RuntimeError("plain"))


class TestEdgeCoverage:
    """Tests aimed at exhaustive branch coverage of `s3.py` paths.

    Each test pins down one branch that the broader unit tests do not
    naturally exercise (Glacier in versioned listing, ISO-string
    LastModified, multi-page version pagination, fetch-time non-throttle
    errors, inventory rows with VersionId).
    """

    async def test_versions_path_skips_glacier(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key="g",
                        body=b"x",
                        version_id="v",
                        storage_class="GLACIER",
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="ok",
                        body=b"x",
                        version_id="v",
                        last_modified=_now(),
                    ),
                ]
            },
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        refs = await _drain_refs(c)
        assert {r.metadata["aws_key"] for r in refs} == {"ok"}

    async def test_versions_pagination_carries_version_marker(self) -> None:
        # Force two pages via small page_size; the second list call must
        # carry KeyMarker so we exercise both pagination branches.
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key=f"k{i}", body=b"x", version_id="v", last_modified=_now()
                    )
                    for i in range(4)
                ]
            },
            page_size=2,
        )
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        refs = await _drain_refs(c)
        assert len(refs) == 4
        # second call had KeyMarker set
        kms = [
            params["KeyMarker"]
            for name, params in client.calls
            if name == "list_object_versions"
        ]
        assert kms[0] is None
        assert kms[1] is not None

    async def test_versions_path_pagination_with_existing_version_marker(self) -> None:
        # Drive with a hand-crafted second-page response that exposes a
        # NextVersionIdMarker so the next iteration sets `version_marker`.
        captured: list[dict] = []

        async def list_versions(**kwargs):
            captured.append(kwargs)
            if "VersionIdMarker" not in kwargs:
                return {
                    "Versions": [
                        {
                            "Key": "k",
                            "VersionId": "v1",
                            "Size": 1,
                            "ETag": '"e"',
                            "LastModified": _now(),
                            "StorageClass": "STANDARD",
                        }
                    ],
                    "IsTruncated": True,
                    "NextKeyMarker": "k",
                    "NextVersionIdMarker": "v1",
                }
            return {
                "Versions": [
                    {
                        "Key": "k",
                        "VersionId": "v2",
                        "Size": 1,
                        "ETag": '"e"',
                        "LastModified": _now(),
                        "StorageClass": "STANDARD",
                    }
                ],
                "IsTruncated": False,
            }

        client = _client_with({"b": []}, page_size=10)
        client.list_object_versions = list_versions  # type: ignore[assignment]
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        refs = await _drain_refs(c)
        assert len(refs) == 2
        # Second call must have included VersionIdMarker.
        assert captured[1].get("VersionIdMarker") == "v1"

    async def test_listing_iso_last_modified_string(self) -> None:
        # When LastModified arrives as an ISO string (some SDK paths do
        # this), the connector must parse it rather than passing it on.
        async def list_objects_v2(**kwargs):  # noqa: ARG001
            return {
                "Contents": [
                    {
                        "Key": "x",
                        "Size": 1,
                        "ETag": '"e"',
                        "LastModified": "2025-04-01T00:00:00Z",
                        "StorageClass": "STANDARD",
                    }
                ],
                "IsTruncated": False,
            }

        client = _client_with({"b": []})
        client.list_objects_v2 = list_objects_v2  # type: ignore[assignment]
        c = _connector(_config([BucketSpec(name="b")]), client)
        refs = await _drain_refs(c)
        assert refs[0].last_modified is not None
        assert refs[0].last_modified.year == 2025

    async def test_versions_non_throttle_error_propagates(self) -> None:
        client = _client_with(
            {
                "b": [
                    FakeObject(key="x", body=b"x", version_id="v", last_modified=_now())
                ]
            }
        )

        async def boom(**_):
            raise _ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                }
            )

        client.list_object_versions = boom  # type: ignore[assignment]
        c = _connector(_config([BucketSpec(name="b")], include_versions=True), client)
        with pytest.raises(_ClientError):
            await _drain_refs(c)

    async def test_sampled_non_throttle_error_propagates(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"x", last_modified=_now())]}
        )

        async def boom(**_):
            raise _ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                }
            )

        client.list_objects_v2 = boom  # type: ignore[assignment]
        cfg = _config(
            [BucketSpec(name="b", estimated_object_count=10**7)],
            reservoir_size=5,
            sampling_seed=1,
        )
        c = _connector(cfg, client)
        with pytest.raises(_ClientError):
            await _drain_refs(c)

    async def test_fetch_whole_non_throttle_error_propagates(self) -> None:
        client = _client_with(
            {"b": [FakeObject(key="x", body=b"x", last_modified=_now())]}
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = (await _drain_refs(c))[0]

        async def boom(**_):
            raise _ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                }
            )

        client.get_object = boom  # type: ignore[assignment]
        with pytest.raises(_ClientError):
            [d async for d in c.fetch(ref)]

    async def test_fetch_chunked_versioned(self) -> None:
        body = b"Z" * 4_000
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key="x", body=body, version_id="v1", last_modified=_now()
                    )
                ]
            },
            page_size=10,
        )
        cfg = _config(
            [BucketSpec(name="b")],
            chunk_bytes=1_000,
            max_doc_bytes=500,
            include_versions=True,
        )
        c = _connector(cfg, client)
        ref = (await _drain_refs(c))[0]
        out = [d async for d in c.fetch(ref)]
        assert b"".join(p.binary for p in out) == body
        # Every get_object had VersionId set.
        for name, kwargs in client.calls:
            if name == "get_object":
                assert kwargs["VersionId"] == "v1"

    async def test_fetch_chunked_head_versioned(self) -> None:
        # Combine size-unknown chunked head + version_id metadata to
        # exercise the `if version_id is not None` branch in the head
        # arm of `_fetch_chunked`.
        body = b"H" * 2_000
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key="x", body=body, version_id="v1", last_modified=_now()
                    )
                ]
            },
            page_size=10,
        )
        cfg = _config([BucketSpec(name="b")], chunk_bytes=1_000, max_doc_bytes=500)
        c = _connector(cfg, client)
        ref = DocumentRef(
            source_id=c.id,
            source_kind=KIND,
            path="s3://b/x",
            metadata={
                "aws_account_id": "111111111111",
                "aws_bucket": "b",
                "aws_key": "x",
                "aws_version_id": "v1",
            },
            size=None,
        )
        out = [d async for d in c.fetch(ref)]
        assert b"".join(p.binary for p in out) == body

    async def test_fetch_chunked_subsequent_non_throttle_error(self) -> None:
        body = b"A" * 5_000
        client = _client_with(
            {"b": [FakeObject(key="x", body=body, last_modified=_now())]},
            page_size=10,
        )
        cfg = _config([BucketSpec(name="b")], chunk_bytes=1_000, max_doc_bytes=500)
        c = _connector(cfg, client)
        ref = (await _drain_refs(c))[0]

        original = client.get_object
        call_count = {"n": 0}

        async def maybe_boom(**kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise _ClientError(
                    {
                        "Error": {"Code": "NoSuchKey"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    }
                )
            return await original(**kwargs)

        client.get_object = maybe_boom  # type: ignore[assignment]
        with pytest.raises(_ClientError):
            [d async for d in c.fetch(ref)]

    async def test_sampled_path_skips_glacier(self) -> None:
        # Glacier objects must be skipped in the sampled path too — line 436.
        client = _client_with(
            {
                "b": [
                    FakeObject(
                        key=f"k{i}",
                        body=b"x",
                        storage_class="GLACIER" if i % 2 == 0 else "STANDARD",
                        last_modified=_now(),
                    )
                    for i in range(20)
                ]
            },
            page_size=5,
        )
        cfg = _config(
            [BucketSpec(name="b", estimated_object_count=10**7)],
            reservoir_size=20,  # capacity > non-Glacier count so sample is exact
            sampling_seed=1,
        )
        c = _connector(cfg, client)
        refs = await _drain_refs(c)
        # Half were Glacier; remaining ten must all be STANDARD.
        assert len(refs) == 10
        assert {r.metadata["aws_storage_class"] for r in refs} == {"STANDARD"}

    async def test_chunked_head_non_throttle_error(self) -> None:
        # Cover the non-throttle re-raise inside the chunked head arm — line 567.
        body = b"x" * 1024
        client = _client_with(
            {"b": [FakeObject(key="x", body=body, last_modified=_now())]},
            page_size=10,
        )
        c = _connector(_config([BucketSpec(name="b")]), client)
        ref = DocumentRef(
            source_id=c.id,
            source_kind=KIND,
            path="s3://b/x",
            metadata={
                "aws_account_id": "111111111111",
                "aws_bucket": "b",
                "aws_key": "x",
            },
            size=None,
        )

        async def boom(**_):
            raise _ClientError(
                {
                    "Error": {"Code": "AccessDenied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                }
            )

        client.get_object = boom  # type: ignore[assignment]
        with pytest.raises(_ClientError):
            [d async for d in c.fetch(ref)]

    async def test_inventory_row_with_version_id(self) -> None:
        manifest = {
            "fileSchema": "Bucket, Key, Size, LastModifiedDate, ETag, StorageClass, VersionId",
            "files": [{"key": "shard.csv"}],
        }
        rows = ["b,vk,42,2025-01-01T00:00:00Z,e,STANDARD,vid-1"]
        client = _client_with(
            {
                "manifest-bucket": [
                    FakeObject(
                        key="inv/manifest.json",
                        body=json.dumps(manifest).encode(),
                        last_modified=_now(),
                    ),
                    FakeObject(
                        key="shard.csv",
                        body="\n".join(rows).encode(),
                        last_modified=_now(),
                    ),
                ]
            },
            page_size=10,
        )
        bucket = BucketSpec(
            name="b",
            inventory_manifest_uri="s3://manifest-bucket/inv/manifest.json",
        )
        c = _connector(_config([bucket]), client)
        refs = await _drain_refs(c)
        assert refs[0].metadata["aws_version_id"] == "vid-1"


class TestDefaultClientFactory:
    """Cover the production client_factory closure that wraps aioboto3.

    We exercise the factory shape (it returns an `async with`-able) but
    do NOT enter the context — entering it would attempt to open an
    actual aiohttp connection. The assertion about returning a context
    manager is enough to keep coverage on the lambda body.
    """

    @pytest.mark.filterwarnings(
        "ignore::RuntimeWarning"  # aioboto3 leaves an un-awaited coro from .client()
    )
    def test_returns_async_context_manager(self) -> None:
        from pleno_pii_scanner_aws.s3 import _default_client_factory

        creds = AwsCredentials(
            access_key_id="A", secret_access_key="B", region="us-east-1"
        )
        cm = _default_client_factory(
            session=None,
            creds=creds,
            account=AccountSpec(account_id="1", region="us-east-1"),
            bucket=BucketSpec(name="b"),
        )
        # aioboto3 client is an async context manager.
        assert hasattr(cm, "__aenter__")
        assert hasattr(cm, "__aexit__")
