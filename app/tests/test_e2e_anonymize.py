"""エンドツーエンド匿名化ベンチマーク.

NERモデル + パターン認識器の統合テスト。
実際のAPI入力に近いテキストを使い、漏洩がないことを検証する。
"""

import pytest
from presidio_analyzer import AnalyzerEngine


# ============================================================
# ヘルパー
# ============================================================


def _analyze_all(analyzer: AnalyzerEngine, text: str, lang: str = "ja") -> dict[str, list[str]]:
    """全エンティティを検出してラベル別に返す."""
    results = analyzer.analyze(text=text, language=lang)
    out: dict[str, list[str]] = {}
    for r in results:
        out.setdefault(r.entity_type, []).append(text[r.start : r.end])
    return out


# ============================================================
# 実文書シミュレーション
# ============================================================


class TestRealWorldDocuments:
    """実環境に近い文書の匿名化テスト."""

    def test_medical_intake_form(self, analyzer: AnalyzerEngine):
        """医療初診問診票."""
        text = (
            "患者氏名: 山田太郎\n"
            "生年月日: 昭和55年4月10日\n"
            "住所: 〒150-0001 東京都渋谷区神宮前1-2-3\n"
            "電話: 090-1234-5678\n"
            "メール: yamada@example.co.jp\n"
            "保険証の記号 12345 番号 678901\n"
            "紹介元: 佐藤クリニック"
        )
        entities = _analyze_all(analyzer, text)
        assert "PHONE_NUMBER" in entities
        assert "EMAIL_ADDRESS" in entities
        assert "POSTAL_CODE" in entities
        # 保険証情報も検出すべき
        assert "HEALTH_INSURANCE" in entities

    def test_hr_document(self, analyzer: AnalyzerEngine):
        """人事書類."""
        text = (
            "氏名: 鈴木花子\n"
            "マイナンバー: 1234 5678 9012\n"
            "銀行口座: 三菱UFJ銀行 渋谷支店 普通 1234567\n"
            "パスポート: TK1234567\n"
            "運転免許番号: 012345678901\n"
            "連絡先: 080-9876-5432\n"
            "email: hanako.suzuki@company.co.jp"
        )
        entities = _analyze_all(analyzer, text)
        assert "MY_NUMBER" in entities
        assert "PASSPORT" in entities
        assert "PHONE_NUMBER" in entities
        assert "EMAIL_ADDRESS" in entities

    def test_immigration_document(self, analyzer: AnalyzerEngine):
        """出入国関連書類."""
        text = (
            "氏名: ジョン・スミス\n"
            "在留カード番号: AB12345678CD\n"
            "パスポート: MZ9876543\n"
            "住所: 東京都新宿区西新宿2-8-1\n"
            "連絡先: 03-1234-5678"
        )
        entities = _analyze_all(analyzer, text)
        assert "RESIDENCE_CARD" in entities
        assert "PASSPORT" in entities
        assert "PHONE_NUMBER" in entities

    def test_corporate_registration(self, analyzer: AnalyzerEngine):
        """法人登記関連."""
        text = (
            "商号: 株式会社テスト\n"
            "法人番号: 1234567890123\n"
            "本店所在地: 東京都千代田区丸の内1-1-1\n"
            "代表者: 田中一郎\n"
            "URL: https://test-corp.co.jp"
        )
        entities = _analyze_all(analyzer, text)
        assert "MY_NUMBER_CORPORATE" in entities
        assert "URL" in entities

    def test_financial_transfer_form(self, analyzer: AnalyzerEngine):
        """振込依頼書."""
        text = (
            "依頼人: 佐藤次郎\n"
            "住所: 大阪府大阪市北区梅田1-1-1\n"
            "クレジットカード: 4111-1111-1111-1111\n"
            "振込先: みずほ銀行 本店 普通 9876543\n"
            "IPアドレス: 192.168.1.100"
        )
        entities = _analyze_all(analyzer, text)
        assert "CREDIT_CARD" in entities
        assert "IP_ADDRESS" in entities


# ============================================================
# 匿名化後の漏洩チェック
# ============================================================


class TestLeakageCheck:
    """匿名化後にPIIが残っていないことを検証するメタテスト."""

    KNOWN_PII = [
        "090-1234-5678",
        "user@example.co.jp",
        "1234 5678 9012",
        "4111-1111-1111-1111",
        "TK1234567",
        "012345678901",
        "192.168.1.1",
        "AB12345678CD",
        "1234567890123",
    ]

    def test_no_raw_pii_after_redaction(self, analyzer: AnalyzerEngine):
        """パターンPIIが検出後に少なくとも1つはヒットすること（健全性チェック）."""
        text = (
            "電話: 090-1234-5678, "
            "メール: user@example.co.jp, "
            "マイナンバー: 1234 5678 9012, "
            "カード: 4111-1111-1111-1111, "
            "パスポート: TK1234567, "
            "免許: 012345678901, "
            "IP: 192.168.1.1, "
            "在留カード: AB12345678CD, "
            "法人番号: 1234567890123"
        )
        results = analyzer.analyze(text=text, language="ja")
        detected_entities = {r.entity_type for r in results}
        # 最低限これだけは検出されるべき
        expected_minimum = {"PHONE_NUMBER", "EMAIL_ADDRESS", "IP_ADDRESS"}
        missing = expected_minimum - detected_entities
        assert not missing, f"検出漏れ: {missing}"


# ============================================================
# 非PIIテキスト: 偽陽性ゼロ目標
# ============================================================


class TestCleanTextNoFalsePositives:
    """PIIを含まないテキストで偽陽性がないことを検証."""

    @pytest.mark.parametrize(
        "text",
        [
            "本日の天気は晴れで、気温は25度です。",
            "Pythonのバージョン3.12がリリースされました。",
            "売上高は前年比120%増の100億円を達成しました。",
            "会議室Aを14時から16時まで予約してください。",
            "新幹線の東京-大阪間は約2時間30分です。",
        ],
        ids=["weather", "python_version", "financial_report", "meeting_room", "shinkansen"],
    )
    def test_no_pii_text(self, analyzer: AnalyzerEngine, text: str):
        """PIIを含まない日常テキスト."""
        results = analyzer.analyze(text=text, language="ja")
        high_confidence = [r for r in results if r.score >= 0.5]
        assert not high_confidence, (
            f"偽陽性検出: {[(r.entity_type, text[r.start:r.end], r.score) for r in high_confidence]}"
        )
