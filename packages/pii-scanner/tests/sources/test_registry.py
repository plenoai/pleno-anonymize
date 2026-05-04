"""Tests for the connector registry + entry-points discovery."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from importlib.metadata import EntryPoint
from typing import Any

import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    ConnectorSpec,
    Document,
    DocumentChunk,
    DocumentRef,
    DuplicateConnectorError,
    SourceConnector,
    SourceFilter,
    UnknownConnectorError,
    create,
    get,
    list_kinds,
    list_specs,
    register,
    unregister,
)
from pleno_pii_scanner.sources import registry as _registry_mod


class _StubConnector:
    id = "stub:1"
    kind = "stub"

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.config = dict(config)

    async def discover(
        self, filter: SourceFilter, cursor: str | None
    ) -> AsyncIterator[DocumentRef]:
        yield DocumentRef(source_id=self.id, source_kind=self.kind, path="x")

    async def fetch(
        self, ref: DocumentRef
    ) -> AsyncIterator[Document | DocumentChunk]:
        yield Document(ref=ref, text="ok")

    def capabilities(self) -> Capabilities:
        return Capabilities()

    async def close(self) -> None:
        return None


def _stub_factory(config: Mapping[str, Any]) -> SourceConnector:
    return _StubConnector(config)


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch):
    # Each test gets a fresh process-global registry. Otherwise registrations
    # leak between tests and cause flaky DuplicateConnectorError failures.
    # We also default `entry_points` to an empty list so that any third-party
    # connector wheel installed in the workspace (e.g. `pleno-pii-scanner-github`
    # under `[tool.uv.workspace]`) does not pollute the registry. The four
    # discovery tests below override this with their own monkeypatch.setattr.
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()


class TestRegistration:
    def test_register_and_get(self) -> None:
        spec = ConnectorSpec(kind="stub", version="0.0.1", factory=_stub_factory)
        register(spec)
        assert get("stub") is spec

    def test_unknown_kind_raises_with_available_list(self) -> None:
        register(ConnectorSpec(kind="a", version="0.0.1", factory=_stub_factory))
        register(ConnectorSpec(kind="b", version="0.0.1", factory=_stub_factory))
        with pytest.raises(UnknownConnectorError) as exc:
            get("missing")
        msg = str(exc.value)
        assert "missing" in msg
        assert "['a', 'b']" in msg

    def test_unknown_is_keyerror_compatible(self) -> None:
        # Lets callers do `dict-like` exception handling if they prefer.
        with pytest.raises(KeyError):
            get("missing")

    def test_duplicate_registration_raises(self) -> None:
        register(ConnectorSpec(kind="stub", version="0.0.1", factory=_stub_factory))
        with pytest.raises(DuplicateConnectorError) as exc:
            register(
                ConnectorSpec(kind="stub", version="0.0.2", factory=_stub_factory)
            )
        assert "0.0.1" in str(exc.value)
        assert "0.0.2" in str(exc.value)

    def test_replace_overrides_existing(self) -> None:
        register(ConnectorSpec(kind="stub", version="0.0.1", factory=_stub_factory))
        register(
            ConnectorSpec(kind="stub", version="0.0.2", factory=_stub_factory),
            replace=True,
        )
        assert get("stub").version == "0.0.2"

    def test_unregister_is_idempotent(self) -> None:
        register(ConnectorSpec(kind="stub", version="0.0.1", factory=_stub_factory))
        unregister("stub")
        unregister("stub")  # second call must not raise
        with pytest.raises(UnknownConnectorError):
            get("stub")


class TestListing:
    def test_list_kinds_is_sorted(self) -> None:
        register(ConnectorSpec(kind="b", version="0.0.1", factory=_stub_factory))
        register(ConnectorSpec(kind="a", version="0.0.1", factory=_stub_factory))
        register(ConnectorSpec(kind="c", version="0.0.1", factory=_stub_factory))
        assert list_kinds() == ("a", "b", "c")

    def test_list_specs_returns_in_kind_order(self) -> None:
        s_b = ConnectorSpec(kind="b", version="0.0.1", factory=_stub_factory)
        s_a = ConnectorSpec(kind="a", version="0.0.1", factory=_stub_factory)
        register(s_b)
        register(s_a)
        assert list_specs() == (s_a, s_b)


class TestFactory:
    def test_create_invokes_factory_with_config(self) -> None:
        register(ConnectorSpec(kind="stub", version="0.0.1", factory=_stub_factory))
        c = create("stub", {"hello": "world"})
        assert isinstance(c, SourceConnector)
        # _StubConnector echoes the config back so we can verify it flowed
        # through unmodified.
        assert getattr(c, "config") == {"hello": "world"}

    def test_create_propagates_factory_errors(self) -> None:
        def boom(_config: Mapping[str, Any]) -> SourceConnector:
            raise ValueError("bad config")

        register(ConnectorSpec(kind="boom", version="0.0.1", factory=boom))
        with pytest.raises(ValueError, match="bad config"):
            create("boom", {})


class TestEntryPointsDiscovery:
    def test_discover_loads_valid_entry_point(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = ConnectorSpec(kind="ep-stub", version="0.0.1", factory=_stub_factory)
        ep = _make_ep("ep-stub", spec)
        _patch_entry_points(monkeypatch, [ep])

        assert "ep-stub" in list_kinds()
        assert get("ep-stub").version == "0.0.1"

    def test_discover_runs_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def fake_eps(group: str | None = None, **_kwargs: Any):  # noqa: ARG001
            calls["n"] += 1
            return []

        monkeypatch.setattr(_registry_mod, "entry_points", fake_eps)
        list_kinds()
        list_kinds()
        list_kinds()
        assert calls["n"] == 1

    def test_broken_entry_point_logged_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A third-party wheel that crashes on import must not break
        # `pleno-pii-scanner dir <path>`. The kinds it would have
        # registered will fail later as UnknownConnectorError.
        good_spec = ConnectorSpec(kind="good", version="0.0.1", factory=_stub_factory)
        broken = _make_broken_ep("broken")
        good = _make_ep("good", good_spec)
        _patch_entry_points(monkeypatch, [broken, good])

        kinds = list_kinds()
        captured = capsys.readouterr()
        assert "good" in kinds
        assert "broken" not in kinds
        assert "broken" in captured.err

    def test_entry_point_not_a_spec_is_logged_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ep = _make_ep("wrong-type", "not a spec")  # type: ignore[arg-type]
        _patch_entry_points(monkeypatch, [ep])
        list_kinds()
        captured = capsys.readouterr()
        assert "ConnectorSpec" in captured.err

    def test_concurrent_discover_runs_only_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The double-checked lock in _discover_once must keep the inner
        # `return` reachable: a second thread that wakes up after the
        # first finishes should hit the inner self._discovered check and
        # exit without re-running entry_points.
        import threading
        import time

        calls = {"n": 0}
        gate = threading.Event()

        def slow_eps(group: str | None = None, **_kwargs: Any) -> list[EntryPoint]:  # noqa: ARG001
            calls["n"] += 1
            gate.wait(1.0)
            time.sleep(0.01)
            return []

        monkeypatch.setattr(_registry_mod, "entry_points", slow_eps)

        threads = [threading.Thread(target=list_kinds) for _ in range(8)]
        for t in threads:
            t.start()
        # Let all threads queue on the lock, then release.
        time.sleep(0.05)
        gate.set()
        for t in threads:
            t.join(2.0)
            assert not t.is_alive()
        assert calls["n"] == 1

    def test_duplicate_entry_point_logged_not_raised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Two wheels both declare the same kind. First wins, second is logged.
        s1 = ConnectorSpec(kind="dup", version="0.0.1", factory=_stub_factory)
        s2 = ConnectorSpec(kind="dup", version="0.0.2", factory=_stub_factory)
        _patch_entry_points(monkeypatch, [_make_ep("a", s1), _make_ep("b", s2)])
        kinds = list_kinds()
        captured = capsys.readouterr()
        assert "dup" in kinds
        assert "already registered" in captured.err
        # First registration wins.
        assert get("dup").version == "0.0.1"


# --- helpers --------------------------------------------------------------


def _make_ep(name: str, value: object) -> EntryPoint:
    """Build an EntryPoint that returns `value` from .load()."""

    class _FakeEntryPoint:
        def __init__(self) -> None:
            self.name = name
            self.group = _registry_mod.ENTRY_POINT_GROUP

        def load(self) -> object:
            return value

    return _FakeEntryPoint()  # type: ignore[return-value]


def _make_broken_ep(name: str) -> EntryPoint:
    class _BrokenEntryPoint:
        def __init__(self) -> None:
            self.name = name
            self.group = _registry_mod.ENTRY_POINT_GROUP

        def load(self) -> object:
            raise ImportError("simulated broken third-party wheel")

    return _BrokenEntryPoint()  # type: ignore[return-value]


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch, eps: list[EntryPoint]
) -> None:
    def fake(group: str | None = None, **_kwargs: Any) -> list[EntryPoint]:
        if group != _registry_mod.ENTRY_POINT_GROUP:
            return []
        return eps

    monkeypatch.setattr(_registry_mod, "entry_points", fake)
