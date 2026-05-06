"""schema_version helper — pure component hash, not package-version-aware."""

from __future__ import annotations

import pytest

from pleno_pii_scanner.state import schema_version


class TestSchemaVersion:
    def test_deterministic_across_calls(self) -> None:
        assert schema_version() == schema_version()
        assert schema_version("a", "b") == schema_version("a", "b")

    def test_components_change_the_hash(self) -> None:
        base = schema_version()
        with_extra = schema_version("custom-recognizer-v1")
        assert base != with_extra

    def test_component_order_matters(self) -> None:
        # WHY: the runner must invalidate when *any* component flips, so
        # `(a, b)` and `(b, a)` are intentionally distinct schemas.
        assert schema_version("a", "b") != schema_version("b", "a")

    def test_returns_short_hex(self) -> None:
        sv = schema_version("seed")
        assert len(sv) == 32
        assert all(c in "0123456789abcdef" for c in sv)

    def test_empty_components_is_stable(self) -> None:
        # No components is the empty hash — equal to itself, distinct
        # from any non-empty input. Operators that opt out of all
        # invalidation get a stable, reproducible value.
        assert schema_version() == schema_version()
        assert schema_version() != schema_version("anything")

    def test_does_not_depend_on_pleno_pii_scanner_package_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # WHY: a patch release of pleno-pii-scanner that does not touch
        # detector logic must not invalidate the cache. We assert this
        # by mutating any importlib.metadata.version lookup the helper
        # might still be doing — if the result changes, the test fails
        # and we know `_TRACKED_DISTRIBUTIONS`-style logic crept back.
        from importlib import metadata

        original = metadata.version

        def fake_version(name: str) -> str:
            return "9999.9999.9999"

        monkeypatch.setattr(metadata, "version", fake_version)
        try:
            sv_with_fake = schema_version("seed")
        finally:
            monkeypatch.setattr(metadata, "version", original)
        sv_without_fake = schema_version("seed")
        assert sv_with_fake == sv_without_fake
