"""Tests for the httpx api wrapper — auth, pagination, 429 backoff."""

from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner_ci_logs.api import (
    DEFAULT_BUILDKITE_BASE_URL,
    DEFAULT_CIRCLECI_BASE_URL,
    DEFAULT_GITHUB_ACTIONS_BASE_URL,
    BasicAuth,
    BearerAuth,
    CircleTokenAuth,
    CiLogsApi,
    CiLogsApiError,
    _link_next,
    _retry_after_seconds,
)


# ---------------------------------------------------------------------
# Auth header construction
# ---------------------------------------------------------------------


class TestAuthHeaders:
    def test_basic_auth_header_value(self) -> None:
        # base64("build:secret") = "YnVpbGQ6c2VjcmV0"
        auth = BasicAuth(username="build", password="secret")
        assert auth.header_value() == "Basic YnVpbGQ6c2VjcmV0"

    def test_bearer_auth_header_value(self) -> None:
        assert BearerAuth(token="t1").header_value() == "Bearer t1"

    def test_circle_token_auth_header_value(self) -> None:
        # CircleCI uses the bare token (no `Bearer` prefix).
        assert CircleTokenAuth(token="ct").header_value() == "ct"

    async def test_bearer_uses_authorization_header(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            seen["circle"] = request.headers.get("Circle-Token", "")
            seen["accept"] = request.headers["Accept"]
            seen["ua"] = request.headers["User-Agent"]
            return httpx.Response(200, json={"workflow_runs": []})

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="ghp_xxx"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/repos/o/r/actions/runs", params={"per_page": 1})
            assert seen["auth"] == "Bearer ghp_xxx"
            assert seen["circle"] == ""
            assert seen["accept"] == "application/json"
            assert seen["ua"] == "pleno-pii-scanner-ci-logs"
        finally:
            await api.aclose()

    async def test_circle_token_uses_custom_header(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization", "")
            seen["circle"] = request.headers.get("Circle-Token", "")
            return httpx.Response(200, json={"items": []})

        api = CiLogsApi(
            flavor="circleci",
            base_url=DEFAULT_CIRCLECI_BASE_URL,
            auth=CircleTokenAuth(token="cci"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/project/gh/o/r/job")
            # CircleCI uses Circle-Token, never Authorization.
            assert seen["auth"] == ""
            assert seen["circle"] == "cci"
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------


class TestConstruction:
    def test_unsupported_flavor_rejected(self) -> None:
        with pytest.raises(ValueError, match="unsupported ci_logs flavor"):
            CiLogsApi(
                flavor="travisci",  # type: ignore[arg-type]
                base_url="https://x",
                auth=BearerAuth(token="t"),
            )

    async def test_base_url_strip_trailing_slash(self) -> None:
        api = CiLogsApi(
            flavor="buildkite",
            base_url="https://api.buildkite.com/v2/",
            auth=BearerAuth(token="t"),
        )
        try:
            assert api.base_url == "https://api.buildkite.com/v2"
            assert api.flavor == "buildkite"
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------


class TestRateLimit:
    async def test_429_then_success_returns_response(self) -> None:
        calls = {"count": 0}
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        def handler(_: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(429, headers={"Retry-After": "5"})
            return httpx.Response(200, json={"items": []})

        api = CiLogsApi(
            flavor="circleci",
            base_url=DEFAULT_CIRCLECI_BASE_URL,
            auth=CircleTokenAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            response = await api.get("/project/gh/o/r/job")
            assert response.status_code == 200
            assert slept == [5.0]
        finally:
            await api.aclose()

    async def test_429_twice_raises_rate_limited(self) -> None:
        async def fake_sleep(_: float) -> None:
            return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "1"})

        api = CiLogsApi(
            flavor="buildkite",
            base_url=DEFAULT_BUILDKITE_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(RateLimited, match="ci_logs"):
                await api.get("/x")
        finally:
            await api.aclose()

    async def test_gha_secondary_403_rate_limit_signal(self) -> None:
        # 403 with `X-RateLimit-Remaining: 0` is GHA's secondary
        # limiter — must back off, not be treated as auth failure.
        async def fake_sleep(_: float) -> None:
            return None

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "10"},
            )

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(RateLimited):
                await api.get("/repos/o/r/actions/runs")
        finally:
            await api.aclose()

    def test_retry_after_seconds_default_on_missing(self) -> None:
        response = httpx.Response(429)
        assert _retry_after_seconds(response) == 30.0

    def test_retry_after_seconds_unparseable_falls_back(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "not-a-number"})
        assert _retry_after_seconds(response) == 30.0

    def test_retry_after_seconds_clamps_negative(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "-5"})
        assert _retry_after_seconds(response) == 0.0

    def test_retry_after_seconds_parses_integer(self) -> None:
        response = httpx.Response(429, headers={"Retry-After": "12"})
        assert _retry_after_seconds(response) == 12.0

    def test_retry_after_uses_x_ratelimit_reset_when_no_retry_after(self) -> None:
        response = httpx.Response(403, headers={"X-RateLimit-Reset": "60"})
        assert _retry_after_seconds(response) == 60.0

    def test_retry_after_x_ratelimit_reset_clamped(self) -> None:
        # Out-of-range epoch values are clamped to 4× the default
        # floor so a buggy upstream cannot make us sleep for hours.
        response = httpx.Response(403, headers={"X-RateLimit-Reset": "9999999999"})
        assert _retry_after_seconds(response) == 30.0 * 4

    def test_retry_after_x_ratelimit_reset_unparseable(self) -> None:
        response = httpx.Response(403, headers={"X-RateLimit-Reset": "junk"})
        assert _retry_after_seconds(response) == 30.0


# ---------------------------------------------------------------------
# Link header parser
# ---------------------------------------------------------------------


class TestLinkParser:
    def test_extracts_next_url(self) -> None:
        header = (
            '<https://api.buildkite.com/v2/p?page=2>; rel="next", '
            '<https://api.buildkite.com/v2/p?page=10>; rel="last"'
        )
        assert _link_next(header) == "https://api.buildkite.com/v2/p?page=2"

    def test_returns_none_when_no_next(self) -> None:
        assert _link_next('<https://x>; rel="last"') is None

    def test_returns_none_on_none_header(self) -> None:
        assert _link_next(None) is None

    def test_returns_none_on_empty(self) -> None:
        assert _link_next("") is None


# ---------------------------------------------------------------------
# Pagination — GitHub Actions
# ---------------------------------------------------------------------


class TestGithubActionsPagination:
    async def test_walks_pages_until_short_page(self) -> None:
        pages = iter(
            [
                {
                    "workflow_runs": [{"id": i} for i in range(2)],
                    "total_count": 999,
                },
                {
                    "workflow_runs": [{"id": 2}],
                    "total_count": 999,
                },
            ]
        )
        seen_pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_pages.append(str(request.url.query))
            return httpx.Response(200, json=next(pages))

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            ids = [
                e["id"]
                async for e in api.paginate("/repos/o/r/actions/runs", page_size=2)
            ]
            assert ids == [0, 1, 2]
            assert "page=1" in seen_pages[0]
            assert "page=2" in seen_pages[1]
            assert "per_page=2" in seen_pages[0]
        finally:
            await api.aclose()

    async def test_non_200_yields_empty(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            entries = [e async for e in api.paginate("/repos/x/y/actions/runs")]
            assert entries == []
        finally:
            await api.aclose()

    async def test_malformed_json_yields_empty(self) -> None:
        # A CDN serving HTML on a JSON path must not crash the scan.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html>maintenance</html>",
                headers={"Content-Type": "text/html"},
            )

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            entries = [e async for e in api.paginate("/repos/o/r/actions/runs")]
            assert entries == []
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Pagination — CircleCI
# ---------------------------------------------------------------------


class TestCircleCiPagination:
    async def test_walks_page_token_until_absent(self) -> None:
        pages = iter(
            [
                {
                    "items": [{"job_number": 1}, {"job_number": 2}],
                    "next_page_token": "tok-2",
                },
                {"items": [{"job_number": 3}], "next_page_token": None},
            ]
        )
        seen_qs: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_qs.append(str(request.url.query))
            return httpx.Response(200, json=next(pages))

        api = CiLogsApi(
            flavor="circleci",
            base_url=DEFAULT_CIRCLECI_BASE_URL,
            auth=CircleTokenAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            jobs = [j["job_number"] async for j in api.paginate("/project/gh/o/r/job")]
            assert jobs == [1, 2, 3]
            assert "page-token" not in seen_qs[0]
            assert "page-token=tok-2" in seen_qs[1]
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Pagination — Buildkite
# ---------------------------------------------------------------------


class TestBuildkitePagination:
    async def test_walks_link_header_until_absent(self) -> None:
        responses = iter(
            [
                httpx.Response(
                    200,
                    json=[{"number": 1}, {"number": 2}],
                    headers={
                        "Link": (
                            "<https://api.buildkite.com/v2/"
                            "organizations/o/pipelines/p/builds?page=2>; "
                            'rel="next"'
                        )
                    },
                ),
                httpx.Response(200, json=[{"number": 3}]),
            ]
        )
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return next(responses)

        api = CiLogsApi(
            flavor="buildkite",
            base_url=DEFAULT_BUILDKITE_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            builds = [
                e["number"]
                async for e in api.paginate("/organizations/o/pipelines/p/builds")
            ]
            assert builds == [1, 2, 3]
            # Second URL must be the absolute one from Link rel=next.
            assert seen_urls[1].startswith(
                "https://api.buildkite.com/v2/organizations/o/pipelines/p/builds"
            )
        finally:
            await api.aclose()

    async def test_non_list_body_yields_empty(self) -> None:
        # Buildkite expects a JSON list; a dict means the proxy
        # rewrote our endpoint — refuse to interpret rather than
        # silently emit garbage refs.
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": "shape"})

        api = CiLogsApi(
            flavor="buildkite",
            base_url=DEFAULT_BUILDKITE_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            entries = [
                e async for e in api.paginate("/organizations/o/pipelines/p/builds")
            ]
            assert entries == []
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# Pagination guards
# ---------------------------------------------------------------------


class TestPaginationGuards:
    async def test_jenkins_paginate_rejected(self) -> None:
        api = CiLogsApi(
            flavor="jenkins",
            base_url="https://j.local",
            auth=BasicAuth(username="u", password="p"),
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        )
        try:
            with pytest.raises(
                CiLogsApiError, match="jenkins flavor does not paginate"
            ):
                async for _ in api.paginate("/api/json"):
                    pass
        finally:
            await api.aclose()

    async def test_infinite_pagination_caught(self, monkeypatch) -> None:
        # Patch the depth ceiling so the test does not need 10k pages.
        from pleno_pii_scanner_ci_logs import api as api_mod

        monkeypatch.setattr(api_mod, "_MAX_PAGINATION_DEPTH", 3)

        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "items": [{"job_number": 1}],
                    "next_page_token": "loop",
                },
            )

        api = api_mod.CiLogsApi(
            flavor="circleci",
            base_url=DEFAULT_CIRCLECI_BASE_URL,
            auth=CircleTokenAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(CiLogsApiError, match="pagination exceeded"):
                async for _ in api.paginate("/project/gh/o/r/job"):
                    pass
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# get_bytes (zip log fetch)
# ---------------------------------------------------------------------


class TestGetBytes:
    async def test_returns_response_content(self) -> None:
        seen: dict[str, str] = {}
        body = b"PK\x03\x04binary"

        def handler(request: httpx.Request) -> httpx.Response:
            seen["accept"] = request.headers["Accept"]
            return httpx.Response(200, content=body)

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            blob = await api.get_bytes("/repos/o/r/actions/runs/1/logs")
            assert blob == body
            assert seen["accept"] == "application/zip"
        finally:
            await api.aclose()

    async def test_non_200_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"")

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(CiLogsApiError):
                await api.get_bytes("/x")
        finally:
            await api.aclose()


# ---------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------


class TestAbsoluteUrl:
    async def test_relative_path_joined_to_base_url(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"workflow_runs": []})

        api = CiLogsApi(
            flavor="github_actions",
            base_url=DEFAULT_GITHUB_ACTIONS_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("repos/o/r/actions/runs")
            assert seen["url"].startswith(
                "https://api.github.com/repos/o/r/actions/runs"
            )
        finally:
            await api.aclose()

    async def test_absolute_url_passes_through(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=[])

        api = CiLogsApi(
            flavor="buildkite",
            base_url=DEFAULT_BUILDKITE_BASE_URL,
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("https://other.host/api/v2/x")
            assert seen["url"].startswith("https://other.host/api/v2/x")
        finally:
            await api.aclose()
