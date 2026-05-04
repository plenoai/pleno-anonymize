"""Connector registry + entry-points discovery.

Third-party connector wheels (`pleno-pii-scanner-aws`,
`pleno-pii-scanner-slack`, etc.) declare a setuptools entry point in the
`pleno_pii_scanner.connectors` group. The registry imports them lazily —
the SDK is loaded only when a scan actually targets that kind, so
`pleno-pii-scanner dir <path>` does not pull boto3 into memory.

See ADR-0007 §2.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from threading import RLock
from typing import Any

from pleno_pii_scanner.sources.base import Capabilities, SourceConnector

# Group name used in third-party `pyproject.toml`:
#
#   [project.entry-points."pleno_pii_scanner.connectors"]
#   aws-s3 = "pleno_pii_scanner_aws.s3:Spec"
#
# The value resolves to a `ConnectorSpec` instance.
ENTRY_POINT_GROUP = "pleno_pii_scanner.connectors"

ConnectorFactory = Callable[[Mapping[str, Any]], SourceConnector]


class ConnectorError(Exception):
    """Base for registry errors."""


class UnknownConnectorError(ConnectorError, KeyError):
    """Raised when a connector kind is requested that nothing has registered."""


class DuplicateConnectorError(ConnectorError, ValueError):
    """Raised when two registrations claim the same kind.

    Indicates a packaging bug — a deployment ships two wheels both
    declaring `aws-s3`, for example. Loud failure beats silent override.
    """


@dataclass(frozen=True, slots=True)
class ConnectorSpec:
    """Static description of a connector that the CLI can render.

    Every wheel exports one `ConnectorSpec` per kind via entry-points.
    The `factory` is called with a per-source config dict (validated
    against `config_schema` if provided) and returns a configured
    `SourceConnector` instance. Credentials are not part of the config
    schema — they flow through CredentialBroker (#5).
    """

    kind: str
    version: str
    factory: ConnectorFactory
    capabilities: Capabilities = field(default_factory=Capabilities)
    required_scopes: tuple[str, ...] = ()
    config_schema: Mapping[str, Any] | None = None
    description: str = ""


class _Registry:
    """Process-global lazy registry.

    Mutation is rare (registration + entry-points discovery) and
    thread-safe via RLock. Lookups are lock-free dict reads — the
    dict is replaced atomically when entry-points are first imported.
    """

    def __init__(self) -> None:
        self._specs: dict[str, ConnectorSpec] = {}
        self._lock = RLock()
        self._discovered = False

    def register(self, spec: ConnectorSpec, *, replace: bool = False) -> None:
        """Add a spec.

        `replace=True` is intended for tests that want to inject a fake
        connector. Production code never replaces — duplicates indicate
        a packaging bug and must surface loudly.
        """
        with self._lock:
            if not replace and spec.kind in self._specs:
                existing = self._specs[spec.kind]
                raise DuplicateConnectorError(
                    f"connector kind {spec.kind!r} already registered "
                    f"(existing version={existing.version}, new version={spec.version})"
                )
            self._specs[spec.kind] = spec

    def unregister(self, kind: str) -> None:
        """Remove a spec. Mostly for test cleanup."""
        with self._lock:
            self._specs.pop(kind, None)

    def get(self, kind: str) -> ConnectorSpec:
        self._discover_once()
        try:
            return self._specs[kind]
        except KeyError:
            raise UnknownConnectorError(
                f"unknown connector kind: {kind!r}. "
                f"Available: {sorted(self._specs)}"
            ) from None

    def list_kinds(self) -> tuple[str, ...]:
        """All registered kinds, sorted, after lazy entry-points discovery."""
        self._discover_once()
        return tuple(sorted(self._specs))

    def list_specs(self) -> tuple[ConnectorSpec, ...]:
        self._discover_once()
        return tuple(self._specs[k] for k in sorted(self._specs))

    def create(self, kind: str, config: Mapping[str, Any]) -> SourceConnector:
        """Instantiate a connector via the registered factory.

        Raises UnknownConnectorError if the kind is not registered, or
        whatever the factory raises (typically pydantic ValidationError
        when the config doesn't match `config_schema`).
        """
        spec = self.get(kind)
        return spec.factory(config)

    def _discover_once(self) -> None:
        if self._discovered:
            return
        with self._lock:
            if self._discovered:
                return
            self._import_entry_points()
            self._discovered = True

    def _import_entry_points(self) -> None:
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
            except Exception as exc:
                # A broken third-party wheel must not bring down the whole
                # scanner. Log to stderr and continue — the kinds it would
                # have registered will surface as UnknownConnectorError
                # when something tries to use them.
                print(
                    f"pleno-pii-scanner: failed to load connector entry point "
                    f"{ep.name!r}: {exc!r}",
                    file=sys.stderr,
                )
                continue
            if not isinstance(obj, ConnectorSpec):
                print(
                    f"pleno-pii-scanner: entry point {ep.name!r} did not "
                    f"resolve to a ConnectorSpec (got {type(obj).__name__})",
                    file=sys.stderr,
                )
                continue
            try:
                self.register(obj)
            except DuplicateConnectorError as exc:
                print(f"pleno-pii-scanner: {exc}", file=sys.stderr)


_GLOBAL = _Registry()


def register(spec: ConnectorSpec, *, replace: bool = False) -> None:
    """Register `spec` in the process-global registry."""
    _GLOBAL.register(spec, replace=replace)


def unregister(kind: str) -> None:
    """Remove `kind` from the process-global registry."""
    _GLOBAL.unregister(kind)


def get(kind: str) -> ConnectorSpec:
    """Return the spec for `kind`, raising UnknownConnectorError if missing."""
    return _GLOBAL.get(kind)


def list_kinds() -> tuple[str, ...]:
    """All registered kinds, sorted."""
    return _GLOBAL.list_kinds()


def list_specs() -> tuple[ConnectorSpec, ...]:
    """All registered specs, sorted by kind."""
    return _GLOBAL.list_specs()


def create(kind: str, config: Mapping[str, Any]) -> SourceConnector:
    """Build a connector instance from `kind` + `config`."""
    return _GLOBAL.create(kind, config)


def _reset_for_tests() -> None:
    """Drop the global registry — only tests should call this."""
    global _GLOBAL
    _GLOBAL = _Registry()
