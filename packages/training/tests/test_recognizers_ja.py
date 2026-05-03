"""Tests for `recognizers_ja` (now canonical at `server/src/recognizers_ja.py`).

Focus: regex correctness of pattern recognizers, with emphasis on the newly
added BANK_ACCOUNT recognizer (issue #49) which must achieve f1 >= 0.85 on
the v0.12.0 benchmark.

Tests use raw `re.compile` against `Pattern.regex` rather than spinning up a
full Presidio AnalyzerEngine — this keeps the suite fast and isolates the
regex from analyzer-level overlap resolution.

Module relocated to `server/src/` (#74) to break the server→training
workspace-dep loop. `conftest.py` injects that path onto `sys.path` so the
bare `import recognizers_ja` below resolves without re-introducing a
package-level dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from recognizers_ja import (
    ALL_JA_RECOGNIZERS,
    JA_BANK_ACCOUNT_PATTERNS,
    JapaneseBankAccountRecognizer,
)


# ---------- registry sanity ----------------------------------------------


def test_bank_account_recognizer_registered():
    assert JapaneseBankAccountRecognizer in ALL_JA_RECOGNIZERS


def test_bank_account_recognizer_metadata():
    # Presidio stores `supported_entity` as a single-element `supported_entities`
    # list. We assert via the public attribute to remain forward-compatible.
    assert JapaneseBankAccountRecognizer.supported_entities == ["BANK_ACCOUNT"]
    assert JapaneseBankAccountRecognizer.supported_language == "ja"


# ---------- compiled regex (used as a single union) ----------------------


@pytest.fixture(scope="module")
def bank_pattern() -> re.Pattern[str]:
    union = "|".join(p.regex for p in JA_BANK_ACCOUNT_PATTERNS)
    return re.compile(union)


# ---------- positive cases -----------------------------------------------


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
        # 信託銀行 (must come before 三井住友 in alternation)
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


# ---------- negative cases -----------------------------------------------


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


# ---------- benchmark-driven f1 ------------------------------------------


_BENCHMARK_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "benchmark"
    / "v0.12.0"
    / "ja"
    / "raw.json"
)


@pytest.mark.skipif(not _BENCHMARK_PATH.exists(), reason="benchmark data missing")
def test_bank_account_f1_on_v0_12_0_benchmark(bank_pattern: re.Pattern[str]):
    """Recognizer must achieve f1 >= 0.85 on v0.12.0 BANK_ACCOUNT spans."""
    docs = json.loads(_BENCHMARK_PATH.read_text(encoding="utf-8"))
    tp = fp = fn = 0
    for doc in docs:
        text = doc["text"]
        gold = {
            (e["start"], e["end"])
            for e in doc.get("entities", [])
            if e["label"] == "BANK_ACCOUNT"
        }
        pred = {(m.start(), m.end()) for m in bank_pattern.finditer(text)}
        tp += len(gold & pred)
        fp += len(pred - gold)
        fn += len(gold - pred)

    if tp + fp + fn == 0:
        pytest.skip("no BANK_ACCOUNT entities in benchmark")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    )
    assert f1 >= 0.85, (
        f"BANK_ACCOUNT recognizer f1={f1:.3f} (P={precision:.3f}, "
        f"R={recall:.3f}, tp={tp}, fp={fp}, fn={fn}) below required 0.85"
    )
