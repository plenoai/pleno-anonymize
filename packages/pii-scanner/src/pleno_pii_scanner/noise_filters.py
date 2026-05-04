"""Structural noise filters applied after NER + regex, before verification.

These suppress findings that are structurally implausible as user PII even
though they superficially match a recognizer pattern. The principle is to
filter on robust **content-type** signals (reserved IP ranges, code fences,
version strings) rather than entity value blacklists, so we do not overfit
to one corpus.

Real-world FPs that motivated each filter — see ``tests/test_noise_filters.py``
for canonical examples drawn from azu/azu, mumumu/pep8-ja, nodejs/nodejs-ja
(round 1) and suisya-systems/claude-org-ja, Ajay77187718/awesome-ai-red-teaming-jp
(round 2: EMAIL re-validation, RFC 2606 reserved domains, ISBN-13, ASIN URLs,
non-name PERSON leaders).
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
# JWT / Unix timestamp digits: 10-digit numbers that look like phone
# numbers but are actually JSON Web Token claims. Captured from
# heyinc/development-partner-docs/auth-oauth.md where ``"jti"``, ``"iat"``,
# ``"exp"``, ``"nbf"`` claim values triggered Presidio PHONE_NUMBER.
# ---------------------------------------------------------------------------

_JWT_CLAIM_RE = re.compile(
    r"""
    "(?:iat|exp|nbf|jti|iss|aud|sub|auth_time|acr|azp)"\s*:\s*"?\d
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _in_jwt_claim(line: str) -> bool:
    return bool(_JWT_CLAIM_RE.search(line))


def _is_unix_timestamp_shape(matched: str) -> bool:
    """10-digit numbers in 1_000_000_000 .. 2_000_000_000 are 2001-2033 epochs."""
    if not (len(matched) == 10 and matched.isdigit()):
        return False
    n = int(matched)
    return 1_000_000_000 <= n <= 2_000_000_000


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
# EMAIL: strict re-validation + RFC 2606 reserved domains.
# Presidio's built-in EmailRecognizer is more permissive than our regex and
# captures CLI tokens like ``user.email=ci@example.com`` and pip git+https
# slugs like ``github.com/.../core-harness@v0.x.y`` as EMAIL. Re-validating
# the captured span against a strict pattern catches both classes.
# ---------------------------------------------------------------------------

_STRICT_EMAIL_RE = re.compile(
    r"\A[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\Z"
)

# RFC 2606 plus the conventional ``example.co.jp``. Never real PII.
_RESERVED_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "example.jp",
        "example.co.jp",
        "test",
        "test.com",
        "localhost",
        "invalid",
        "localdomain",
    }
)


def _is_reserved_email_domain(matched: str) -> bool:
    if "@" not in matched:
        return False
    domain = matched.rsplit("@", 1)[-1].lower()
    if domain in _RESERVED_EMAIL_DOMAINS:
        return True
    # Domain whose final label is version-shape (``v0.x.y`` / ``v1.0.0``).
    last = domain.rsplit(".", 1)[-1]
    return bool(re.fullmatch(r"v?\d+|[xy]", last))


# ---------------------------------------------------------------------------
# ISBN-13 detection — books register MY_NUMBER_CORPORATE because both are
# 13-digit numbers. Real ISBNs always start with the GS1 prefix 978 or 979.
# ---------------------------------------------------------------------------


def _is_isbn13(matched: str) -> bool:
    if not (len(matched) == 13 and matched.isdigit()):
        return False
    return matched.startswith(("978", "979"))


# ---------------------------------------------------------------------------
# Product / book URL paths — Amazon ASINs and ISBN paths surface 8-13 digit
# blobs that Presidio reads as PHONE / MY_NUMBER. The URL prefix is the
# strongest signal that the digits are a product code.
# ---------------------------------------------------------------------------

