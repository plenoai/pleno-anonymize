"""Shared hermetic-test fixtures for pleno-pii-scanner-jira.

Tests never reach the real Jira API; every HTTP call is served by
`httpx.MockTransport`. The `make_handler` helper builds a substring
dispatcher so a single test can register canned responses for
`/rest/api/3/project/search`, `/rest/api/2/search`, etc.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import httpx


ResponseFactory = Callable[[httpx.Request], httpx.Response]


def make_handler(
    routes: Iterable[tuple[str, ResponseFactory]],
) -> ResponseFactory:
    """Build a routing handler from `(substring, response_fn)` tuples.

    Routes are matched in order; the first containment match wins. An
    unmatched URL raises `AssertionError` so a test never silently
    receives a 200 for a URL it forgot to mock.
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
    """Pop the next pre-built response from `responses` per request."""
    it = iter(responses)

    def fn(_: httpx.Request) -> httpx.Response:
        try:
            return next(it)
        except StopIteration as exc:
            raise AssertionError("queued responses exhausted") from exc

    return fn


def json_response(payload: Any, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


# --- ADF / storage fixture builders -------------------------------


def adf_doc(*nodes: dict[str, Any]) -> dict[str, Any]:
    """Wrap nodes into an ADF document root."""
    return {"type": "doc", "version": 1, "content": list(nodes)}


def adf_text(text: str, marks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        out["marks"] = marks
    return out


def cloud_issue(
    key: str = "PROJ-1",
    summary: str = "Investigate leak",
    updated: str = "2026-05-04T00:00:00.000+0000",
    description: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "10000",
        "key": key,
        "fields": {
            "summary": summary,
            "updated": updated,
            "status": {"name": "Open"},
            "assignee": {"displayName": "Alice"},
            "reporter": {"displayName": "Bob"},
            "description": description or adf_doc(
                {
                    "type": "paragraph",
                    "content": [adf_text("Hello, world.")],
                }
            ),
            "attachment": attachments or [],
        },
    }


def dc_issue(
    key: str = "PROJ-1",
    summary: str = "DC issue",
    updated: str = "2026-05-04T00:00:00.000+0000",
    description: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": "10000",
        "key": key,
        "fields": {
            "summary": summary,
            "updated": updated,
            "status": {"name": "Open"},
            "assignee": {"displayName": "Alice", "name": "alice"},
            "reporter": {"displayName": "Bob", "name": "bob"},
            "description": (
                description if description is not None else "<p>Hello, <b>world</b>.</p>"
            ),
            "attachment": attachments or [],
        },
    }
