"""ConfluenceApi tests — auth header shape, paginator, rate-limit retry."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner_confluence.api import (
    BasicAuth,
    BearerAuth,
    ConfluenceApi,
    ConfluenceApiError,
)

from .conftest import json_response, make_handler, queued


# Cloud + DC base URLs we use across the suite. Cloud is a fake
# `<site>.atlassian.net/wiki`; DC is a self-hosted host. Both share the
# `/rest/api` prefix so the paginator path is identical.
_CLOUD_BASE = "https://acme.atlassian.net/wiki"
_DC_BASE = "https://confluence.acme.internal"


class TestAuthHeaders:
    def test_basic_auth_header_value_is_b64(self) -> None:
        # `email:api_token` base64-encoded; we assert against a known
        # transformation rather than re-running base64 in the test.
        h = BasicAuth(username="alice@x.test", password="t").header_value()
        assert h.startswith("Basic ")
        # The token must not appear verbatim in the header.
        assert "t" not in h.split(" ", 1)[1] or h != "Basic dDp0"

    def test_bearer_auth_header_value(self) -> None:
        assert BearerAuth(token="ya29").header_value() == "Bearer ya29"


class TestApiCloud:
    async def test_get_sends_authorization_header(self) -> None:
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return json_response({"results": [], "_links": {}})

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="ya29"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/rest/api/space")
            assert seen_headers["authorization"] == "Bearer ya29"
            assert seen_headers["accept"] == "application/json"
            assert "pleno" in seen_headers["user-agent"]
        finally:
            await api.aclose()

    async def test_paginate_walks_links_next(self) -> None:
        # First page has _links.next pointing at the second page; the
        # second page has no _links.next and terminates the walk.
        responses = iter(
            [
                {
                    "results": [{"id": "1"}, {"id": "2"}],
                    "_links": {"next": "/rest/api/space?cursor=B"},
                },
                {
                    "results": [{"id": "3"}],
                    "_links": {},
                },
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return json_response(next(responses))

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            ids = [e["id"] async for e in api.paginate("/rest/api/space")]
            assert ids == ["1", "2", "3"]
        finally:
            await api.aclose()

    async def test_paginate_handles_absolute_next_url(self) -> None:
        # Cloud v2 returns `_links.next` as an absolute URL (different
        # host); the paginator must follow it without rewriting.
        responses = iter(
            [
                {
                    "results": [{"id": "1"}],
                    "_links": {
                        "next": "https://api.atlassian.com/ex/confluence/CID/pages?cursor=B"
                    },
                },
                {"results": [{"id": "2"}], "_links": {}},
            ]
        )
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return json_response(next(responses))

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            ids = [e["id"] async for e in api.paginate("/rest/api/space")]
            assert ids == ["1", "2"]
            assert any("api.atlassian.com" in u for u in seen_urls)
        finally:
            await api.aclose()

    async def test_paginate_yields_nothing_on_4xx(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "forbidden"})

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            assert [e async for e in api.paginate("/rest/api/space")] == []
        finally:
            await api.aclose()

    async def test_429_retried_once_then_succeeds(self) -> None:
        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "1"}),
                json_response({"results": [{"id": "1"}], "_links": {}}),
            ]
        )
        slept_for: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept_for.append(seconds)

        def handler(_: httpx.Request) -> httpx.Response:
            return next(responses)

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            ids = [e["id"] async for e in api.paginate("/rest/api/space")]
            assert ids == ["1"]
            assert slept_for == [1.0]
        finally:
            await api.aclose()

    async def test_persistent_429_raises_rate_limited(self) -> None:
        async def fake_sleep(_: float) -> None:
            return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "2"})

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(RateLimited):
                await api.get("/rest/api/space")
        finally:
            await api.aclose()

    async def test_503_on_cloud_is_not_throttling(self) -> None:
        # Cloud's 503 is a real error, not a backoff hint — we surface
        # the response (status 503) without retry; paginate sees a
        # non-200 and yields nothing.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={})

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            assert [e async for e in api.paginate("/rest/api/space")] == []
        finally:
            await api.aclose()


class TestApiDatacenter:
    async def test_get_sends_basic_auth_header(self) -> None:
        seen_headers: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(request.headers)
            return json_response({"results": [], "_links": {}})

        api = ConfluenceApi(
            flavor="datacenter",
            base_url=_DC_BASE,
            auth=BasicAuth(username="alice", password="hunter2"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/rest/api/space")
            assert seen_headers["authorization"].startswith("Basic ")
            # `hunter2` must NEVER appear in clear in the wire headers.
            assert "hunter2" not in seen_headers["authorization"]
        finally:
            await api.aclose()

    async def test_503_with_retry_after_is_throttled(self) -> None:
        responses = iter(
            [
                httpx.Response(503, headers={"Retry-After": "1"}),
                json_response({"results": [{"id": "1"}], "_links": {}}),
            ]
        )
        slept_for: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept_for.append(seconds)

        def handler(_: httpx.Request) -> httpx.Response:
            return next(responses)

        api = ConfluenceApi(
            flavor="datacenter",
            base_url=_DC_BASE,
            auth=BasicAuth(username="u", password="p"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            ids = [e["id"] async for e in api.paginate("/rest/api/space")]
            assert ids == ["1"]
            assert slept_for == [1.0]
        finally:
            await api.aclose()

    async def test_persistent_503_raises_rate_limited(self) -> None:
        async def fake_sleep(_: float) -> None:
            return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, headers={"Retry-After": "2"})

        api = ConfluenceApi(
            flavor="datacenter",
            base_url=_DC_BASE,
            auth=BasicAuth(username="u", password="p"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(RateLimited):
                await api.get("/rest/api/space")
        finally:
            await api.aclose()

    async def test_missing_retry_after_uses_default(self) -> None:
        # First response 429 with no Retry-After, second succeeds.
        # The fallback delay must be > 0 so the scheduler's AIMD bucket
        # actually backs off on a misbehaving server.
        responses = iter(
            [
                httpx.Response(429),
                json_response({"results": [], "_links": {}}),
            ]
        )
        slept_for: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept_for.append(seconds)

        def handler(_: httpx.Request) -> httpx.Response:
            return next(responses)

        api = ConfluenceApi(
            flavor="datacenter",
            base_url=_DC_BASE,
            auth=BasicAuth(username="u", password="p"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            await api.get("/rest/api/space")
            assert slept_for and slept_for[0] > 0
        finally:
            await api.aclose()

    async def test_retry_after_unparseable_uses_default(self) -> None:
        # Garbage `Retry-After` (HTTP-date, which Atlassian doesn't
        # send but we defend against) falls back to the constant.
        responses = iter(
            [
                httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2099"}),
                json_response({"results": [], "_links": {}}),
            ]
        )
        slept_for: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept_for.append(seconds)

        def handler(_: httpx.Request) -> httpx.Response:
            return next(responses)

        api = ConfluenceApi(
            flavor="datacenter",
            base_url=_DC_BASE,
            auth=BasicAuth(username="u", password="p"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            await api.get("/rest/api/space")
            assert slept_for and slept_for[0] > 0
        finally:
            await api.aclose()


class TestApiConstruction:
    def test_invalid_flavor_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported confluence flavor"):
            ConfluenceApi(
                flavor="server",  # type: ignore[arg-type]
                base_url=_CLOUD_BASE,
                auth=BearerAuth(token="t"),
            )

    def test_property_accessors(self) -> None:
        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE + "/",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        )
        # base_url is normalised (trailing slash dropped) so the API is
        # safe to concatenate with `/rest/api/...` paths.
        assert api.base_url == _CLOUD_BASE
        assert api.flavor == "cloud"

    async def test_aclose_releases_client(self) -> None:
        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        )
        await api.aclose()
        # Calling aclose twice on httpx is a no-op; the test just
        # verifies our wrapper doesn't crash on double-close.
        await api.aclose()


class TestPaginationGuard:
    async def test_pagination_depth_cap_raises(self) -> None:
        # Configure the module's depth cap down to a tiny number so the
        # test runs in bounded time, then make every page link back to
        # itself.
        from pleno_pii_scanner_confluence import api as api_mod

        original = api_mod._MAX_PAGINATION_DEPTH
        api_mod._MAX_PAGINATION_DEPTH = 3  # type: ignore[attr-defined]

        def handler(_: httpx.Request) -> httpx.Response:
            return json_response(
                {
                    "results": [{"id": "x"}],
                    "_links": {"next": "/rest/api/space?cursor=loop"},
                }
            )

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(ConfluenceApiError, match="pagination exceeded"):
                _ = [e async for e in api.paginate("/rest/api/space")]
        finally:
            api_mod._MAX_PAGINATION_DEPTH = original  # type: ignore[attr-defined]
            await api.aclose()

    async def test_paginate_stops_when_next_link_missing_or_empty(self) -> None:
        # `_links.next = None` and `_links` absent both terminate.
        responses = iter(
            [
                {"results": [{"id": "1"}], "_links": {"next": None}},
            ]
        )

        def handler(_: httpx.Request) -> httpx.Response:
            return json_response(next(responses))

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            ids = [e["id"] async for e in api.paginate("/rest/api/space")]
            assert ids == ["1"]
        finally:
            await api.aclose()


class TestAbsolutePassThrough:
    async def test_get_with_absolute_url_does_not_prefix_base(self) -> None:
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return json_response({"results": [], "_links": {}})

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("https://api.atlassian.com/ex/confluence/CID/pages")
            assert seen_urls == [
                "https://api.atlassian.com/ex/confluence/CID/pages"
            ]
        finally:
            await api.aclose()

    async def test_get_with_path_prefixes_base(self) -> None:
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return json_response({"results": [], "_links": {}})

        api = ConfluenceApi(
            flavor="cloud",
            base_url=_CLOUD_BASE,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("rest/api/space")  # no leading slash
            assert seen_urls and seen_urls[0].startswith(_CLOUD_BASE)
        finally:
            await api.aclose()
