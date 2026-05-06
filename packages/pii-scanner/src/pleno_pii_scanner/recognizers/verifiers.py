"""Built-in verifier registry.

A verifier promotes a regex match from `unverified` to `passed` (or
`failed`) by checking a secondary signal — typically a checksum
(Luhn, MyNumber), a contextual keyword window, or a callable provided
by the user that talks to an external API.

Custom recognizers reference verifiers by name in their TOML config:

    [recognizer.verifier]
    type = "luhn"

    [recognizer.verifier]
    type = "callable"
    module = "myorg.verifiers"
    function = "verify_internal_token"

See ADR-0007 §8.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

VerifierResult = Literal["passed", "failed", "unverified"]
VerifierFn = Callable[[str, Mapping[str, Any]], VerifierResult]


class VerifierResolutionError(LookupError):
    """Raised when a verifier `type` (or callable target) cannot be resolved."""


@dataclass(frozen=True, slots=True)
class Verifier:
    """Resolved verifier ready to call against a matched value.

    `params` is the verifier-specific config from the TOML block (e.g.
    `extra_pattern` for `regex_check`, or `module` + `function` for
    `callable`). The dataclass is frozen so a recognizer's verifier is
    immutable for the life of a scan.
    """

    name: str
    fn: VerifierFn
    params: Mapping[str, Any]

    def check(self, value: str) -> VerifierResult:
        return self.fn(value, self.params)


def _verify_context_only(_value: str, _params: Mapping[str, Any]) -> VerifierResult:
    # Marker verifier: leaves the finding `unverified` so the existing
    # context-keyword pass in `pleno_pii_scanner.verify` can decide.
    # Useful when the user wants the scanner to surface matches but
    # judge them with the same rules as built-in recognizers.
    return "unverified"


def _verify_regex_check(value: str, params: Mapping[str, Any]) -> VerifierResult:
    # Confirms a second, stricter regex matches the value. Lets users
    # express "match this loose regex but only confirm if the value
    # additionally has this internal prefix/structure" without writing
    # Python.
    pattern = params.get("extra_pattern")
    if not isinstance(pattern, str):
        raise VerifierResolutionError(
            "regex_check verifier requires `extra_pattern: str` parameter"
        )
    return "passed" if re.search(pattern, value) else "failed"


def _verify_luhn(value: str, _params: Mapping[str, Any]) -> VerifierResult:
    # Standard mod-10 Luhn checksum, as used for credit cards and many
    # custom corporate ID formats. Implemented inline (rather than
    # delegated to pleno_recognizers.validators) to keep the BYOD module
    # free of cross-package import cycles.
    digits = [int(c) for c in value if c.isdigit()]
    if len(digits) < 2:
        return "failed"
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return "passed" if total % 10 == 0 else "failed"


def _verify_callable(value: str, params: Mapping[str, Any]) -> VerifierResult:
    # Importable `module:function` pair. The function receives the matched
    # value and must return one of {"passed", "failed", "unverified"}.
    # Anything else is treated as a verifier bug and produces `unverified`
    # so the scanner errs on the side of surfacing the finding.
    module_name = params.get("module")
    function_name = params.get("function")
    if not isinstance(module_name, str) or not isinstance(function_name, str):
        raise VerifierResolutionError(
            "callable verifier requires `module: str` and `function: str` parameters"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise VerifierResolutionError(
            f"callable verifier could not import {module_name!r}: {exc}"
        ) from exc
    try:
        fn = getattr(module, function_name)
    except AttributeError as exc:
        raise VerifierResolutionError(
            f"callable verifier could not resolve {module_name}:{function_name}: {exc}"
        ) from exc
    if not callable(fn):
        raise VerifierResolutionError(
            f"callable verifier target {module_name}:{function_name} is not callable"
        )
    try:
        result = fn(value)
    except Exception:
        # A user-supplied verifier crash must not abort the whole scan.
        # Log as `failed` so the operator sees the regression, not silent
        # `unverified` which would hide a buggy custom verifier.
        return "failed"
    if result in ("passed", "failed", "unverified"):
        return result  # type: ignore[no-any-return]
    return "unverified"


BUILTIN_VERIFIERS: dict[str, VerifierFn] = {
    "context_only": _verify_context_only,
    "regex_check": _verify_regex_check,
    "luhn": _verify_luhn,
    "callable": _verify_callable,
}


def register_verifier(name: str, fn: VerifierFn) -> None:
    """Add or replace a verifier in the global built-in registry.

    Intended for first-party plugins that ship additional checksum
    algorithms (e.g. MyNumber). Tests reset the registry between cases
    via `BUILTIN_VERIFIERS.clear()` + repopulation.
    """
    BUILTIN_VERIFIERS[name] = fn


def resolve_verifier(name: str, params: Mapping[str, Any]) -> Verifier:
    """Look up `name` in the registry and return a ready-to-call Verifier."""
    try:
        fn = BUILTIN_VERIFIERS[name]
    except KeyError as exc:
        raise VerifierResolutionError(
            f"unknown verifier type: {name!r}. Built-in: {sorted(BUILTIN_VERIFIERS)}"
        ) from exc
    return Verifier(name=name, fn=fn, params=dict(params))
