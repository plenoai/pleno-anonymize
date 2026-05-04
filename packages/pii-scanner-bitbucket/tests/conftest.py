"""Shared pytest helpers for pleno-pii-scanner-bitbucket.

Centralises the `httpx.MockTransport` route builder so every test
declares its routes as `(substring, response_fn)` tuples instead of
hand-writing a request dispatcher. Unmatched URLs raise
`AssertionError`, which is the behaviour we want — silently returning
a default response would mask test gaps.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from pleno_pii_scanner.credentials.broker import Credential


Handler = Callable[[httpx.Request], httpx.Response]


def make_handler(routes: list[tuple[str, Handler]]) -> Handler:
    """Build a routing handler from a list of (substring, response_fn).

    First match wins. Centralised here so individual test files do not
    re-implement the same dispatcher.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for suffix, fn in routes:
            if suffix in url:
                return fn(request)
        raise AssertionError(f"no route matches {url}")

    return handler


@pytest.fixture
def cloud_token_credential() -> Credential:
    """Bearer-token credential for Cloud (workspace access token)."""
    return Credential(
        kind="bitbucket",
        payload={"access_token": "ws_token_abc"},
    )


@pytest.fixture
def cloud_basic_credential() -> Credential:
    """Username + app_password credential for Cloud."""
    return Credential(
        kind="bitbucket",
        payload={"username": "alice", "app_password": "ATBB-abc123"},
    )


@pytest.fixture
def server_token_credential() -> Credential:
    """Bearer-token credential for Server (HTTP access token)."""
    return Credential(
        kind="bitbucket",
        payload={"access_token": "BBDC-server-tok"},
    )


@pytest.fixture
def server_basic_credential() -> Credential:
    """Username + password credential for Server (basic PAT)."""
    return Credential(
        kind="bitbucket",
        payload={"username": "svc", "password": "p@55"},
    )


def noop_clone(_url: str, dest):
    """Test seam: pretend a `git clone` succeeded by leaving an empty dir."""
    return dest
