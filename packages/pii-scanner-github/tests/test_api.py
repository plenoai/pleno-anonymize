"""Tests for the httpx wrapper (api.py) — rate limiting, GraphQL, headers."""

from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner_github.api import (
    DEFAULT_BASE_URL,
    GithubApi,
    GithubApiError,
    graphql_url_for,
)


class TestGraphqlUrlFor:
    def test_dotcom_default(self) -> None:
        assert graphql_url_for(DEFAULT_BASE_URL) == "https://api.github.com/graphql"

    def test_ghes_v3_prefix_rewrite(self) -> None:
        assert graphql_url_for("https://ghe.example.com/api/v3") == (
            "https://ghe.example.com/api/graphql"
        )

    def test_trailing_slash_stripped(self) -> None:
        assert graphql_url_for("https://api.github.com/") == (
            "https://api.github.com/graphql"
        )


class TestHeaders:
    async def test_user_agent_and_api_version_set(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["ua"] = request.headers["User-Agent"]
            seen["accept"] = request.headers["Accept"]
            seen["api_version"] = request.headers["X-GitHub-Api-Version"]
            return httpx.Response(200, json={})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            await api.get("/user")
            assert seen["ua"] == "pleno-pii-scanner-github"
            assert seen["accept"] == "application/vnd.github+json"
            assert seen["api_version"] == "2022-11-28"
        finally:
            await api.aclose()

    async def test_authorization_header_set_when_token_present(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            return httpx.Response(200, json={})

        api = GithubApi(token="ghs_x", transport=httpx.MockTransport(handler))
        try:
            await api.get("/repos/o/r")
            assert seen["auth"] == "Bearer ghs_x"
        finally:
            await api.aclose()

    async def test_no_authorization_header_without_token(self) -> None:
        seen: dict[str, bool] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["has_auth"] = "Authorization" in request.headers
            return httpx.Response(200, json={})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            await api.get("/zen")
            assert seen["has_auth"] is False
        finally:
            await api.aclose()

    async def test_per_request_token_overrides_default(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Authorization"])
            return httpx.Response(200, json={})

        api = GithubApi(token="ghs_default", transport=httpx.MockTransport(handler))
        try:
            await api.get("/a", token="ghs_override")
            await api.get("/b")
            assert seen == ["Bearer ghs_override", "Bearer ghs_default"]
        finally:
            await api.aclose()

    async def test_set_token_swaps_default(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["Authorization"])
            return httpx.Response(200, json={})

        api = GithubApi(token="t1", transport=httpx.MockTransport(handler))
        try:
            await api.get("/a")
            api.set_token("t2")
            await api.get("/b")
            assert seen == ["Bearer t1", "Bearer t2"]
        finally:
            await api.aclose()

    async def test_accept_override_for_raw_blob(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept"] = request.headers["Accept"]
            return httpx.Response(200, json={})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            await api.get("/x", accept="application/vnd.github.v3.raw")
            assert seen["accept"] == "application/vnd.github.v3.raw"
        finally:
            await api.aclose()


class TestPaths:
    async def test_relative_path_joined_to_base_url(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        api = GithubApi(
            base_url="https://ghe.example.com/api/v3",
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/repos/o/r")
            assert seen["url"] == "https://ghe.example.com/api/v3/repos/o/r"
        finally:
            await api.aclose()

    async def test_absolute_url_passed_through(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            await api.get("https://example.com/x")
            assert seen["url"] == "https://example.com/x"
        finally:
            await api.aclose()


class TestRateLimited:
    async def test_429_raises_rate_limited(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "60"})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(RateLimited, match="primary 429"):
                await api.get("/x")
        finally:
            await api.aclose()

    async def test_403_with_retry_after_is_secondary_rate_limit(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, headers={"Retry-After": "120"})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(RateLimited, match="secondary 403"):
                await api.get("/x")
        finally:
            await api.aclose()

    async def test_403_with_remaining_zero_is_quota_exhausted(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1700000000",
                },
            )

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(RateLimited, match="quota exhausted"):
                await api.get("/x")
        finally:
            await api.aclose()

    async def test_403_without_rate_limit_signals_passes_through(self) -> None:
        # A normal 403 (forbidden — wrong scope) is not RateLimited.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"message": "forbidden"})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            response = await api.get("/x")
            assert response.status_code == 403
        finally:
            await api.aclose()


class TestGraphQL:
    async def test_data_payload_returned(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"x": 1}})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            data = await api.graphql("query { x }")
            assert data == {"x": 1}
        finally:
            await api.aclose()

    async def test_variables_sent_in_body(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen.update(json.loads(request.content))
            return httpx.Response(200, json={"data": {}})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            await api.graphql("query { x }", variables={"a": 1})
            assert seen["query"] == "query { x }"
            assert seen["variables"] == {"a": 1}
        finally:
            await api.aclose()

    async def test_graphql_endpoint_is_used(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"data": {}})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            await api.graphql("{ x }")
            assert seen["url"] == "https://api.github.com/graphql"
        finally:
            await api.aclose()

    async def test_graphql_errors_field_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"errors": [{"message": "bad query"}]})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(GithubApiError, match="graphql errors"):
                await api.graphql("{ x }")
        finally:
            await api.aclose()

    async def test_graphql_non_200_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="oops")

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(GithubApiError, match="graphql 500"):
                await api.graphql("{ x }")
        finally:
            await api.aclose()


class TestPost:
    async def test_post_sends_json_body(self) -> None:
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"ok": True})

        api = GithubApi(transport=httpx.MockTransport(handler))
        try:
            await api.post("/path", json={"key": "value"})
            assert seen["body"] == {"key": "value"}
        finally:
            await api.aclose()


class TestProperties:
    async def test_base_url_and_graphql_url_exposed(self) -> None:
        api = GithubApi(base_url="https://ghe.example.com/api/v3")
        try:
            assert api.base_url == "https://ghe.example.com/api/v3"
            assert api.graphql_url == "https://ghe.example.com/api/graphql"
        finally:
            await api.aclose()

    async def test_default_base_url_strips_trailing_slash(self) -> None:
        api = GithubApi(base_url="https://api.github.com/")
        try:
            assert api.base_url == "https://api.github.com"
        finally:
            await api.aclose()
