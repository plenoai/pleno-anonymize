"""Tests for the httpx wrapper (api.py): headers, pagination, rate limit."""

from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner.scheduler.rate_limit import RateLimited
from pleno_pii_scanner_gitlab.api import (
    DEFAULT_BASE_URL,
    GitlabApi,
)
from pleno_pii_scanner_gitlab.auth import GitlabAuthMode


def _api(
    handler,
    *,
    auth_mode: GitlabAuthMode = GitlabAuthMode.PAT,
    token: str = "glpat-test",
    base_url: str = DEFAULT_BASE_URL,
) -> GitlabApi:
    return GitlabApi(
        base_url=base_url,
        auth_mode=auth_mode,
        token=token,
        transport=httpx.MockTransport(handler),
    )


class TestHeaders:
    async def test_pat_sends_private_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return httpx.Response(200, json={})

        api = _api(handler, auth_mode=GitlabAuthMode.PAT, token="glpat-x")
        try:
            await api.get("/projects")
            assert seen.get("private-token") == "glpat-x"
            assert seen.get("user-agent") == "pleno-pii-scanner-gitlab"
            assert seen.get("accept") == "application/json"
        finally:
            await api.aclose()

    async def test_oauth_sends_bearer(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return httpx.Response(200, json={})

        api = _api(handler, auth_mode=GitlabAuthMode.OAUTH, token="abc")
        try:
            await api.get("/projects")
            assert seen.get("authorization") == "Bearer abc"
            # The PAT header must be absent in OAuth mode.
            assert "private-token" not in seen
        finally:
            await api.aclose()

    async def test_project_token_uses_private_token(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.headers))
            return httpx.Response(200, json={})

        api = _api(handler, auth_mode=GitlabAuthMode.PROJECT, token="ptok")
        try:
            await api.get("/projects")
            assert seen.get("private-token") == "ptok"
        finally:
            await api.aclose()


class TestPaths:
    async def test_relative_path_joined_to_v4_prefix(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        api = _api(handler, base_url="https://gitlab.example.com")
        try:
            await api.get("/projects/42")
            assert seen["url"] == "https://gitlab.example.com/api/v4/projects/42"
        finally:
            await api.aclose()

    async def test_absolute_url_passed_through(self) -> None:
        # GitLab's Link header returns absolute URLs; we must not
        # double-prefix `/api/v4` when following them.
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={})

        api = _api(handler)
        try:
            await api.get("https://gitlab.com/api/v4/projects?page=2")
            assert seen["url"] == "https://gitlab.com/api/v4/projects?page=2"
        finally:
            await api.aclose()

    async def test_trailing_slash_stripped_from_base_url(self) -> None:
        api = _api(lambda _: httpx.Response(200), base_url="https://gitlab.com/")
        try:
            assert api.base_url == "https://gitlab.com"
        finally:
            await api.aclose()


class TestProperties:
    async def test_auth_mode_exposed(self) -> None:
        api = _api(lambda _: httpx.Response(200), auth_mode=GitlabAuthMode.OAUTH)
        try:
            assert api.auth_mode is GitlabAuthMode.OAUTH
        finally:
            await api.aclose()


class TestRateLimited:
    async def test_429_raises(self) -> None:
        api = _api(lambda _: httpx.Response(429, headers={"Retry-After": "30"}))
        try:
            with pytest.raises(RateLimited, match="429"):
                await api.get("/projects")
        finally:
            await api.aclose()

    async def test_403_with_modern_remaining_zero_raises(self) -> None:
        # GitLab >= 13.0 emits unprefixed RateLimit-* headers.
        api = _api(
            lambda _: httpx.Response(
                403,
                headers={
                    "RateLimit-Remaining": "0",
                    "RateLimit-Reset": "1700000000",
                },
            )
        )
        try:
            with pytest.raises(RateLimited, match="quota exhausted"):
                await api.get("/projects")
        finally:
            await api.aclose()

    async def test_403_with_legacy_x_remaining_zero_raises(self) -> None:
        # GitLab < 13.0 still emitted X-RateLimit-* on some endpoints.
        api = _api(
            lambda _: httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1700000000",
                },
            )
        )
        try:
            with pytest.raises(RateLimited, match="quota exhausted"):
                await api.get("/projects")
        finally:
            await api.aclose()

    async def test_plain_403_passes_through(self) -> None:
        # A 403 without a rate-limit signal is a permission failure;
        # surfacing it as RateLimited would lock the scheduler in a
        # retry loop against an auth problem that needs a human.
        api = _api(lambda _: httpx.Response(403, json={"message": "forbidden"}))
        try:
            response = await api.get("/projects")
            assert response.status_code == 403
        finally:
            await api.aclose()


