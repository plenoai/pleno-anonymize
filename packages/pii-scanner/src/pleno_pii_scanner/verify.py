"""Verification layer: checksum + context proximity.

Mirrors trufflehog's "verified vs unverified" UX. PII-specific because
we cannot hit an API to confirm — we use formal checksums and contextual
keyword proximity instead.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Iterable

from pleno_recognizers.types import PiiRecognizer
from pleno_recognizers.validators import validate

from pleno_pii_scanner.models import Finding


def _context_keywords(
    recognizers: Iterable[PiiRecognizer],
) -> dict[str, tuple[str, ...]]:
    # Multiple recognizers may share an entity (e.g. PERSON regex layers);
    # union their context keyword tuples instead of clobbering.
    keywords: dict[str, tuple[str, ...]] = {}
    for r in recognizers:
        merged = keywords.get(r.entity, ()) + r.context
        # Dedup while preserving order — important for stable test snapshots.
        seen: set[str] = set()
        keywords[r.entity] = tuple(k for k in merged if not (k in seen or seen.add(k)))
    return keywords


_CONTEXT_WINDOW = 64


def _keyword_in_window(window: str, keyword: str) -> bool:
    """Substring presence with word-boundary awareness for ASCII keywords.

    Plain ``in`` matched ``"by"`` against ``"RubyMine"`` and boosted every
    JetBrains-IDE list as a "PERSON-near-keyword" hit. Word boundaries fix
    the false positive without changing behavior for Japanese keywords (the
    Unicode word-boundary regex still treats them as bounded by punctuation
    or whitespace), and keep punctuation-bearing markers like ``"Reviewed-by"``
    matching as expected.
    """
    if not keyword:
        return False
    # ``\b`` is wide enough for our purposes: it fires at any transition
    # between a word character and a non-word character (including kanji /
    # kana boundaries against ASCII punctuation).
    pattern = r"(?<!\w)" + re.escape(keyword) + r"(?!\w)"
    return bool(re.search(pattern, window, re.IGNORECASE))


# Latin-script PERSON candidates (issue #102) gain a stronger boost when an
# email pattern is in the immediate context, since "Name <email>" is the
# strongest possible attribution signal. Wider window than the keyword path
# because PEP-style author lists put email on a continuation line.
_PERSON_EMAIL_WINDOW = 96
_EMAIL_NEAR_NAME_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


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
            out.append(
                replace(f, verification="failed", score=max(0.05, f.score - 0.4))
            )
            continue

        # 2. Context proximity boost.
        score = f.score
        verification = "passed" if checksum is True else "unverified"
        kw = keywords.get(f.entity, ())
        if file_text_for is not None and (kw or f.entity == "PERSON"):
            text = file_text_for.get(f.file, "")
            if text:
                offset = _find_match_offset(text, f)
                start_in_text = max(0, offset - _CONTEXT_WINDOW)
                end_in_text = start_in_text + _CONTEXT_WINDOW * 2 + len(f.matched)
                window = text[start_in_text:end_in_text]
                # Exclude the matched span from the keyword window: a
                # finding "Contributor Covenant" otherwise self-promotes
                # because the keyword "Contributor" sits inside the match
                # itself. We split the window into the slice BEFORE and the
                # slice AFTER the match.
                match_in_window = window.find(f.matched) if f.matched else -1
                if match_in_window >= 0:
                    keyword_haystack = (
                        window[:match_in_window]
                        + " "
                        + window[match_in_window + len(f.matched) :]
                    )
                else:
                    keyword_haystack = window
                if kw and any(_keyword_in_window(keyword_haystack, k) for k in kw):
                    score = min(0.99, score + 0.15)
                    if verification == "unverified":
                        verification = "passed"

                # PERSON recall booster: when an email sits within the wider
                # window (PEP author lists span continuation lines), promote
                # the candidate above the 0.5 default reporting bar so it
                # doesn't get filtered as low-confidence noise.
                if f.entity == "PERSON":
                    wide_start = max(0, offset - _PERSON_EMAIL_WINDOW)
                    wide_end = wide_start + _PERSON_EMAIL_WINDOW * 2 + len(f.matched)
                    if _EMAIL_NEAR_NAME_RE.search(text[wide_start:wide_end]):
                        score = min(0.99, max(score, 0.4) + 0.4)
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
