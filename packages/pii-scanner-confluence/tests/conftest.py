"""Shared hermetic-test fixtures for pleno-pii-scanner-confluence.

Tests never reach the real Confluence API; every HTTP call is served
by `httpx.MockTransport` with hand-written JSON fixtures. The
`make_handler` helper builds a path-substring dispatcher so a single
test can register canned responses for `/rest/api/space`,
`/rest/api/space/{key}/content/page`, `/rest/api/content/{id}/...`
etc.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import httpx


# Public re-export so tests can `from .conftest import ...` if needed.
ResponseFactory = Callable[[httpx.Request], httpx.Response]


def make_handler(
    routes: Iterable[tuple[str, ResponseFactory]],
) -> ResponseFactory:
    """Build a routing handler from `(path-suffix, response_fn)` tuples.

    Routes are matched in order; the first containment match wins. An
    unmatched path raises `AssertionError` so a test never silently
    receives a 200 for a URL it forgot to mock — that's a sign the
    connector is calling an endpoint the test isn't asserting on.
    """
    materialised = list(routes)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for suffix, fn in materialised:
            if suffix in url:
                return fn(request)
        raise AssertionError(
            f"no route matches {request.method} {url}; "
            f"routes={[r[0] for r in materialised]}"
        )

    return handler


def queued(responses: Iterable[httpx.Response]) -> ResponseFactory:
    """Pop the next pre-built response from `responses` per request.

    Useful when a single endpoint must return different bodies on
    successive calls (pagination chains, retry-then-succeed).
    """
    iterator = iter(responses)

    def fn(_: httpx.Request) -> httpx.Response:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise AssertionError("queued responses exhausted") from exc

    return fn


def json_response(payload: Any, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)
