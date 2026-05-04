"""Structural noise filters applied after NER + regex, before verification.

These suppress findings that are structurally implausible as user PII even
though they superficially match a recognizer pattern. The principle is to
filter on robust **content-type** signals (reserved IP ranges, code fences,
version strings) rather than entity value blacklists, so we do not overfit
to one corpus.

Real-world FPs that motivated each filter — see ``tests/test_noise_filters.py``
for canonical examples drawn from azu/azu, mumumu/pep8-ja, nodejs/nodejs-ja.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Iterable

from pleno_pii_scanner.models import Finding


# ---------------------------------------------------------------------------
# IP address: drop reserved / special-purpose ranges that are not user PII.
# ---------------------------------------------------------------------------

# Presidio's IpRecognizer also fires on textual IPv6 short forms like ``::``,
# which only appears in source as the Sphinx ``.. code-block::`` directive.
# We treat these as non-PII regardless of context.
_NON_PII_IPV6_LITERALS = frozenset({"::", "::1"})


def _is_reserved_ip(matched: str) -> bool:
    try:
        ip = ipaddress.ip_address(matched)
    except ValueError:
        return False
    # ip_address considers the following non-global; all are uninteresting
    # for user-identification PII purposes.
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_private  # RFC1918 + ULA
    )


# ---------------------------------------------------------------------------
# Version / identifier context: source files that talk about software version
# numbers, GitHub issue/PR ids, package tarball hashes, or semver-shape lock
# entries surface IPv4-shaped and digit-blob matches that are clearly not
# PII. We look for these signals in a small window around the match.
# ---------------------------------------------------------------------------

# Explicit version/identifier markers only. Bare semver-shape patterns in the
# line cannot be used here because IPv4 addresses are themselves semver-shape
# (e.g. ``8.8.8.8`` would self-trigger). Keywords give a much higher precision
# signal: a version conversation contains lexical hints, not just digits.
_VERSION_CONTEXT_RE = re.compile(
    r"""
    (?:
        \bversion\b              # "version 1.2.3"
      | \bv\d+\.\d+              # "v1.2.3" prefix in changelog/release notes
      | \bV8\b                   # V8 engine version chatter dominates nodejs-ja
      | \bbump(?:ed|s|ing)?\b
      | \bupgrade(?:d|s|ing)?\b
      | \brelease(?:d|s)?\b
      | \.tgz\#                  # yarn.lock tarball checksum suffix
      | \[\#\d+\]                # markdown link to issue/PR — "[#1694]"
      | /(?:pull|issues)/\d+     # github URL
      | ^\s*version\s+"          # yarn.lock '  version "1.2.3"'
      | [\^~]\d+\.\d+\.          # npm semver range, e.g. "^1.0.30001093"
      | (?:>=|<=|<|>)\s*\d+\.\d  # ">=1.0", "< 2.0"
      | @[\^~]?\d+\.\d+          # yarn.lock entry, e.g. "react-is@^16.8.1"
    )
    """,
    re.IGNORECASE | re.VERBOSE | re.MULTILINE,
)


def _has_version_context(window: str) -> bool:
    return bool(_VERSION_CONTEXT_RE.search(window))


# ---------------------------------------------------------------------------
# Code-span detection: if the matched substring is bounded by inline-code
# delimiters (``…``, `` ` ``, RST ``…``) on the same line, treat it as a
# code identifier rather than data. Common in docs/READMEs that quote
# Python / shell tokens.
# ---------------------------------------------------------------------------


def _in_code_span(line: str, col_1based: int, matched: str) -> bool:
    """Return True if ``matched`` at ``col`` sits inside a backtick code span."""
    if not matched or "`" not in line:
        return False
    # Convert to 0-based and locate the actual match (the col reported by
    # Presidio/spaCy may be slightly off when chunking).
    idx = line.find(matched)
    if idx < 0:
        idx = max(0, col_1based - 1)
    end = idx + len(matched)
    left = line[:idx]
    right = line[end:]
    # Inline code = unmatched backticks on either side. Counting odd backticks
    # before AND after the match is the standard markdown rule and avoids
    # firing on a single decorative backtick.
    return (left.count("`") % 2 == 1) and (right.count("`") % 2 == 1)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


# How many characters of context around the match to consider for version /
# code-span signals. Wider than verify._CONTEXT_WINDOW because version chatter
# often sits a few words away ("upgrade V8 from 4.2.77.18 to 4.2.77.20").
_NOISE_WINDOW = 96


def _line_for(file_text: str, line: int) -> str:
    if not file_text:
        return ""
    # Walk to the requested 1-based line number cheaply.
    pos = 0
    cur = 1
    while cur < line:
        nxt = file_text.find("\n", pos)
        if nxt == -1:
            return ""
        pos = nxt + 1
        cur += 1
    end = file_text.find("\n", pos)
    return file_text[pos:end] if end != -1 else file_text[pos:]


def _window_around(file_text: str, line_text: str, matched: str) -> str:
    if matched and matched in line_text:
        idx = line_text.find(matched)
        return line_text[max(0, idx - _NOISE_WINDOW) : idx + len(matched) + _NOISE_WINDOW]
    return line_text


def filter_noise(
    findings: Iterable[Finding],
    *,
    file_text_for: dict[str, str] | None = None,
) -> list[Finding]:
    """Drop structurally-implausible findings.

    The filters are conservative: each drop must point at a robust non-PII
    signal (reserved IP range, version-string context, inline code span).
    Anything ambiguous is kept and surfaced as ``unverified``.
    """
    file_text_for = file_text_for or {}
    kept: list[Finding] = []
    for f in findings:
        if _should_drop(f, file_text_for):
            continue
        kept.append(f)
    return kept


def _should_drop(f: Finding, file_text_for: dict[str, str]) -> bool:
    text = file_text_for.get(f.file, "")
    line = _line_for(text, f.line)
    window = _window_around(text, line, f.matched)

    if f.entity == "IP_ADDRESS":
        # 1. IPv6 ``::`` / ``::1`` literals — almost always Sphinx markup.
        if f.matched in _NON_PII_IPV6_LITERALS:
            return True
        # 2. Reserved / private / loopback / link-local / multicast.
        if _is_reserved_ip(f.matched):
            return True
        # 3. Looks like a software version mentioned in a version-y context.
        if _has_version_context(window):
            return True
        # 4. Sits inside a backtick code span (``4.1.0.25``).
        if _in_code_span(line, f.col, f.matched):
            return True
        return False

    if f.entity == "PHONE_NUMBER":
        # Low-confidence Presidio phone matches need either a context keyword
        # (handled by verify.py promoting to "passed") or a strict JA pattern.
        # Drop unverified low-confidence ones that sit in version/PR contexts
        # or inside a code span — that's the pattern behind every observed
        # PHONE FP (yarn.lock semver, GitHub PR ids, V8 patch numbers).
        if f.score <= 0.45 and f.verification != "passed":
            if _has_version_context(window):
                return True
            if _in_code_span(line, f.col, f.matched):
                return True
            # Pure semver or floats like "16.43.2", "1.0.30001093" — never a phone.
            if re.fullmatch(r"\d+\.\d+(?:\.\d+)+", f.matched):
                return True
            # Bare numeric ID pulled from a Markdown link like "[#1694]" —
            # the regex match drops the punctuation, so check the raw line.
            if re.search(r"[#\[]\s*" + re.escape(f.matched) + r"\b", line):
                return True
            # Date-like fragment "16 2015-02-16".
            if re.search(r"\d{4}-\d{2}-\d{2}", f.matched):
                return True
        return False

    if f.entity == "PERSON":
        # spaCy ja_ner_ja fires on quoted code identifiers like ``if`` or
        # ``is not``. Three filter signals, all robust:
        #
        # (a) Match contains a backtick — span includes inline-code delimiters,
        #     which means spaCy bled a code token into the entity.
        # (b) Match crosses a line boundary — spaCy hallucination on RST
        #     (we have observed multi-paragraph "PERSON" spans on pep8-ja).
        # (c) Match sits inside a markdown inline-code span on the same line.
        if "`" in f.matched:
            return True
        if "\n" in f.matched:
            return True
        if _in_code_span(line, f.col, f.matched):
            return True
        # Trailing/leading backtick adjacent to the match (matched span often
        # excludes the backtick because spaCy tokenises on it).
        idx = line.find(f.matched) if f.matched else -1
        if idx >= 0:
            left = line[idx - 1] if idx > 0 else ""
            right = line[idx + len(f.matched)] if idx + len(f.matched) < len(line) else ""
            if left == "`" or right == "`":
                return True
        return False

    return False
