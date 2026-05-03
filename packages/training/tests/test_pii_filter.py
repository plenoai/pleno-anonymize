"""Unit tests for the zero-entity hard-negative PII filter (#67).

`PII_HINT_RE` lives in `convert_to_hf_dataset.py` and is the gate that
decides whether an `entities=[]` document is kept as an all-O hard-negative
or dropped as label-miss noise. Letting label-miss docs through teaches the
model to *not* tag real PII — exactly the regression observed in #66 (hardneg
run vs. baseline: ADDRESS F1 0.692 → 0.667, BANK_ACCOUNT F1 0.615 → 0.558).

These tests pin the regex's intent: each PII surface form that the original
v0 set missed (postal codes, phone numbers, email, account numbers, the
remaining 41 prefectures, ISO-style birthdates, …) is asserted to match,
and a curated set of clearly non-PII strings is asserted NOT to match. If
someone narrows or removes a branch in the future, the test will tell us
which PII class regressed.
"""

from __future__ import annotations

import pytest

from pleno_ner_training.convert_to_hf_dataset import PII_HINT_RE


# ---------------------------------------------------------------------------
# Strings that MUST match — each corresponds to a PII class we cannot leak
# into the hard-negative pool.
# ---------------------------------------------------------------------------

POSITIVE_CASES: list[tuple[str, str]] = [
    # --- legacy patterns (kept for back-compat) ------------------------------
    ("legacy/digit-cluster-3", "電話 03-1234-5678 までご連絡ください。"),
    ("legacy/digit-cluster-grouped", "受付番号 12345 - 67890 です。"),
    ("legacy/honorific-name", "山田 太郎さんのご来店です。"),
    ("legacy/corp", "株式会社サンプル商事と契約しました。"),
    ("legacy/era-date", "令和5年に入社しました。"),
    ("legacy/prefecture-tokyo", "東京都港区赤坂"),
    # --- #67: postal code ----------------------------------------------------
    ("postal/with-hyphen", "〒100-0001 千代田区千代田1-1"),
    ("postal/no-hyphen", "〒1000001"),
    ("postal/with-space", "〒 100-0001"),
    # --- #67: Japanese phone -------------------------------------------------
    ("phone/landline", "0120-456-7890 までお電話ください。"),
    ("phone/mobile", "携帯は090-1234-5678です。"),
    ("phone/long-area", "問い合わせ 03-1234-5678"),
    # --- #67: email ----------------------------------------------------------
    ("email/basic", "連絡先は taro.yamada@example.com です。"),
    ("email/plus-tag", "support+pii@example.co.jp"),
    # --- #67: financial identifiers -----------------------------------------
    ("card/16-spaced", "カード番号 4111 1111 1111 1111"),
    ("card/16-hyphen", "4111-1111-1111-1111"),
    ("card/16-solid", "4111111111111111"),
    ("iban/de", "IBAN: DE89370400440532013000"),
    ("account/koza", "口座番号 1234567"),
    ("account/futsuu", "普通 7654321"),
    ("account/touza", "当座: 9876543"),
    ("bank/with-digits", "三菱UFJ銀行渋谷支店 0123456"),
    ("bank/yuucho", "ゆうちょ銀行記号10100 番号12345678"),
    # --- #67: more prefectures (the original list missed 41 of them) -------
    ("prefecture/kanagawa", "神奈川県横浜市"),
    ("prefecture/aichi", "愛知県名古屋市"),
    ("prefecture/fukuoka", "福岡県福岡市中央区"),
    ("prefecture/hiroshima", "広島県広島市中区基町"),
    # --- #67: ward + chome ---------------------------------------------------
    ("address/ward-chome", "中区基町10丁目"),
    # --- #67: banchi / go ----------------------------------------------------
    ("address/banchi", "1-2-3 のビルです。"),
    # --- #67: ISO-style birthdate -------------------------------------------
    ("dob/iso", "生年月日は1990年4月1日です。"),
    ("dob/iso-spaced", "1992 年 4 月 10 日"),
    # --- #67: name + furigana ------------------------------------------------
    ("name/furigana", "田村健司（タムラケンジ）"),
    # --- #67: 名義 + name ----------------------------------------------------
    ("meigi/kanji", "名義はヤマダタロウ"),
    # --- #67: residual prompt-template tags ---------------------------------
    ("tag/xml-person", "<PERSON>木村優香</PERSON>"),
    ("tag/xml-address", "<ADDRESS>福岡県福岡市中央区</ADDRESS>"),
    ("tag/brace-person", "《PERSON>木村優香</PERSON》"),
]


# ---------------------------------------------------------------------------
# Strings that MUST NOT match — typical clean Japanese with no PII signal.
# These guard against the regex turning into a "drop everything" wildcard.
# ---------------------------------------------------------------------------

NEGATIVE_CASES: list[tuple[str, str]] = [
    ("neutral/diary", "昨日は雨が降ったので家で本を読んでいた。"),
    ("neutral/refusal", "ご依頼にはお応えできません。代わりに別の文章を作成します。"),
    ("neutral/short", "わかりました。"),
    ("neutral/numbers-but-not-pii", "売上は120個でした。"),
    ("neutral/year-only", "2025年に発表されました。"),  # year alone is OK
    ("neutral/era-no-digits", "昭和の風景"),  # era without 年-digits
]


@pytest.mark.parametrize("case_id,text", POSITIVE_CASES, ids=[c[0] for c in POSITIVE_CASES])
def test_pii_hint_re_matches_pii_surface_form(case_id: str, text: str) -> None:
    """Every PII surface form we care about must trigger PII_HINT_RE.

    Failure here means the regex would let a label-miss zero-entity doc into
    the hard-negative pool, recreating the #66 ADDRESS / BANK_ACCOUNT
    regression.
    """
    assert PII_HINT_RE.search(text), (
        f"[{case_id}] expected PII match in: {text!r}"
    )


@pytest.mark.parametrize("case_id,text", NEGATIVE_CASES, ids=[c[0] for c in NEGATIVE_CASES])
def test_pii_hint_re_does_not_match_clean_text(case_id: str, text: str) -> None:
    """Clean prose without PII must pass through (kept as hard-negative)."""
    assert not PII_HINT_RE.search(text), (
        f"[{case_id}] unexpected PII match in: {text!r}"
    )


def test_pii_hint_re_drop_rate_floor_on_ja_v02() -> None:
    """Regression guard for #67 acceptance criterion.

    On `data/raw/ja-v02/generated.json` the expanded regex must drop at least
    ~25% of zero-entity docs (the original v0 set dropped 16.4%). We pin a
    conservative 22% floor so non-PII edits to the data file don't break the
    test, while still failing loudly if a regex branch is removed.

    Skipped when the data file is unavailable (CI without LFS).
    """
    import json
    from pathlib import Path

    data_path = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "raw"
        / "ja-v02"
        / "generated.json"
    )
    if not data_path.exists():
        pytest.skip(f"raw data not available: {data_path}")

    with open(data_path, encoding="utf-8") as f:
        records = json.load(f)

    zero = [r for r in records if not r.get("entities") and r.get("text")]
    if not zero:
        pytest.skip("no zero-entity docs in raw data")

    dropped = sum(1 for r in zero if PII_HINT_RE.search(r["text"]))
    rate = dropped / len(zero)
    assert rate >= 0.22, (
        f"PII_HINT_RE drop rate regressed: {rate:.3f} "
        f"(zero={len(zero)}, drop={dropped}); expected >= 0.22"
    )
