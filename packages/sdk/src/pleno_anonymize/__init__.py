"""Local-first Japanese PII detection and redaction.

The :class:`PlenoAnonymize` class is the single entry point. By default it
loads the spaCy NER models and Presidio recognizer registry locally,
auto-downloading the model wheel on first use. Pass ``base_url`` to instead
talk to a hosted ``pleno-anonymize`` server (the same that serves
``/api/analyze`` and ``/api/redact``).
"""

from __future__ import annotations

from ._engine import (
    Engine,
    Finding,
    PlenoAnonymize,
    RedactResult,
)
from ._local import LocalEngine
from ._remote import PlenoAnonymizeError, RemoteEngine
from ._scanner import FileScanResult, ScanSummary, scan_file, scan_paths

__all__ = [
    "Engine",
    "FileScanResult",
    "Finding",
    "LocalEngine",
    "PlenoAnonymize",
    "PlenoAnonymizeError",
    "RedactResult",
    "RemoteEngine",
    "ScanSummary",
    "scan_file",
    "scan_paths",
]

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("pleno-anonymize")
except PackageNotFoundError:
    # Source-tree checkout without an installed dist (e.g. editable dev) —
    # fall back to a sentinel rather than guessing, so `--version` makes
    # the mismatch obvious instead of silently lying.
    __version__ = "0.0.0+unknown"
