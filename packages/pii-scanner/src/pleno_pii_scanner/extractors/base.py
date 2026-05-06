"""ContentExtractor Protocol + glob-aware ExtractorRegistry.

The registry maps a MIME type (or sniffed magic family) to an Extractor
that turns a binary/text Document into a stream of ExtractedFragment for
the regex / NER passes. Dispatch is glob-aware so a single registration
of ``text/*`` covers ``text/plain``, ``text/markdown``, ``text/html``
without enumerating every subtype.

See ADR-0007 §6 for the architectural placement.
"""

from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from threading import RLock
from typing import Protocol, runtime_checkable

from pleno_pii_scanner.sources.base import Document, DocumentChunk


class ExtractorError(Exception):
    """Base for extractor errors."""


class UnknownExtractorError(ExtractorError, KeyError):
    """No registered Extractor accepts the requested MIME type."""


class BombGuardError(ExtractorError):
    """Archive depth or expansion ratio exceeded the configured limit.

    The pipeline catches this and emits a finding-class warning event;
    the rest of the scan continues on sibling documents. Raising lets us
    keep guard checks at the point of detection (deepest in the
    decompression loop) without threading a status code back up.
    """


class ExtractionWarning(UserWarning):
    """Soft signal — member skipped, charset fell back, etc.

    Not an error: extraction continues. Surfaced via ``warnings.warn``
    so operators can filter for them in their log pipeline rather than
    treating them as findings.
    """


@dataclass(frozen=True, slots=True)
class ExtractedFragment:
    """Smallest scannable unit produced by an Extractor.

    ``path_hint`` is *content-internal* (zip member path, PDF page index,
    sheet name) — never the connector ``source_id`` or DocumentRef path.
    Callers concatenate them: ``f"{ref.path}::{fragment.path_hint}"``.
    ``byte_offset`` is the offset *within the original document* (not the
    decompressed member) so regex line-number recovery still works after
    archive descent.
    """

    text: str
    path_hint: str
    byte_offset: int | None
    extractor: str


@runtime_checkable
class Extractor(Protocol):
    """Type contract every content extractor implements."""

    name: str
    accepts: frozenset[str]

    def extract(
        self, doc: Document | DocumentChunk
    ) -> AsyncIterator[ExtractedFragment]:
        """Yield fragments, in document order. Async to allow nested I/O."""
        ...


class ExtractorRegistry:
    """Glob-aware MIME -> Extractor lookup.

    Multiple extractors may register patterns that match the same MIME;
    the most-specific (longest, with literal characters) wins. The order
    of `register` calls is otherwise irrelevant — important because
    extractors are wired up at module-import time and import order is not
    stable across `python -m`, `pytest`, and entry-points discovery.
    """

    def __init__(self) -> None:
        self._patterns: list[tuple[str, Extractor]] = []
        self._lock = RLock()

    def register(self, mime_pattern: str, extractor: Extractor) -> None:
        """Add ``extractor`` for MIMEs matching ``mime_pattern`` (glob)."""
        with self._lock:
            # Replace if the exact same pattern is registered again so test
            # fixtures can override without leaking across cases.
            self._patterns = [(p, e) for (p, e) in self._patterns if p != mime_pattern]
            self._patterns.append((mime_pattern, extractor))

    def for_mime(self, mime: str) -> Extractor:
        """Return the most-specific Extractor accepting ``mime``."""
        with self._lock:
            matches = [
                (pattern, ex)
                for (pattern, ex) in self._patterns
                if fnmatch.fnmatchcase(mime, pattern)
            ]
        if not matches:
            raise UnknownExtractorError(f"no extractor registered for MIME {mime!r}")
        # Specificity = literal-character count (wildcards don't count).
        # Tie-break on raw length so ``text/x-*`` beats ``text/*``.
        matches.sort(key=lambda pe: (_specificity(pe[0]), len(pe[0])), reverse=True)
        return matches[0][1]

    def patterns(self) -> tuple[str, ...]:
        """All registered patterns (for debug + CLI ``extractors list``)."""
        with self._lock:
            return tuple(p for (p, _) in self._patterns)

    def clear(self) -> None:
        """Drop all registrations. Tests use this between cases."""
        with self._lock:
            self._patterns.clear()


def _specificity(pattern: str) -> int:
    """Count of non-wildcard characters in a fnmatch pattern."""
    return sum(1 for c in pattern if c not in "*?[]")


_GLOBAL = ExtractorRegistry()


def register(mime_pattern: str, extractor: Extractor) -> None:
    """Register ``extractor`` in the process-global ExtractorRegistry."""
    _GLOBAL.register(mime_pattern, extractor)


def for_mime(mime: str) -> Extractor:
    """Lookup in the process-global ExtractorRegistry."""
    return _GLOBAL.for_mime(mime)


def patterns() -> tuple[str, ...]:
    """All globally-registered patterns."""
    return _GLOBAL.patterns()


def _reset_for_tests() -> None:
    """Drop the global registry — only tests should call this."""
    global _GLOBAL
    _GLOBAL = ExtractorRegistry()


async def collect(
    extractor: Extractor, doc: Document | DocumentChunk
) -> list[ExtractedFragment]:
    """Materialize an Extractor's stream — convenience for tests + CLI."""
    out: list[ExtractedFragment] = []
    async for frag in extractor.extract(doc):
        out.append(frag)
    return out


def doc_payload(doc: Document | DocumentChunk) -> bytes | str:
    """Return whichever of ``text``/``binary`` is populated.

    Centralises the (text XOR binary) handling that every Extractor needs
    so individual extractors don't reimplement the if-ladder + raise.
    """
    if doc.text is not None:
        return doc.text
    if doc.binary is not None:
        return doc.binary
    # Both __post_init__ checks already enforce XOR, so this is unreachable
    # in practice — kept as defence in depth in case someone bypasses the
    # dataclass via object.__new__ in a fixture.
    raise ExtractorError("Document has neither text nor binary populated")


def iter_extractors() -> Iterable[tuple[str, Extractor]]:
    """All ``(pattern, extractor)`` pairs in registration order."""
    return tuple(_GLOBAL._patterns)