class TestParseNextLink:
    def test_returns_url_for_rel_next(self) -> None:
        response = httpx.Response(
            200,
            headers={
                "Link": (
                    '<https://gitlab.com/api/v4/projects?page=2>; rel="next", '
                    '<https://gitlab.com/api/v4/projects?page=10>; rel="last"'
                )
            },
        )
        assert (
            GitlabApi.parse_next_link(response)
            == "https://gitlab.com/api/v4/projects?page=2"
        )

    def test_returns_none_when_header_absent(self) -> None:
        assert GitlabApi.parse_next_link(httpx.Response(200)) is None

    def test_returns_none_on_last_page_only_links(self) -> None:
        # GitLab returns a `rel="prev"` only on intermediate pages; the
        # final page may carry only `rel="first"` and `rel="prev"`.
        response = httpx.Response(
            200,
            headers={
                "Link": (
                    '<https://gitlab.com/api/v4/projects?page=1>; rel="first", '
                    '<https://gitlab.com/api/v4/projects?page=4>; rel="prev"'
                )
            },
        )
        assert GitlabApi.parse_next_link(response) is None

    def test_skips_malformed_entries(self) -> None:
        # Garbage between two well-formed entries must not derail parse.
        response = httpx.Response(
            200,
            headers={
                "Link": (
                    "garbage_no_brackets, "
                    '<https://gitlab.com/api/v4/projects?page=2>; rel="next"'
                )
            },
        )
        assert (
            GitlabApi.parse_next_link(response)
            == "https://gitlab.com/api/v4/projects?page=2"
        )

    def test_skips_entry_missing_rel_attr(self) -> None:
        # Entry without any attribute (just `<url>`) is malformed; skip.
        response = httpx.Response(
            200, headers={"Link": "<https://gitlab.com/api/v4/projects?page=2>"}
        )
        assert GitlabApi.parse_next_link(response) is None

    def test_skips_entry_with_only_other_rel(self) -> None:
        # Only a `rel="prev"` entry — no `next`, return None.
        response = httpx.Response(
            200,
            headers={"Link": '<https://gitlab.com/api/v4/projects?page=1>; rel="prev"'},
        )
        assert GitlabApi.parse_next_link(response) is None

    def test_handles_unbracketed_url_as_malformed(self) -> None:
        # A URL without `<>` brackets is not RFC 5988 compliant; bail.
        response = httpx.Response(
            200,
            headers={"Link": 'https://gitlab.com/api/v4/projects?page=2; rel="next"'},
        )
        assert GitlabApi.parse_next_link(response) is None


class TestVerify:
    async def test_default_base_url(self) -> None:
        # Smoke check: api defaults route to gitlab.com.
        api = _api(lambda _: httpx.Response(200, json={}))
        try:
            assert api.base_url == "https://gitlab.com"
        finally:
            await api.aclose()

    async def test_verify_arg_not_passed_when_transport_given(self) -> None:
        # MockTransport short-circuits TLS entirely; we use this fact in
        # the rest of the suite to avoid touching real CAs. Sanity-check
        # that the verify path is the one not exercised here.
        api = GitlabApi(
            auth_mode=GitlabAuthMode.PAT,
            token="x",
            transport=httpx.MockTransport(lambda _: httpx.Response(200)),
            verify="/nonexistent/path",  # ignored when transport is provided
        )
        try:
            response = await api.get("/x")
            assert response.status_code == 200
        finally:
            await api.aclose()
