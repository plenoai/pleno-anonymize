"""schema_version helper — determinism and component sensitivity."""

from __future__ import annotations

from pleno_pii_scanner.state import schema_version


class TestSchemaVersion:
    def test_deterministic_across_calls(self) -> None:
        assert schema_version() == schema_version()

    def test_extra_components_change_the_hash(self) -> None:
        base = schema_version()
        with_extra = schema_version("custom-recognizer-v1")
        assert base != with_extra

    def test_component_order_matters(self) -> None:
        # WHY: the runner must invalidate when *any* component flips, so
        # `(a, b)` and `(b, a)` are intentionally distinct schemas. A
        # stable ordering keeps the invariant clear.
        assert schema_version("a", "b") != schema_version("b", "a")

    def test_returns_short_hex(self) -> None:
        sv = schema_version()
        assert len(sv) == 32
        assert all(c in "0123456789abcdef" for c in sv)
