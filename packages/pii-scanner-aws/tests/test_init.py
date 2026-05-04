"""Smoke test the public package surface stays stable.

Re-exports from `pleno_pii_scanner_aws/__init__.py` are part of the
documented contract; downstream wheels and the core CLI import them by
name. A typo here would break the entry-point lookup at the worst
possible moment (production).
"""

from __future__ import annotations

import pleno_pii_scanner_aws as pkg


def test_version_string() -> None:
    assert pkg.__version__ == "0.1.0"


def test_spec_exported() -> None:
    assert pkg.SPEC.kind == "aws-s3"


def test_all_listed_symbols_resolve() -> None:
    # Every entry in __all__ must actually be importable.
    for name in pkg.__all__:
        assert hasattr(pkg, name), f"missing export: {name}"


def test_entry_point_target_resolves() -> None:
    # Mirrors what `pleno_pii_scanner.sources.registry` does at boot:
    # ep.load() resolves to `pleno_pii_scanner_aws:SPEC`. Validate the
    # attribute path explicitly so a refactor that drops SPEC fails here.
    import importlib

    mod = importlib.import_module("pleno_pii_scanner_aws")
    assert getattr(mod, "SPEC") is pkg.SPEC
