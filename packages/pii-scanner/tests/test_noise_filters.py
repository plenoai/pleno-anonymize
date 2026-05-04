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
# Issue #102: PERSON_LATIN recall booster — keep author-context names, drop
# title-case prose. The recognizer is intentionally permissive; noise_filters
# is what makes it safe to ship.
# ---------------------------------------------------------------------------


def test_keeps_latin_name_with_email_in_window():
    f = _f(
        "PERSON",
        "Guido van Rossum",
        "| Author: Guido van Rossum <guido@python.org>,",
        score=0.3,
    )
    assert _kept([f], f.snippet) == [f]


def test_keeps_latin_name_with_pr_link_changelog_pattern():
    """nodejs-ja "(Name) [#1313]" credits."""
    line = "- 修正されました。(Yosuke Furukawa) [#1313](https://github.com/...)"
    f = _f("PERSON", "Yosuke Furukawa", line, score=0.3)
    assert _kept([f], line) == [f]


def test_keeps_latin_name_with_author_marker():
    line = "Author: Barry Warsaw"
    f = _f("PERSON", "Barry Warsaw", line, score=0.3)
    assert _kept([f], line) == [f]


def test_drops_apache_license_in_readme():
    line = "Licensed under the Apache License, Version 2.0."
    f = _f("PERSON", "Apache License", line, score=0.3)
    assert _kept([f], line) == []


def test_drops_pull_request_in_contributing():
    line = "Open a Pull Request against main."
    f = _f("PERSON", "Pull Request", line, score=0.3)
    assert _kept([f], line) == []


def test_drops_hello_world_in_quickstart():
    line = "Run the Hello World example."
    f = _f("PERSON", "Hello World", line, score=0.3)
    assert _kept([f], line) == []


def test_drops_latin_title_case_without_context():
    """Plain title-case bigrams in normal Japanese prose stay suppressed."""
    line = "詳細は New York のドキュメントを参照してください。"
    f = _f("PERSON", "New York", line, score=0.3)
    assert _kept([f], line) == []


def test_keeps_high_score_ner_latin_name_even_without_context():
    """ja_ner_ja confidence ≥ 0.6 bypasses the context gate — the model
    rarely hallucinates ASCII names at that score, and human review is
    cheaper than a silent drop."""
    line = "Speakers: Some Person."
    f = _f("PERSON", "Some Person", line, score=0.85)
    assert _kept([f], line) == [f]


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


# ---------------------------------------------------------------------------
# EMAIL_ADDRESS — strict re-validation + reserved domains.
# Round 2 cases from claude-org-ja.
# ---------------------------------------------------------------------------


def test_drops_email_with_cli_prefix():
    line = '          git -C "$d" -c user.email=ci@example.com -c user.name=CI'
    f = _f("EMAIL_ADDRESS", "user.email=ci@example.com", line, score=0.95)
    assert _kept([f], line) == []


def test_drops_email_with_url_path_prefix():
    line = "pip install git+https://github.com/suisya-systems/core-harness@v0.x.y"
    f = _f("EMAIL_ADDRESS", "github.com/suisya-systems/core-harness@v0.x.y",
           line, score=0.95)
    assert _kept([f], line) == []


def test_drops_email_with_reserved_example_com():
    line = "test@example.com is a reserved fake address"
    f = _f("EMAIL_ADDRESS", "test@example.com", line, score=0.95)
    assert _kept([f], line) == []


def test_drops_email_with_version_shape_domain():
    # Pip URL with no slash but with version-shape final label.
    line = "core-harness@v0.1.0"
    f = _f("EMAIL_ADDRESS", "core-harness@v0.1.0", line, score=0.95)
    assert _kept([f], line) == []


def test_keeps_real_user_email():
    line = "Author: <foo.bar@example.test.org>"
    f = _f("EMAIL_ADDRESS", "foo.bar@example.test.org", line, score=0.95)
    assert len(_kept([f], line)) == 1


# ---------------------------------------------------------------------------
# ISBN-13 / ASIN / book-URL noise.
# Round 2 cases from awesome-ai-red-teaming-jp.
# ---------------------------------------------------------------------------


def test_drops_isbn13_my_number_corporate():
    line = "- [Textbook](https://www.books.or.jp/book-details/9784911384039)"
    f = _f("MY_NUMBER_CORPORATE", "9784911384039", line, score=0.3,
           verification="failed")
    assert _kept([f], line) == []


def test_drops_amazon_asin_phone():
    line = "- [AI White Paper](https://www.amazon.co.jp/dp/4049112388) - By"
    f = _f("PHONE_NUMBER", "4049112388", line, score=0.4)
    assert _kept([f], line) == []


def test_drops_jwt_iat_claim_as_phone():
    line = '  "iat": 1654514191,'
    f = _f("PHONE_NUMBER", "1654514191", line, score=0.4)
    assert _kept([f], line) == []


def test_drops_jwt_jti_claim_as_phone():
    line = '  "jti": "1654514191",'
    f = _f("PHONE_NUMBER", "1654514191", line, score=0.4)
    assert _kept([f], line) == []


def test_keeps_10_digit_phone_without_jwt_context():
    # 10 digits is unusual for a JP phone number, but a 10-digit phone
    # in a non-JWT context (e.g. an international format mention) should
    # not be silently dropped just because the value happens to fall in
    # the Unix timestamp range. Without JWT keywords, keep.
    line = "発信元: 1654514191 まで折り返し"
    f = _f("PHONE_NUMBER", "1654514191", line, score=0.4)
    assert len(_kept([f], line)) == 1


