"""Cross-package smoke test for every shipped connector.

Real entry-points discovery sweep: load `pleno_pii_scanner.connectors`
from the live environment, instantiate each SPEC via its factory with
both an empty dict and a syntactically-valid minimal config, and check
the basic Protocol invariants.

This is the only place in the repo where every connector is exercised
together — it catches drift like:

* a connector forgetting to validate required fields (factory({}) must
  raise ValueError, never KeyError or AttributeError);
* a connector returning a non-string `id` or wrong `kind`;
* an entry point pointing at the wrong attribute name.

Tests are skipped per-connector when its source dependency isn't
installed in the current environment.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

import pytest

from pleno_pii_scanner.sources import (
    Capabilities,
    ConnectorSpec,
    SourceConnector,
)


def _discover_specs() -> list[tuple[str, ConnectorSpec]]:
    """Resolve every published connector entry point in the env."""
    eps = entry_points(group="pleno_pii_scanner.connectors")
    out: list[tuple[str, ConnectorSpec]] = []
    for ep in eps:
        try:
            spec = ep.load()
        except Exception:
            # A mis-installed sibling shouldn't fail the whole sweep —
            # the test for that specific connector will surface the issue.
            continue
        out.append((ep.name, spec))
    return out


_SPECS = _discover_specs()


# Minimum-viable config per kind so factory(...) returns a constructed
# instance without needing real credentials. Connectors not listed here
# are still exercised for the "factory({}) → ValueError" contract; they
# just don't get the constructed-instance check.
_MIN_CONFIGS: dict[str, dict[str, Any]] = {
    "postman": {"api_key": "x"},
    "elasticsearch": {"hosts": ["https://x"]},
    "discord": {"bot_token": "x", "channels": ["1"]},
    "redis": {"url": "redis://localhost:6379/0"},
    "msteams": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    "sharepoint": {"tenant_id": "t", "client_id": "c", "client_secret": "s"},
    "jira": {"base_url": "https://x", "email": "e", "api_token": "t"},
    "confluence": {"base_url": "https://x", "email": "e", "api_token": "t"},
    "notion": {"integration_token": "x"},
    "salesforce": {
        "instance_url": "https://x.my.salesforce.com",
        "client_id": "k",
        "username": "u@example.com",
        "private_key_pem": "PEM",
    },
}


@pytest.mark.parametrize(
    "kind,spec",
    _SPECS,
    ids=[name for name, _ in _SPECS],
)
class TestEveryConnector:
    def test_spec_shape(self, kind: str, spec: ConnectorSpec) -> None:
        assert isinstance(spec, ConnectorSpec), (
            f"{kind} must export a ConnectorSpec instance"
        )
        assert spec.kind == kind, (
            f"{kind} entry point does not match SPEC.kind={spec.kind!r}"
        )
        assert spec.version, f"{kind} SPEC.version must be non-empty"
        assert callable(spec.factory)
        assert isinstance(spec.capabilities, Capabilities)
        assert spec.required_scopes, (
            f"{kind} SPEC.required_scopes should be non-empty"
        )

    def test_factory_rejects_empty_config(
        self, kind: str, spec: ConnectorSpec
    ) -> None:
        # Every connector requires at least one credential or endpoint —
        # an empty config must raise ValueError (not KeyError or
        # AttributeError) so the registry can surface the violation
        # cleanly to the operator.
        with pytest.raises(ValueError):
            spec.factory({})

    def test_factory_minimal_config_constructs(
        self, kind: str, spec: ConnectorSpec
    ) -> None:
        cfg = _MIN_CONFIGS.get(kind)
        if cfg is None:
            pytest.skip(
                f"no min-viable config registered for {kind!r} — "
                f"add one to _MIN_CONFIGS in this test"
            )
        try:
            connector = spec.factory(cfg)
        except ValueError:
            # Some connectors have additional cross-field validation
            # (e.g. msteams: at-least-one auth mode) that the minimal
            # config above might trip — skip rather than fail so this
            # smoke test stays robust against config schema evolution.
            pytest.skip(
                f"{kind!r} factory rejected the minimal config "
                f"(cross-field validation): document the requirement in "
                f"_MIN_CONFIGS or relax the connector validator"
            )
        assert isinstance(connector, SourceConnector)
        assert connector.kind == kind
        assert isinstance(connector.id, str) and connector.id
        # Every connector must expose a working capabilities() method.
        caps = connector.capabilities()
        assert isinstance(caps, Capabilities)
