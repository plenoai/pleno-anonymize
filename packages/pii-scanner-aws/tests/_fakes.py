"""In-memory aioboto3-shaped fake S3 client.

Avoids moto / aioboto3.Session over the network so the test suite stays
deterministic, fast (no DNS), and never accidentally talks to AWS. We
implement only the surface that `S3Connector` actually calls:

  * ``list_objects_v2``
  * ``list_object_versions``
  * ``get_object`` (with optional ``Range`` and ``VersionId``)

The fake honours pagination via ``MaxKeys`` defaulting to a small page
size so multi-page tests do not need to insert thousands of objects.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class FakeObject:
    """One stored S3 object used by the fakes."""

    key: str
    body: bytes
    last_modified: datetime = field(default_factory=lambda: datetime.now(UTC))
    storage_class: str = "STANDARD"
    etag: str = "00000000000000000000000000000000"
    version_id: str | None = None
    content_type: str = "application/octet-stream"


class FakeStreamingBody:
    """Minimal aioboto3 StreamingBody shape: async ``read`` + ``close``."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    async def read(self) -> bytes:
        return self._data

    async def close(self) -> None:
        self.closed = True


class FakeS3Client:
    """Per-bucket in-memory S3.

    `throttle_after_calls` tells the fake to raise a botocore-shaped
    SlowDown after N successful list/get calls — exercises the
    `_maybe_raise_rate_limited` translation without standing up moto.
    """

    def __init__(
        self,
        objects: dict[str, list[FakeObject]],
        page_size: int = 2,
        *,
        throttle_after_calls: int | None = None,
    ) -> None:
        self._objects = objects
        self._page_size = page_size
        self._throttle_after = throttle_after_calls
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _maybe_throttle(self) -> None:
        if self._throttle_after is None:
            return
        if len(self.calls) > self._throttle_after:
            raise _ClientError(
                {
                    "Error": {
                        "Code": "SlowDown",
                        "Message": "Reduce your request rate",
                    },
                    "ResponseMetadata": {"HTTPStatusCode": 503},
                }
            )

    async def list_objects_v2(
        self,
        Bucket: str,
        Prefix: str = "",
        ContinuationToken: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append(("list_objects_v2", {"Bucket": Bucket, "Prefix": Prefix}))
        self._maybe_throttle()
        items = [o for o in self._objects.get(Bucket, []) if o.key.startswith(Prefix)]
        # de-dup to head version per key (V2 returns current versions only)
        seen: dict[str, FakeObject] = {}
        for o in items:
            seen.setdefault(o.key, o)
        items = list(seen.values())
        items.sort(key=lambda o: o.key)
        start = 0
        if ContinuationToken:
            start = int(ContinuationToken)
        page = items[start : start + self._page_size]
        next_token = (
            str(start + self._page_size)
            if start + self._page_size < len(items)
            else None
        )
        return {
            "Contents": [
                {
                    "Key": o.key,
                    "Size": len(o.body),
                    "ETag": f'"{o.etag}"',
                    "LastModified": o.last_modified,
                    "StorageClass": o.storage_class,
                    "ContentType": o.content_type,
                }
                for o in page
            ],
            "IsTruncated": next_token is not None,
            "NextContinuationToken": next_token,
        }

    async def list_object_versions(
        self,
        Bucket: str,
        Prefix: str = "",
        KeyMarker: str | None = None,
        VersionIdMarker: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "list_object_versions",
                {"Bucket": Bucket, "Prefix": Prefix, "KeyMarker": KeyMarker},
            )
        )
        self._maybe_throttle()
        items = [o for o in self._objects.get(Bucket, []) if o.key.startswith(Prefix)]
        items.sort(key=lambda o: (o.key, o.version_id or ""))
        start = 0
        if KeyMarker:
            start = int(KeyMarker)
        page = items[start : start + self._page_size]
        next_marker = (
            str(start + self._page_size)
            if start + self._page_size < len(items)
            else None
        )
        return {
            "Versions": [
                {
                    "Key": o.key,
                    "VersionId": o.version_id,
                    "Size": len(o.body),
                    "ETag": f'"{o.etag}"',
                    "LastModified": o.last_modified,
                    "StorageClass": o.storage_class,
                }
                for o in page
            ],
            "IsTruncated": next_marker is not None,
            "NextKeyMarker": next_marker,
            "NextVersionIdMarker": None,
        }

    async def get_object(
        self,
        Bucket: str,
        Key: str,
        Range: str | None = None,
        VersionId: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "get_object",
                {"Bucket": Bucket, "Key": Key, "Range": Range, "VersionId": VersionId},
            )
        )
        self._maybe_throttle()
        for o in self._objects.get(Bucket, []):
            if o.key != Key:
                continue
            if VersionId is not None and o.version_id != VersionId:
                continue
            data = o.body
            content_range = None
            if Range is not None:
                start, end = _parse_range(Range)
                end = min(end, len(o.body) - 1)
                data = o.body[start : end + 1]
                content_range = f"bytes={start}-{end}/{len(o.body)}"
            return {
                "Body": FakeStreamingBody(data),
                "ETag": f'"{o.etag}"',
                "ContentLength": len(data),
                "ContentRange": content_range or "",
                "ContentType": o.content_type,
            }
        raise _ClientError(
            {
                "Error": {"Code": "NoSuchKey", "Message": "Not found"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            }
        )


class _ClientError(Exception):
    """Botocore-shaped ClientError stand-in; carries the response dict."""

    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__(str(response))
        self.response = response


def _parse_range(header: str) -> tuple[int, int]:
    # `bytes=START-END`
    _, _, rest = header.partition("=")
    a, _, b = rest.partition("-")
    return int(a), int(b)


@asynccontextmanager
async def fake_client_cm(client: FakeS3Client):
    """Adapt a `FakeS3Client` into the `async with session.client(...)` shape."""
    yield client


def make_client_factory(
    builder,  # callable: (account, bucket) -> FakeS3Client
):
    """Build a `client_factory` callable accepting the S3Connector signature."""

    def _factory(session, creds, account, bucket):
        del session, creds
        client = builder(account, bucket)
        return fake_client_cm(client)

    return _factory


def stable_objects(items: Iterable[FakeObject]) -> list[FakeObject]:
    """Return a deterministically ordered list (key, version_id)."""
    return sorted(items, key=lambda o: (o.key, o.version_id or ""))