# ---------------------------------------------------------------------------
# PERSON / ORGANIZATION leader / digit / paren / box-drawing FPs.
# Round 2 cases from claude-org-ja.
# ---------------------------------------------------------------------------


def test_drops_person_with_non_name_leader():
    line = "凡例: ◎ 高度に実装 / ○ 実装あり"
    f = _f("PERSON", "◎ 高度", line)
    assert _kept([f], line) == []


def test_drops_person_with_digit_in_match():
    line = "| LOC 見積もり | ~120（既存 ~120 + 拡張 ~30） | ~120（新規） |"
    f = _f("PERSON", "~120（新規）", line)
    assert _kept([f], line) == []


def test_drops_person_with_paren_in_match():
    line = "プロンプト泥棒（Zenn）です"
    f = _f("PERSON", "（Zenn）", line)
    assert _kept([f], line) == []


def test_keeps_organization_with_trailing_markdown_bracket():
    # fnwiya/japanese-rust-companies recall miss — spaCy captures the trailing
    # ``]`` of ``[株式会社ユーザベース](https://...)``. The closing bracket is
    # tolerable spaCy bleed, the org name itself is genuine PII candidate.
    line = "- [株式会社ユーザベース](https://www.uzabase.com/)"
    f = _f("ORGANIZATION", "株式会社ユーザベース]", line, score=0.85)
    assert len(_kept([f], line)) == 1


def test_drops_organization_when_match_extends_into_url():
    # When the NER span keeps going past "](" into the URL itself, the match
    # contains an OPENING bracket "(" mid-span — drop, the boundary is wrong.
    line = "- [株式会社 LabBase ](https://labbase.co.jp/)"
    f = _f("ORGANIZATION", "株式会社 LabBase ](https://labbase.co.jp/)", line, score=0.85)
    assert _kept([f], line) == []


def test_drops_organization_box_drawing():
    line = "   │   ├─ 指示送信 ──────────────────>│"
    f = _f("ORGANIZATION", "─────────────────>│", line)
    assert _kept([f], line) == []


def test_keeps_real_organization():
    line = "総務省から発表された報告書"
    f = _f("ORGANIZATION", "総務省", line, score=0.7)
    assert len(_kept([f], line)) == 1


# ---------------------------------------------------------------------------
# UniDic short-common-noun filter (issue #101).
#
# Drops short PERSON/ORG candidates whose morphology Sudachi parses as a
# common noun. Audited via Sudachi/UniDic dictionary lookup, not a
# surface-form blacklist.
# ---------------------------------------------------------------------------


def test_drops_person_unidic_common_noun_oomoji():
    # mumumu/pep8-ja@index.rst:661 — root cause of issue #101.
    line = "コメントは大文字にすべきです"
    f = _f("PERSON", "大文字", line)
    assert _kept([f], line) == []


def test_drops_person_unidic_common_noun_komoji():
    line = "全ての文字を大文字にします"
    f = _f("PERSON", "小文字", line)
    assert _kept([f], line) == []


def test_drops_person_unidic_common_noun_moji():
    line = "全ての文字を大文字にします"
    f = _f("PERSON", "文字", line)
    assert _kept([f], line) == []


def test_drops_person_unidic_common_noun_namae():
    line = "関数の名前は動詞句にする"
    f = _f("PERSON", "名前", line)
    assert _kept([f], line) == []


def test_drops_person_unidic_common_noun_zankaku():
    # Generalises beyond hard-coded suffix list — Sudachi tags 半角 as 普通名詞.
    line = "半角スペースで区切る"
    f = _f("PERSON", "半角", line)
    assert _kept([f], line) == []


def test_keeps_unidic_proper_noun_yamada():
    # 山田 is UniDic 名詞-固有名詞-人名-姓 → must NOT be filtered.
    line = "著者: 山田"
    f = _f("PERSON", "山田", line, score=0.8)
    assert len(_kept([f], line)) == 1


def test_keeps_unidic_place_name_tokyo():
    # 東京 is 名詞-固有名詞-地名 → not a common noun, must pass through.
    line = "本社は東京にある"
    f = _f("ORGANIZATION", "東京", line, score=0.7)
    assert len(_kept([f], line)) == 1


def test_keeps_organization_short_common_noun_tag():
    # 総務省 is UniDic 名詞-普通名詞 but legitimately an ORGANIZATION.
    # The UniDic filter is PERSON-scoped to avoid over-filtering ORG.
    line = "総務省から発表された"
    f = _f("ORGANIZATION", "総務省", line, score=0.7)
    assert len(_kept([f], line)) == 1


def test_drops_person_with_assignment_suffix():
    line = "適切なワーカーに作業を割り当てる"
    f = _f("PERSON", "割り当てる", line)
    assert _kept([f], line) == []


def test_drops_person_with_list_suffix():
    line = "| `workItems` | `array` | 作業アイテム一覧 |"
    f = _f("PERSON", "アイテム一覧", line)
    assert _kept([f], line) == []


def test_drops_organization_with_invocation_suffix():
    line = "副作用なしで呼び出せるツールについてはサンプル呼び出しで応答"
    f = _f("ORGANIZATION", "サンプル呼び出し", line)
    assert _kept([f], line) == []


def test_drops_person_with_residual_suffix():
    line = "Minor / Nit は原則残置。README"
    f = _f("PERSON", "原則残置", line)
    assert _kept([f], line) == []


def test_keeps_real_japanese_name_with_normal_kanji():
    # Family name + given name — neither token is in the suffix list.
    line = "著者: 田中太郎 と 山田花子"
    f1 = _f("PERSON", "田中太郎", line, score=0.8)
    f2 = _f("PERSON", "山田花子", line, score=0.8)
    assert len(_kept([f1, f2], line)) == 2
