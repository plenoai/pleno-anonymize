"""Regex correctness for the BANK_ACCOUNT recognizer (#49).

Tests the union of `JA_BANK_ACCOUNT.patterns` directly (no Presidio engine) so
that boundary cases — bank-name alternation order, 当座/普通 variants, ゆうちょ
記号-番号 layout, 7- vs. 8-digit accounts — are pinned independently of any
analyzer-level overlap resolution.
"""

from __future__ import annotations

import re

import pytest

from pleno_recognizers.ja import ALL_JA_RECOGNIZERS, JA_BANK_ACCOUNT


def test_bank_account_recognizer_registered():
    assert JA_BANK_ACCOUNT in ALL_JA_RECOGNIZERS
    assert JA_BANK_ACCOUNT.entity == "BANK_ACCOUNT"
    assert JA_BANK_ACCOUNT.language == "ja"


@pytest.fixture(scope="module")
def bank_pattern() -> re.Pattern[str]:
    union = "|".join(p.regex for p in JA_BANK_ACCOUNT.patterns)
    return re.compile(union)


@pytest.mark.parametrize(
    "text,expected",
    [
        # benchmark-style: <bank>銀行<branch>支店(普通|当座)<7-8 digits>
        ("みずほ銀行渋谷支店普通1234567", "みずほ銀行渋谷支店普通1234567"),
        ("りそな銀行日本橋支店普通5566778", "りそな銀行日本橋支店普通5566778"),
        ("楽天銀行第二営業支店普通4567890", "楽天銀行第二営業支店普通4567890"),
        ("三井住友銀行品川支店普通7722114", "三井住友銀行品川支店普通7722114"),
        ("三井住友銀行梅田支店普通8844221", "三井住友銀行梅田支店普通8844221"),
        ("京都銀行四条支店普通6611223", "京都銀行四条支店普通6611223"),
        # 当座 (current account) variant
        ("みずほ銀行本店当座9876543", "みずほ銀行本店当座9876543"),
        # 信託銀行 — must be matched before plain 三井住友 in the alternation
        (
            "三井住友信託銀行東京営業部普通1112223",
            "三井住友信託銀行東京営業部普通1112223",
        ),
        # ネット銀行 (alphanumeric prefix)
        (
            "GMOあおぞらネット銀行法人営業支店普通1234567",
            "GMOあおぞらネット銀行法人営業支店普通1234567",
        ),
        ("PayPay銀行ビジネス営業部普通1234567", "PayPay銀行ビジネス営業部普通1234567"),
        # 8桁 account variant
        ("三菱UFJ銀行新宿支店普通12345678", "三菱UFJ銀行新宿支店普通12345678"),
        # ゆうちょ
        ("ゆうちょ銀行記号10100番号12345671", "ゆうちょ銀行記号10100番号12345671"),
        ("ゆうちょ銀行記号10180番号12345678", "ゆうちょ銀行記号10180番号12345678"),
        # surrounded by Japanese context
        (
            "振込先はみずほ銀行渋谷支店普通1234567です。",
            "みずほ銀行渋谷支店普通1234567",
        ),
    ],
)
def test_bank_account_positive(bank_pattern: re.Pattern[str], text: str, expected: str):
    matches = [m.group(0) for m in bank_pattern.finditer(text)]
    assert expected in matches, f"expected {expected!r} in {matches!r}"


@pytest.mark.parametrize(
    "text",
    [
        # bank mention without account number
        "三菱UFJ銀行で口座を開設しました",
        "みずほ銀行渋谷支店",
        # phone, not bank
        "電話番号 03-1234-5678",
        # bare numbers
        "私は1234567をパスワードにした",
        "資本金1000万円",
        # bank-adjacent vocabulary
        "銀行員と話した",
        "中央銀行の総裁",
        # unknown bank name (alternation must reject)
        "ABC銀行渋谷支店普通1234567",
        "テスト銀行新宿支店普通1234567",
        # account-only without bank prefix
        "普通1234567",
        # ゆうちょ format with too-short 記号
        "ゆうちょ銀行記号101番号12345671",
        # 当座/普通 with too-short tail (6 digits)
        "みずほ銀行渋谷支店普通123456",
    ],
)
def test_bank_account_negative(bank_pattern: re.Pattern[str], text: str):
    matches = [m.group(0) for m in bank_pattern.finditer(text)]
    assert matches == [], f"unexpected match {matches!r} in {text!r}"
