"""Tests for built-in verifier registry."""

from __future__ import annotations

import sys
import types

import pytest

from pleno_pii_scanner.recognizers.verifiers import (
    BUILTIN_VERIFIERS,
    Verifier,
    VerifierResolutionError,
    register_verifier,
    resolve_verifier,
)


class TestContextOnly:
    def test_returns_unverified_so_existing_pass_decides(self) -> None:
        v = resolve_verifier("context_only", {})
        assert v.check("anything") == "unverified"


class TestRegexCheck:
    def test_passed_when_extra_pattern_matches(self) -> None:
        v = resolve_verifier("regex_check", {"extra_pattern": r"^INT-[A-Z]{4}-"})
        assert v.check("INT-PROD-12345") == "passed"

    def test_failed_when_extra_pattern_misses(self) -> None:
        v = resolve_verifier("regex_check", {"extra_pattern": r"^INT-[A-Z]{4}-"})
        assert v.check("XYZ-12345") == "failed"

    def test_missing_extra_pattern_raises(self) -> None:
        with pytest.raises(VerifierResolutionError, match="extra_pattern"):
            resolve_verifier("regex_check", {}).check("x")

    def test_non_string_extra_pattern_raises(self) -> None:
        with pytest.raises(VerifierResolutionError, match="extra_pattern"):
            resolve_verifier("regex_check", {"extra_pattern": 42}).check("x")


class TestLuhn:
    @pytest.mark.parametrize(
        "value",
        [
            "4242424242424242",  # well-known Stripe test PAN
            "4111111111111111",  # canonical valid Visa test
            "5555555555554444",  # MasterCard test
        ],
    )
    def test_passes_known_valid_pans(self, value: str) -> None:
        assert resolve_verifier("luhn", {}).check(value) == "passed"

    def test_fails_invalid_pan(self) -> None:
        assert resolve_verifier("luhn", {}).check("4242424242424241") == "failed"

    def test_ignores_non_digits(self) -> None:
        # Real-world matches often include separators (`4242 4242 4242 4242`).
        # Luhn must treat the digit-only sequence.
        assert resolve_verifier("luhn", {}).check("4242-4242-4242-4242") == "passed"

    def test_fails_too_short(self) -> None:
        assert resolve_verifier("luhn", {}).check("4") == "failed"

    def test_fails_no_digits(self) -> None:
        assert resolve_verifier("luhn", {}).check("abcdef") == "failed"


class TestCallable:
    def test_resolves_and_invokes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("_pleno_test_verifier")
        mod.verify = lambda v: "passed" if v == "secret" else "failed"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "_pleno_test_verifier", mod)
        v = resolve_verifier(
            "callable",
            {"module": "_pleno_test_verifier", "function": "verify"},
        )
        assert v.check("secret") == "passed"
        assert v.check("nope") == "failed"

    def test_unknown_return_value_falls_back_to_unverified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # User-supplied functions that return garbage ("ok", True, ...) must
        # not corrupt the Verification literal type. Treating unexpected
        # outputs as `unverified` keeps the finding visible without
        # promoting it.
        mod = types.ModuleType("_pleno_test_verifier_bad")
        mod.verify = lambda _v: "weird"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "_pleno_test_verifier_bad", mod)
        v = resolve_verifier(
            "callable",
            {"module": "_pleno_test_verifier_bad", "function": "verify"},
        )
        assert v.check("x") == "unverified"

    def test_crashing_callable_returns_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = types.ModuleType("_pleno_test_verifier_crash")

        def boom(_value: str) -> str:
            raise RuntimeError("upstream API down")

        mod.verify = boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "_pleno_test_verifier_crash", mod)
        v = resolve_verifier(
            "callable",
            {"module": "_pleno_test_verifier_crash", "function": "verify"},
        )
        # `failed` rather than `unverified` so a buggy custom verifier
        # is visible — silent `unverified` would let regressions hide.
        assert v.check("x") == "failed"

    def test_missing_module_raises(self) -> None:
        with pytest.raises(VerifierResolutionError, match="could not import"):
            resolve_verifier(
                "callable",
                {"module": "_pleno_test_definitely_not_a_module", "function": "x"},
            ).check("x")

    def test_missing_function_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = types.ModuleType("_pleno_test_no_attr")
        monkeypatch.setitem(sys.modules, "_pleno_test_no_attr", mod)
        with pytest.raises(VerifierResolutionError, match="could not resolve"):
            resolve_verifier(
                "callable",
                {"module": "_pleno_test_no_attr", "function": "nonexistent"},
            ).check("x")

    def test_non_callable_target_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = types.ModuleType("_pleno_test_not_callable")
        mod.verify = "not a function"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "_pleno_test_not_callable", mod)
        with pytest.raises(VerifierResolutionError, match="not callable"):
            resolve_verifier(
                "callable",
                {"module": "_pleno_test_not_callable", "function": "verify"},
            ).check("x")

    def test_missing_module_param_raises(self) -> None:
        with pytest.raises(VerifierResolutionError, match="module"):
            resolve_verifier("callable", {"function": "verify"}).check("x")

    def test_missing_function_param_raises(self) -> None:
        with pytest.raises(VerifierResolutionError, match="module"):
            resolve_verifier("callable", {"module": "x"}).check("x")


class TestRegistry:
    def test_unknown_type_lists_builtins(self) -> None:
        with pytest.raises(VerifierResolutionError) as exc:
            resolve_verifier("nope", {})
        msg = str(exc.value)
        assert "luhn" in msg
        assert "regex_check" in msg

    def test_register_then_resolve_picks_up(self) -> None:
        def my_verifier(value: str, _params: object) -> str:
            return "passed" if value == "magic" else "failed"

        original = BUILTIN_VERIFIERS.copy()
        try:
            register_verifier("test_added", my_verifier)
            v = resolve_verifier("test_added", {})
            assert v.check("magic") == "passed"
            assert v.check("other") == "failed"
        finally:
            BUILTIN_VERIFIERS.clear()
            BUILTIN_VERIFIERS.update(original)


class TestVerifierDataclass:
    def test_is_immutable(self) -> None:
        v = Verifier(name="x", fn=lambda _v, _p: "passed", params={})
        with pytest.raises((AttributeError, TypeError)):
            v.name = "y"  # type: ignore[misc]

    def test_check_delegates_to_fn(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def fn(value: str, params: dict[str, object]) -> str:
            calls.append((value, dict(params)))
            return "passed"

        v = Verifier(name="x", fn=fn, params={"k": "v"})
        assert v.check("abc") == "passed"
        assert calls == [("abc", {"k": "v"})]
