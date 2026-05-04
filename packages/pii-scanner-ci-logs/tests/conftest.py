"""Shared pytest helpers for pleno-pii-scanner-ci-logs.

Centralises the `httpx.MockTransport` route builder and a tiny
zip-fixture builder so every test declares routes as
`(substring, response_fn)` tuples instead of hand-writing a request
dispatcher. Unmatched URLs raise `AssertionError`, which is the
behaviour we want — silently returning a default response would
mask test gaps.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from pleno_pii_scanner.credentials.broker import Credential
from pleno_pii_scanner.sources.base import DocumentRef


Handler = Callable[[httpx.Request], httpx.Response]


def make_handler(routes: list[tuple[str, Handler]]) -> Handler:
    """Build a routing handler from a list of (substring, response_fn).

    First match wins. Centralised here so individual test files do
    not re-implement the same dispatcher.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for needle, fn in routes:
            if needle in url:
                return fn(request)
        raise AssertionError(f"no route matches {url}")

    return handler


def build_zip(members: dict[str, bytes]) -> bytes:
    """Serialize `{name: bytes}` to an in-memory zip blob.

    Used to drive GHA `/logs` fetch tests without touching disk.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def build_zip_bomb(member_name: str, declared_size: int) -> bytes:
    """Build a zip whose member uncompressed size exceeds `declared_size`.

    We write `declared_size` bytes of compressible data with deflate
    so the on-disk zip stays small (single-byte runs compress ~1000x)
    but `ZipInfo.file_size` legitimately reports the declared value.
    The extractor's `info.file_size > cap` guard fires before any
    `read()` call so memory stays bounded — exactly the path the
    test asserts.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Single-byte run compresses to a few hundred bytes regardless
        # of declared_size — characteristic of a real zip-bomb attack.
        zf.writestr(member_name, b"\x00" * declared_size)
    return buf.getvalue()


async def drain(it: AsyncIterator[DocumentRef]) -> list[DocumentRef]:
    return [ref async for ref in it]


# ---------------------------------------------------------------------
# Credential fixtures (one per flavor)
# ---------------------------------------------------------------------


@pytest.fixture
def gha_credential() -> Credential:
    """GitHub Actions PAT — `Authorization: Bearer <token>`."""
    return Credential(kind="ci_logs", payload={"token": "ghp_TESTTOKEN"})


@pytest.fixture
def circleci_credential() -> Credential:
    """CircleCI personal API token — `Circle-Token: <token>`."""
    return Credential(kind="ci_logs", payload={"token": "circleci_TESTTOKEN"})


@pytest.fixture
def buildkite_credential() -> Credential:
    """Buildkite REST API token — `Authorization: Bearer <token>`."""
    return Credential(kind="ci_logs", payload={"token": "bk_TESTTOKEN"})


@pytest.fixture
def jenkins_credential() -> Credential:
    """Jenkins user + API token — HTTP Basic."""
    return Credential(
        kind="ci_logs",
        payload={"username": "build", "api_token": "jenkins_TESTTOKEN"},
    )