_PRODUCT_URL_RE = re.compile(
    r"""
    (?:
        amazon\.[a-z.]+/(?:dp|gp/product|exec/obidos/asin)/[A-Z0-9]+
      | books\.or\.jp/book-details/\d+
      | rakuten\.co\.jp/[^/\s]+/\d+
      | honto\.jp/(?:netstore|bookstore)/[^\s]*\d+
      | bookmeter\.com/books/\d+
      | calil\.jp/book/\d+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _in_product_url(line: str) -> bool:
    return bool(_PRODUCT_URL_RE.search(line))


# ---------------------------------------------------------------------------
# Japanese common-noun / verb suffixes that NER mis-tags as PERSON / ORG.
#
# These compound-noun and deverbal-noun suffixes are kept for spans the
# UniDic POS check below cannot reach (length > 3, or compounds Sudachi
# doesn't lemmatize to a single common-noun morpheme). Short atomic common
# nouns like 大文字 / 小文字 / 文字 / 名前 are intentionally NOT here — they
# go through `_is_unidic_short_common_noun`, where Sudachi's dictionary
# disagreement with spaCy NER is the audit trail.
#
# Sourced from the recurring FP set across azu/azu, mumumu/pep8-ja,
# nodejs/nodejs-ja, suisya-systems/claude-org-ja eval. We match the
# **suffix** of the candidate span, so general technical prose suppresses
# naturally without listing every compound.
# ---------------------------------------------------------------------------

_NER_FP_NOUN_SUFFIXES = (
    # Generic head nouns frequent in dev-doc prose
    "一覧", "番号", "設計", "機能", "属性", "定数", "変数",
    "関数", "引数", "戻り値", "演算子", "例外", "数値",
    "残置",                          # 原則残置 (leave-as-is)
    "呼び出し",                       # サンプル呼び出し
    # Verb-form deverbal nouns (action-of-X) — never personal names
    "割り当て", "割り当てる",
    "書き込み", "書き出し", "読み込み", "読み出し",
    "貼り付け", "切り出し", "差し替え",
    "立ち上げ", "立ち上がり",
    "問い合わせ",
)


def _ends_with_common_noun_suffix(matched: str) -> bool:
    return any(matched.endswith(s) for s in _NER_FP_NOUN_SUFFIXES)


# ---------------------------------------------------------------------------
# UniDic-derived auditable allowlist for short common nouns (issue #101).
#
# spaCy ja_ner_ja hallucinates PERSON on common Japanese nouns that appear
# in technical prose (大文字, 小文字, 文字, 名前, 半角, 全角, 残置, ...).
# A surface-form blacklist would be brittle and reduce recall on real
# surnames that share characters.
#
# Instead, we delegate to Sudachi/UniDic — a curated morphological
# dictionary — to reject candidates whose every morpheme is tagged
# **名詞-普通名詞** AND whose total length is ≤ 3 characters.
#
# The rule is auditable in two senses:
#   * Sudachi's dictionary lookup is the gate, not a hand-crafted list.
#   * The length≤3 cap bounds the harm: longer spans (e.g. real
#     compound surnames like 五十嵐) are never even considered.
#
# Real surnames (山田, 田中, 佐藤, 鈴木, 本田, ...) are tagged
# **名詞-固有名詞-人名-姓** by UniDic and pass through. Place names
# (東京, 京都, 大阪) are 名詞-固有名詞-地名 and likewise pass through.
# ---------------------------------------------------------------------------

_UNIDIC_SHORT_COMMON_NOUN_MAX_LEN = 3
_sudachi_tokenizer = None


def _get_sudachi():
    global _sudachi_tokenizer
    if _sudachi_tokenizer is not None:
        return _sudachi_tokenizer
    try:
        from sudachipy import dictionary, tokenizer as _sudachi_tok
    except ImportError:
        # spacy[ja] pulls sudachipy; if it's missing the filter degrades to
        # no-op rather than raising. The suffix list still covers most cases.
        _sudachi_tokenizer = (None, None)
        return _sudachi_tokenizer
    _sudachi_tokenizer = (
        dictionary.Dictionary().create(),
        _sudachi_tok.Tokenizer.SplitMode.C,
    )
    return _sudachi_tokenizer


def _is_unidic_short_common_noun(matched: str) -> bool:
    """Return True iff Sudachi/UniDic morphologically classifies ``matched``
    as a short common noun (every morpheme tagged 名詞-普通名詞, length ≤ 3).

    This is the principled replacement for surface-form blacklisting of
    大文字/小文字/文字/名前 (issue #101). The dictionary lookup, not a
    hand-curated list, decides what counts as a common noun — so the rule
    extends naturally to 半角/全角/数値/etc. without a code change, and
    never fires on UniDic-known proper nouns.
    """
    if not matched:
        return False
    if len(matched) > _UNIDIC_SHORT_COMMON_NOUN_MAX_LEN:
        return False
    tok, mode = _get_sudachi()
    if tok is None:
        return False
    morphs = list(tok.tokenize(matched, mode))
    if not morphs:
        return False
    for m in morphs:
        pos = m.part_of_speech()
        if pos[0] != "名詞" or pos[1] != "普通名詞":
            return False
    return True


# ---------------------------------------------------------------------------
# PERSON: non-name leaders / trailers from spaCy hallucinations. Real
# Japanese personal names never start with these characters or contain digits.
# ---------------------------------------------------------------------------

_NON_NAME_LEADERS = ("~", "～", "◎", "○", "△", "×", "（", "(", "［", "[", "※", "・")


def _starts_with_non_name_lead(matched: str) -> bool:
    if not matched:
        return False
    return matched[0] in _NON_NAME_LEADERS


def _contains_digit(matched: str) -> bool:
    return any(ch.isdigit() for ch in matched)


def _contains_paren(matched: str) -> bool:
    # Only OPENING brackets count as a non-name signal. Trailing closing
    # brackets are a common spaCy markdown-leak — e.g. ``株式会社ユーザベース]``
    # captured from a ``[株式会社ユーザベース](https://...)`` link. The bracket
    # leader case (``（Zenn）``) is already covered by ``_starts_with_non_name_lead``.
    return any(ch in matched for ch in "（(［[｛{")


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
            # Amazon ASIN, ISBN path — product-id digits, not phones.
            if _in_product_url(line):
                return True
            # JWT claim values ("iat": 1654514191, etc.) are 10-digit Unix
            # timestamps in JSON, not phone numbers. Gated on the claim-name
            # context so we don't suppress legitimate 10-digit phones.
            if _in_jwt_claim(line) and _is_unix_timestamp_shape(f.matched):
                return True
        return False

    if f.entity == "EMAIL_ADDRESS":
        # Presidio's EmailRecognizer is greedy and includes preceding CLI
        # tokens or trailing URL fragments. Re-check against a strict pattern.
        if not _STRICT_EMAIL_RE.match(f.matched):
            return True
        # RFC 2606 reserved or version-shape final-label domains.
        if _is_reserved_email_domain(f.matched):
            return True
        return False

    if f.entity in ("MY_NUMBER_CORPORATE", "MY_NUMBER"):
        # 13-digit ISBNs always start with 978/979 — never a corporate number.
        # Surfaced from Ajay77187718/awesome-ai-red-teaming-jp book lists.
        if f.entity == "MY_NUMBER_CORPORATE" and _is_isbn13(f.matched):
            return True
        # Product-URL digit blobs (Amazon dp, books.or.jp, rakuten, etc.)
        if _in_product_url(line):
            return True
        return False

    if f.entity == "PERSON" or f.entity == "ORGANIZATION":
        # spaCy ja_ner_ja fires on quoted code identifiers like ``if`` or
        # ``is not``. Robust structural signals, valid for both PERSON and
        # ORGANIZATION (both are NER-derived and share the failure modes):
        #
        # (a) Match contains a backtick — span includes inline-code delimiters.
        # (b) Match crosses a line boundary — spaCy hallucination on RST.
        # (c) Match sits inside a markdown inline-code span on the same line.
        # (d) Match starts with a non-name punctuation lead (◎, ~, （, ※, ...).
        # (e) Match contains a digit (real Japanese names never do).
        # (f) Match contains parentheses (NER bled control characters in).
        if "`" in f.matched:
            return True
        if "\n" in f.matched:
            return True
        if _in_code_span(line, f.col, f.matched):
            return True
        if _starts_with_non_name_lead(f.matched):
            return True
        if _contains_digit(f.matched):
            return True
        if _contains_paren(f.matched):
            return True
        if _ends_with_common_noun_suffix(f.matched):
            return True
        # Issue #101: UniDic morphological audit for short common-noun
        # PERSON candidates. Drops 大文字 / 小文字 / 文字 / 名前 / 半角 /
        # 全角 / 残置 / ... without a surface-form blacklist. Scoped to
        # PERSON only — short common nouns can legitimately surface as
        # ORGANIZATION (e.g. 総務省, 銀行), so the same morphological
        # rule would over-filter the ORG class.
        if f.entity == "PERSON" and _is_unidic_short_common_noun(f.matched):
            return True
        # Trailing/leading backtick adjacent to the match (matched span often
        # excludes the backtick because spaCy tokenises on it).
        idx = line.find(f.matched) if f.matched else -1
        if idx >= 0:
            left = line[idx - 1] if idx > 0 else ""
            right = line[idx + len(f.matched)] if idx + len(f.matched) < len(line) else ""
            if left == "`" or right == "`":
                return True
        # ASCII art "─────>│" runs of box-drawing chars — never a real entity.
        if any(c in f.matched for c in "─━│┃┌┐└┘├┤┬┴┼>"):
            return True
        return False

    return False
