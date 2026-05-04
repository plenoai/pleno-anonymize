"""Custom (Bring-Your-Own-Detector) recognizer framework.

Enterprises invariably have internal secret formats (corporate API
tokens, custom employee-ID schemes, vendor-specific account numbers)
that the built-in `pleno_recognizers.ja` set does not cover. This module
loads `PiiRecognizer` definitions from a TOML file at scan time so users
can extend coverage without forking the package.

See ADR-0007 §8.
"""

from pleno_pii_scanner.recognizers.custom import (
    CustomRecognizerError,
    CustomRecognizerLoadError,
    CustomRecognizerSchemaError,
    VerifierResolutionError,
    load_custom_recognizers,
)
from pleno_pii_scanner.recognizers.verifiers import (
    BUILTIN_VERIFIERS,
    Verifier,
    VerifierFn,
    register_verifier,
)

__all__ = [
    "BUILTIN_VERIFIERS",
    "CustomRecognizerError",
    "CustomRecognizerLoadError",
    "CustomRecognizerSchemaError",
    "Verifier",
    "VerifierFn",
    "VerifierResolutionError",
    "load_custom_recognizers",
    "register_verifier",
]
