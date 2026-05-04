"""ContentExtractor framework — MIME-dispatched binary -> text pipeline.

ADR-0007 §6 splits content extraction from connector I/O. Connectors
(``sources.*``) deliver raw bytes; extractors here turn those bytes into
``ExtractedFragment`` records that the regex / NER passes can consume,
regardless of whether the original was a markdown file, a Word doc, or a
zstd-compressed parquet inside a tarball.

Layout:

- ``base``: Protocol, ExtractorRegistry, ExtractedFragment, errors
- ``sniff``: stdlib-only magic-byte sniffer (replaces ``python-magic``)
- ``text``: text/* passthrough + charset-normalizer decode
- ``html``: text/html via stdlib HTMLParser
- ``archive``: zip/tar/gz/zstd with depth + expansion bomb guards
- ``pdf``, ``office``, ``columnar``: optional extras

The core install (``text + archive + html + sniff``) has zero new
dependencies beyond ``charset-normalizer``. Optional extractors are
imported lazily so a tenant scanning only Markdown does not pay for
PDF/Office/columnar wheels.
"""

from pleno_pii_scanner.extractors.base import (
    BombGuardError,
    ExtractedFragment,
    Extractor,
    ExtractorError,
    ExtractorRegistry,
    ExtractionWarning,
    UnknownExtractorError,
    collect,
    doc_payload,
    for_mime,
    iter_extractors,
    patterns,
    register,
)
from pleno_pii_scanner.extractors.sniff import OCTET_STREAM, sniff

__all__ = [
    "BombGuardError",
    "ExtractedFragment",
    "Extractor",
    "ExtractorError",
    "ExtractorRegistry",
    "ExtractionWarning",
    "OCTET_STREAM",
    "UnknownExtractorError",
    "collect",
    "doc_payload",
    "for_mime",
    "iter_extractors",
    "patterns",
    "register",
    "sniff",
]
