"""Shared test fixtures: a fake AsyncWebClient that doesn't hit Slack."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class FakeResponse:
    """Mimics slack-sdk's `SlackResponse`.

    Real SlackResponse has `.data` (dict), `.status_code`, `.headers`,
    behaves like a Mapping via `__getitem__` / `.get()`. We implement
    the slim subset the connector code actually touches.
    """

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        status_code: int = 200,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.data = dict(payload)
        self.status_code = status_code
        self.headers = dict(headers or {})

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __contains__(self, key: object) -> bool:
        return key in self.data


class FakeAsyncWebClient:
    """In-memory AsyncWebClient stand-in.

    Tests register canned responses keyed by `(method_name, frozenset(kwargs.items()))`
    or by method name alone. Each call records its kwargs so tests can
    assert on pagination / oldest forwarding.
    """

    def __init__(self) -> None:
        # method -> list[FakeResponse | Exception]; each call pops index
        # 0 so a method with two pages can return them in order.
        self._scripted: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def script(self, method: str, response: Any) -> None:
        """Append `response` to the queue for `method`."""
        self._scripted.setdefault(method, []).append(response)

    async def _invoke(self, method: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, kwargs))
        queue = self._scripted.get(method)
        if not queue:
            raise AssertionError(
                f"FakeAsyncWebClient: no scripted response for {method!r}; "
                f"kwargs={kwargs!r}; scripted methods={list(self._scripted)}"
            )
        nxt = queue.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        if not isinstance(nxt, FakeResponse):
            nxt = FakeResponse(nxt)
        return nxt

    # AsyncWebClient methods used by the connector. We only implement
    # those that get touched in tests; an unknown method falls through to
    # _invoke via __getattr__.
    async def auth_test(self, **kw: Any) -> FakeResponse:
        return await self._invoke("auth_test", **kw)

    async def conversations_list(self, **kw: Any) -> FakeResponse:
        return await self._invoke("conversations_list", **kw)

    async def conversations_history(self, **kw: Any) -> FakeResponse:
        return await self._invoke("conversations_history", **kw)

    async def conversations_replies(self, **kw: Any) -> FakeResponse:
        return await self._invoke("conversations_replies", **kw)

    async def files_info(self, **kw: Any) -> FakeResponse:
        return await self._invoke("files_info", **kw)

    async def users_info(self, **kw: Any) -> FakeResponse:
        return await self._invoke("users_info", **kw)

    async def discovery_enterprise_info(self, **kw: Any) -> FakeResponse:
        return await self._invoke("discovery_enterprise_info", **kw)

    async def discovery_conversations_list(self, **kw: Any) -> FakeResponse:
        return await self._invoke("discovery_conversations_list", **kw)

    async def discovery_conversations_history(self, **kw: Any) -> FakeResponse:
        return await self._invoke("discovery_conversations_history", **kw)

    async def close(self) -> None:
        self.closed = True


def make_client_factory(client: FakeAsyncWebClient):
    """Return a `client_factory` that always returns the provided fake."""

    def factory(*, token: str, timeout: float) -> FakeAsyncWebClient:  # noqa: ARG001
        return client

    return factory
