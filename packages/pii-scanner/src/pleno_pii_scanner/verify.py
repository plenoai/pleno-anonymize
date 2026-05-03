"""Verification layer: checksum + context proximity.

Mirrors trufflehog's "verified vs unverified" UX. PII-specific because
we cannot hit an API to confirm — we use formal checksums and contextual
keyword proximity instead.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from pleno_recognizers.types import PiiRecognizer
from pleno_recognizers.validators import validate

from pleno_pii_scanner.models import Finding


def _context_keywords(recognizers: Iterable[PiiRecognizer]) -> dict[str, tuple[str, ...]]:
    return {r.entity: r.context for r in recognizers}


_CONTEXT_WINDOW = 64


def verify(
    findings: list[Finding],
    recognizers: Iterable[PiiRecognizer],
    *,
    file_text_for: dict[str, str] | None = None,
) -> list[Finding]:
    """Annotate each finding with verification status + adjusted score."""
    keywords = _context_keywords(recognizers)
    out: list[Finding] = []
    for f in findings:
        # 1. Checksum.
        checksum = validate(f.entity, f.matched)
        if checksum is False:
            out.append(replace(f, verification="failed", score=max(0.05, f.score - 0.4)))
            continue

        # 2. Context proximity boost.
        score = f.score
        verification = "passed" if checksum is True else "unverified"
        kw = keywords.get(f.entity, ())
        if kw and file_text_for is not None:
            text = file_text_for.get(f.file, "")
            if text:
                start_in_text = max(0, _find_match_offset(text, f) - _CONTEXT_WINDOW)
                end_in_text = start_in_text + _CONTEXT_WINDOW * 2 + len(f.matched)
                window = text[start_in_text:end_in_text]
                if any(k.lower() in window.lower() for k in kw):
                    score = min(0.99, score + 0.15)
                    if verification == "unverified":
                        verification = "passed"

        out.append(replace(f, score=score, verification=verification))
    return out


def _find_match_offset(text: str, f: Finding) -> int:
    """Best-effort: locate matched string near reported (line, col).

    Walks to the start of `f.line` then advances to find the match. Falls
    back to 0 if not found (in which case the context check is harmless).
    """
    line = 1
    pos = 0
    while line < f.line and pos < len(text):
        nxt = text.find("\n", pos)
        if nxt == -1:
            return 0
        pos = nxt + 1
        line += 1
    idx = text.find(f.matched, pos)
    return idx if idx >= 0 else pos
