"""Real-world FP regression tests.

Each case is anchored in a finding observed during the v0.2.1 real-world eval
on azu/azu, mumumu/pep8-ja, nodejs/nodejs-ja. Keeping these as code-level
tests prevents the structural noise filter from regressing as recognizers
or models evolve.
"""

from pleno_pii_scanner.models import Finding
from pleno_pii_scanner.noise_filters import filter_noise


def _f(entity, matched, snippet, *, line=1, col=1, score=0.4, verification="unverified"):
    return Finding(
        entity=entity,
        file="a.txt",
        line=line,
        col=col,
        score=score,
        snippet=snippet,
        matched=matched,
        pattern_name="presidio",
        verification=verification,
    )


def _kept(findings, file_text):
    return filter_noise(findings, file_text_for={"a.txt": file_text})


# ---------------------------------------------------------------------------
# IP_ADDRESS
# ---------------------------------------------------------------------------


def test_drops_ipv6_double_colon_from_rst_code_block():
    f = _f("IP_ADDRESS", "::", ".. code-block::")
    assert _kept([f], ".. code-block::\n") == []


def test_drops_loopback_ipv4():
    f = _f("IP_ADDRESS", "127.0.0.1", "* http://127.0.0.1:8080/ja/index.html を開く")
    assert _kept([f], f.snippet) == []


def test_drops_private_rfc1918():
    f = _f("IP_ADDRESS", "192.168.1.1", "internal: 192.168.1.1")
    assert _kept([f], f.snippet) == []


def test_drops_ipv4_in_v8_version_chatter():
    f = _f("IP_ADDRESS", "4.2.77.18",
           "* **V8**: upgrade V8 from 4.2.77.18 to 4.2.77.20 with minor fixes")
    assert _kept([f], f.snippet) == []


def test_drops_ipv4_in_inline_code_span():
    line = "see `4.1.0.25` for details"
    f = _f("IP_ADDRESS", "4.1.0.25", line)
    assert _kept([f], line) == []


def test_keeps_public_ipv4_without_version_context():
    line = "サーバ 8.8.8.8 にDNSクエリを投げる"
    f = _f("IP_ADDRESS", "8.8.8.8", line, score=0.6)
    assert len(_kept([f], line)) == 1


# ---------------------------------------------------------------------------
# PHONE_NUMBER
# ---------------------------------------------------------------------------


def test_drops_yarn_lock_semver():
    line = '  version "16.43.2"'
    f = _f("PHONE_NUMBER", "16.43.2", line, score=0.4)
    assert _kept([f], line) == []


def test_drops_long_caniuse_pseudo_version():
    line = '  caniuse-lite "^1.0.30001093"'
    f = _f("PHONE_NUMBER", "30001093", line, score=0.4)
    assert _kept([f], line) == []


def test_drops_github_pr_id_in_markdown_link():
    line = "(Yosuke Furukawa) [#1694](https://github.com/nodejs/io.js/pull/1694)"
    f = _f("PHONE_NUMBER", "1694", line, score=0.4)
    assert _kept([f], line) == []


def test_drops_date_fragment():
    line = "v. 2.0.16 2015-02-16 release"
    f = _f("PHONE_NUMBER", "16 2015-02-16", line, score=0.4)
    assert _kept([f], line) == []


def test_keeps_real_japanese_mobile_phone():
    line = "連絡先: 090-1234-5678 まで"
    f = _f("PHONE_NUMBER", "090-1234-5678", line, score=0.7)
    assert len(_kept([f], line)) == 1


# ---------------------------------------------------------------------------
# PERSON (spaCy ja_ner_ja FPs)
# ---------------------------------------------------------------------------


def test_drops_person_with_backtick_in_match():
    # nodejs-ja weekly/2015-05-08.md
    line = "  - `npm outdated` や `npm update` を実行する際に"
    f = _f("PERSON", "や `npm update`", line)
    assert _kept([f], line) == []


def test_drops_person_match_crossing_line_boundary():
    # pep8-ja: spaCy returned a multi-paragraph PERSON span
    matched = "や ``AnyStr`` や ``Num``\n\nグローバル変数の名前"
    snippet = matched.splitlines()[0]
    f = _f("PERSON", matched, snippet)
    assert _kept([f], matched) == []


def test_drops_person_inside_inline_code_span():
    line = "演算子 ``is not`` を使うべきです"
    f = _f("PERSON", "is not", line)
    assert _kept([f], line) == []


def test_keeps_real_japanese_person_name():
    line = "著者: 山田太郎"
    f = _f("PERSON", "山田太郎", line, score=0.8)
    assert len(_kept([f], line)) == 1


# ---------------------------------------------------------------------------
# Untouched entities pass through
# ---------------------------------------------------------------------------


def test_passes_through_email():
    line = "Author: Guido van Rossum <guido@python.org>,"
    f = _f("EMAIL_ADDRESS", "guido@python.org", line, score=0.9)
    assert len(_kept([f], line)) == 1


def test_passes_through_unrelated_entity_unchanged():
    line = "MyNumber 1234-5678-9012"
    f = _f("MY_NUMBER", "1234-5678-9012", line, score=0.5)
    assert len(_kept([f], line)) == 1
