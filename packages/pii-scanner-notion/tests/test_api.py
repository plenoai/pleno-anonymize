"""Tests for `NotionApi` — header pinning, status handling, rate-limit propagation."""

from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner_notion.api import (
    DEFAULT_BASE_URL,
    NOTION_VERSION,
    NotionApi,
    NotionApiError,
)

from .conftest import json_response


class TestConstruction:
    def test_rejects_empty_token(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            NotionApi(token="")

    def test_default_base_and_version(self) -> None:
        api = NotionApi(token="secret_x")
        assert api.base_url == DEFAULT_BASE_URL
        assert api.notion_version == NOTION_VERSION


class TestHeaders:
    async def test_authorization_and_notion_version_pinned(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.headers))
            return json_response({"ok": True})

        transport = httpx.MockTransport(handler)
        api = NotionApi(token="secret_abc", transport=transport)
        try:
            body = await api.get("/users/me")
            assert body == {"ok": True}
            assert captured["authorization"] == "Bearer secret_abc"
            assert captured["notion-version"] == NOTION_VERSION
            assert captured["accept"] == "application/json"
        finally:
            await api.aclose()

    async def test_post_sets_json_body_and_headers(self) -> None:
        seen_body: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_body["text"] = request.content.decode()
            seen_body["ct"] = request.headers["content-type"]
            return json_response({"results": []})

        transport = httpx.MockTransport(handler)
        api = NotionApi(token="x", transport=transport)
        try:
            await api.post("/search", json={"query": ""})
            assert "query" in seen_body["text"]
            assert seen_body["ct"].startswith("application/json")
        finally:
            await api.aclose()

    async def test_custom_notion_version_propagates(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["v"] = request.headers["notion-version"]
            return json_response({})

        api = NotionApi(
            token="x",
            transport=httpx.MockTransport(handler),
            notion_version="2099-12-31",
        )
        try:
            await api.get("/foo")
            assert captured["v"] == "2099-12-31"
        finally:
            await api.aclose()


class TestStatusHandling:
    async def test_404_returns_empty_dict(self) -> None:
        api = NotionApi(
            token="x",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(404, text="not found")
            ),
        )
        try:
            assert await api.get("/pages/missing") == {}
        finally:
            await api.aclose()

    async def test_500_raises_notion_api_error(self) -> None:
        api = NotionApi(
            token="x",
            transport=httpx.MockTransport(lambda _: httpx.Response(500, text="boom")),
        )
        try:
            with pytest.raises(NotionApiError):
                await api.get("/pages/x")
        finally:
            await api.aclose()

    async def test_429_raises_rate_limited_with_retry_after(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "3"}, text="slow")

        api = NotionApi(token="x", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(RateLimited, match="3"):
                await api.get("/search")
        finally:
            await api.aclose()

    async def test_post_propagates_rate_limit(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        api = NotionApi(token="x", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(RateLimited):
                await api.post("/search")
        finally:
            await api.aclose()


class TestUrls:
    async def test_absolute_url_passes_through(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            return json_response({})

        api = NotionApi(token="x", transport=httpx.MockTransport(handler))
        try:
            await api.get("https://example.test/v1/whatever")
            assert captured == ["https://example.test/v1/whatever"]
        finally:
            await api.aclose()

    async def test_post_absolute_url_passes_through(self) -> None:
        captured: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            return json_response({})

        api = NotionApi(token="x", transport=httpx.MockTransport(handler))
        try:
            await api.post("https://example.test/v1/post")
            assert captured == ["https://example.test/v1/post"]
        finally:
            await api.aclose()
