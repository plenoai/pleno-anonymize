"""Hermetic tests for JiraApi (auth header, throttle, error path)."""

from __future__ import annotations

import httpx
import pytest

from pleno_pii_scanner_jira.api import (
    BasicAuth,
    BearerAuth,
    JiraApi,
    JiraApiError,
)


class TestAuthHeaders:
    def test_basic_header_format(self) -> None:
        auth = BasicAuth(username="alice@acme", password="tok")
        # `Basic ` + base64("alice@acme:tok")
        assert auth.header_value().startswith("Basic ")
        # Defensive: secret value never appears in plaintext in header.
        assert "tok" not in auth.header_value()

    def test_bearer_header_format(self) -> None:
        assert BearerAuth(token="abc").header_value() == "Bearer abc"


class TestApiPrefix:
    async def test_cloud_uses_v3(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request.url.path)
            return httpx.Response(200, json={"ok": True})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/myself")
            assert observed == ["/rest/api/3/myself"]
        finally:
            await api.aclose()

    async def test_datacenter_uses_v2(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request.url.path)
            return httpx.Response(200, json={"ok": True})

        api = JiraApi(
            flavor="datacenter",
            base_url="https://jira.acme.internal",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/myself")
            assert observed == ["/rest/api/2/myself"]
        finally:
            await api.aclose()

    async def test_rest_path_passes_through(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request.url.path)
            return httpx.Response(200, json={})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/rest/auth/1/session")
            assert observed == ["/rest/auth/1/session"]
        finally:
            await api.aclose()

    async def test_path_without_leading_slash_normalised(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(request.url.path)
            return httpx.Response(200, json={})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("myself")  # no leading slash
            assert observed == ["/rest/api/3/myself"]
        finally:
            await api.aclose()

    async def test_absolute_url_passes_through(self) -> None:
        observed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(str(request.url))
            return httpx.Response(200, json={})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("https://other.example/x")
            assert observed == ["https://other.example/x"]
        finally:
            await api.aclose()

    def test_properties_exposed(self) -> None:
        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net/",
            auth=BearerAuth(token="t"),
        )
        try:
            assert api.flavor == "cloud"
            assert api.base_url == "https://acme.atlassian.net"
            assert api.api_prefix == "/rest/api/3"
        finally:
            import asyncio

            asyncio.get_event_loop().run_until_complete(api.aclose()) if False else None


class TestRejectedFlavor:
    def test_invalid_flavor(self) -> None:
        with pytest.raises(ValueError, match="unsupported jira flavor"):
            JiraApi(
                flavor="server",  # type: ignore[arg-type]
                base_url="x",
                auth=BearerAuth(token="t"),
            )


class TestErrorMapping:
    async def test_404_returns_empty_dict(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"errorMessages": ["nope"]})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            assert await api.get("/issue/X-1") == {}
        finally:
            await api.aclose()

    async def test_400_raises(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(400, text="bad jql")

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(JiraApiError, match="400"):
                await api.get("/search")
        finally:
            await api.aclose()

    async def test_no_token_leak_in_error_message(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal")

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="super-secret-token-xyz"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(JiraApiError) as exc:
                await api.get("/myself")
            assert "super-secret-token-xyz" not in str(exc.value)
        finally:
            await api.aclose()


class TestThrottle:
    async def test_429_then_200(self) -> None:
        attempts = {"n": 0}
        slept: list[float] = []

        async def fake_sleep(t: float) -> None:
            slept.append(t)

        def handler(_r: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0.01"})
            return httpx.Response(200, json={"ok": True})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            body = await api.get("/myself")
            assert body == {"ok": True}
            assert slept == [0.01]
        finally:
            await api.aclose()

    async def test_503_dc_then_200(self) -> None:
        attempts = {"n": 0}

        async def fake_sleep(_t: float) -> None:
            return None

        def handler(_r: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(503, headers={"Retry-After": "0.01"})
            return httpx.Response(200, json={"ok": True})

        api = JiraApi(
            flavor="datacenter",
            base_url="https://jira.acme.internal",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            body = await api.get("/myself")
            assert body == {"ok": True}
        finally:
            await api.aclose()

    async def test_503_cloud_propagates(self) -> None:
        # Cloud flavor must NOT treat 503 as a throttle; it should raise.
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream")

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(JiraApiError):
                await api.get("/myself")
        finally:
            await api.aclose()

    async def test_persistent_throttle_propagates(self) -> None:
        async def fake_sleep(_t: float) -> None:
            return None

        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "0.01"})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            with pytest.raises(JiraApiError, match="persistent throttle"):
                await api.get("/myself")
        finally:
            await api.aclose()

    async def test_retry_after_missing_uses_default(self) -> None:
        slept: list[float] = []

        async def fake_sleep(t: float) -> None:
            slept.append(t)

        attempts = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            await api.get("/myself")
            assert slept and slept[0] == 30.0
        finally:
            await api.aclose()

    async def test_retry_after_capped(self) -> None:
        slept: list[float] = []

        async def fake_sleep(t: float) -> None:
            slept.append(t)

        attempts = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "3600"})
            return httpx.Response(200, json={})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            await api.get("/myself")
            assert slept[0] == 60.0
        finally:
            await api.aclose()

    async def test_retry_after_garbage_uses_default(self) -> None:
        slept: list[float] = []

        async def fake_sleep(t: float) -> None:
            slept.append(t)

        attempts = {"n": 0}

        def handler(_r: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(
                    429, headers={"Retry-After": "not-a-number"}
                )
            return httpx.Response(200, json={})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
        )
        try:
            await api.get("/myself")
            assert slept[0] == 30.0
        finally:
            await api.aclose()


class TestParamsSanitisation:
    async def test_drops_none_param_values(self) -> None:
        observed: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed.append(dict(request.url.params))
            return httpx.Response(200, json={})

        api = JiraApi(
            flavor="cloud",
            base_url="https://acme.atlassian.net",
            auth=BearerAuth(token="t"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await api.get("/x", params={"a": "1", "b": None})
            assert observed == [{"a": "1"}]
        finally:
            await api.aclose()
