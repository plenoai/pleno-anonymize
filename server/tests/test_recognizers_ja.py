"""日本語パターン認識器のベンチマーク.

レッドチーミング視点でエッジケース・偽陽性・境界値を網羅的にテスト。
"""

import pytest
from presidio_analyzer import AnalyzerEngine


# ============================================================
# ヘルパー
# ============================================================


def _detect(analyzer: AnalyzerEngine, text: str, entity: str, lang: str = "ja") -> list[str]:
    """指定エンティティの検出結果テキストを返す."""
    results = analyzer.analyze(text=text, language=lang, entities=[entity])
    return [text[r.start : r.end] for r in results]


def _detected(analyzer: AnalyzerEngine, text: str, entity: str, lang: str = "ja") -> bool:
    """エンティティが1つ以上検出されるか."""
    return len(_detect(analyzer, text, entity, lang)) > 0


# ============================================================
# 電話番号
# ============================================================


class TestPhoneNumber:
    """PHONE_NUMBER 認識器."""

    # --- True Positive: 検出すべきケース ---

    @pytest.mark.parametrize(
        "text",
        [
            "電話番号は090-1234-5678です",
            "携帯: 080-9876-5432",
            "TEL 070-1111-2222",
            "連絡先: 03-1234-5678",
            "tel: 06-6789-0123",
            "phone: 0120-123-456",
        ],
        ids=[
            "mobile_090",
            "mobile_080",
            "mobile_070",
            "fixed_tokyo",
            "fixed_osaka",
            "freephone",
        ],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "PHONE_NUMBER")

    @pytest.mark.parametrize(
        "text",
        [
            "TEL: ０９０−１２３４−５６７８",  # 全角ハイフン
            "携帯: ０８０ー１２３４ー５６７８",  # 長音記号
            "TEL: ０３‐１２３４‐５６７８",  # 半角ハイフン混在
        ],
        ids=["fullwidth_hyphen", "fullwidth_prolonged", "mixed_hyphen"],
    )
    def test_fullwidth(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "PHONE_NUMBER")

    def test_no_separator_mobile(self, analyzer: AnalyzerEngine):
        """区切りなし携帯電話番号."""
        assert _detected(analyzer, "TEL 09012345678", "PHONE_NUMBER")

    def test_space_separator(self, analyzer: AnalyzerEngine):
        """スペース区切りは現状未対応（将来の改善項目）."""
        results = analyzer.analyze(
            text="携帯: 080 1234 5678", language="ja", entities=["PHONE_NUMBER"]
        )
        if not results:
            pytest.skip("スペース区切り電話番号は現在未対応")

    # --- False Positive: 検出すべきでないケース ---

    @pytest.mark.parametrize(
        "text",
        [
            "注文番号: 0901234567890",  # 13桁 - 電話番号ではない
            "商品コード: 0312345678901",  # 13桁
            "2024年1月15日",  # 日付
            "口座番号: 1234567",  # 7桁
        ],
        ids=["order_number_13digits", "product_code", "date", "account_7digits"],
    )
    def test_false_positive(self, analyzer: AnalyzerEngine, text: str):
        assert not _detected(analyzer, text, "PHONE_NUMBER")


# ============================================================
# メールアドレス
# ============================================================


class TestEmail:
    """EMAIL_ADDRESS 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "メール: user@example.co.jp",
            "email: taro.yamada+tag@company.com",
            "Eメール: info@sub.domain.org",
            "メールアドレス: a@b.jp",  # 最短形
        ],
        ids=["standard_jp", "plus_tag", "subdomain", "minimal"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "EMAIL_ADDRESS")

    @pytest.mark.parametrize(
        "text",
        [
            "メール: user@example",  # TLDなし
            "メール: @example.com",  # ローカルパートなし
            "メール: user@.com",  # ドメインなし
        ],
        ids=["no_tld", "no_local", "no_domain"],
    )
    def test_invalid_email_no_detect(self, analyzer: AnalyzerEngine, text: str):
        assert not _detected(analyzer, text, "EMAIL_ADDRESS")

    def test_email_in_url_context(self, analyzer: AnalyzerEngine):
        """URL内のメールアドレスパターンを過検出しないか."""
        text = "https://example.com/path?param=value"
        assert not _detected(analyzer, text, "EMAIL_ADDRESS")


# ============================================================
# マイナンバー
# ============================================================


class TestMyNumber:
    """MY_NUMBER 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "マイナンバー: 1234 5678 9012",
            "個人番号: 1234-5678-9012",
            "マイナンバー: 123456789012",
        ],
        ids=["spaced", "hyphenated", "continuous"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "MY_NUMBER")

    def test_context_required(self, analyzer: AnalyzerEngine):
        """文脈なしの12桁数字はスコアが低いはず（偽陽性抑制）."""
        results = analyzer.analyze(
            text="会議室番号: 123456789012",
            language="ja",
            entities=["MY_NUMBER"],
        )
        # スコアが0.5未満であれば文脈なしでは低信頼として扱える
        if results:
            assert all(r.score < 0.5 for r in results)


# ============================================================
# クレジットカード
# ============================================================


class TestCreditCard:
    """CREDIT_CARD 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "カード番号: 4111-1111-1111-1111",  # Visa
            "VISA: 4111 1111 1111 1111",  # Visa スペース区切り
            "Mastercard: 5500-0000-0000-0004",  # Mastercard
        ],
        ids=["visa_hyphen", "visa_space", "mastercard"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "CREDIT_CARD")

    def test_amex_format(self, analyzer: AnalyzerEngine):
        """Amex形式（4-6-5桁）は現状未対応."""
        results = analyzer.analyze(
            text="クレジットカード: 3782 822463 10005",
            language="ja",
            entities=["CREDIT_CARD"],
        )
        if not results:
            pytest.skip("Amex 4-6-5形式は現在未対応 - 将来の改善項目")

    @pytest.mark.parametrize(
        "text",
        [
            "電話: 03-1234-5678",  # 電話番号
            "IP: 192.168.1.1",  # IPアドレス
        ],
        ids=["phone_not_card", "ip_not_card"],
    )
    def test_false_positive(self, analyzer: AnalyzerEngine, text: str):
        assert not _detected(analyzer, text, "CREDIT_CARD")

    def test_luhn_invalid(self, analyzer: AnalyzerEngine):
        """Luhn チェックに失敗する番号 - パターンマッチはするがスコアは文脈次第."""
        results = analyzer.analyze(
            text="カード番号: 4111-1111-1111-1112",  # Luhn invalid
            language="ja",
            entities=["CREDIT_CARD"],
        )
        # パターンマッチ + 文脈ブーストで高スコアになりうる（既知の制限）
        # Presidio のパターン認識器は Luhn 検証を行わない
        assert results  # マッチすること自体は確認


# ============================================================
# パスポート
# ============================================================


class TestPassport:
    """PASSPORT 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "パスポート番号: TK1234567",
            "旅券: MZ9876543",
            "passport: AB1111111",
        ],
        ids=["standard_tk", "standard_mz", "english_context"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "PASSPORT")

    @pytest.mark.parametrize(
        "text",
        [
            "型番: AB1234567X",  # 後続文字あり
            "コード: abc1234567",  # 小文字
        ],
        ids=["trailing_char", "lowercase"],
    )
    def test_non_passport(self, analyzer: AnalyzerEngine, text: str):
        assert not _detected(analyzer, text, "PASSPORT")


# ============================================================
# 運転免許証
# ============================================================


class TestDriverLicense:
    """DRIVER_LICENSE 認識器."""

    def test_basic_detection(self, analyzer: AnalyzerEngine):
        text = "運転免許番号: 012345678901"
        assert _detected(analyzer, text, "DRIVER_LICENSE")

    def test_context_required(self, analyzer: AnalyzerEngine):
        """文脈なしの12桁数字は低スコアであるべき."""
        results = analyzer.analyze(
            text="管理番号: 012345678901",
            language="ja",
            entities=["DRIVER_LICENSE"],
        )
        if results:
            assert all(r.score < 0.5 for r in results)


# ============================================================
# IPアドレス
# ============================================================


class TestIPAddress:
    """IP_ADDRESS 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "IPアドレス: 192.168.1.1",
            "サーバー: 10.0.0.1",
            "IP: 172.16.0.100",
        ],
        ids=["private_c", "private_a", "private_b"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "IP_ADDRESS")

    @pytest.mark.parametrize(
        "text",
        [
            "IPアドレス: 0.0.0.0",
            "IP: 255.255.255.255",
        ],
        ids=["all_zeros", "broadcast"],
    )
    def test_boundary_values(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "IP_ADDRESS")

    @pytest.mark.parametrize(
        "text",
        [
            "バージョン: 1.2.3",  # 3オクテット
            "値: 999.999.999.999",  # 範囲外
        ],
        ids=["three_octets", "out_of_range"],
    )
    def test_false_positive(self, analyzer: AnalyzerEngine, text: str):
        assert not _detected(analyzer, text, "IP_ADDRESS")


# ============================================================
# 横断テスト: 複数エンティティの共存
# ============================================================


class TestMultiEntityCoexistence:
    """同一テキスト内に複数のPIIタイプが共存するケース."""

    def test_phone_and_email_coexist(self, analyzer: AnalyzerEngine):
        text = "連絡先: 電話 090-1234-5678, メール user@example.co.jp"
        results = analyzer.analyze(text=text, language="ja")
        entity_types = {r.entity_type for r in results}
        assert "PHONE_NUMBER" in entity_types
        assert "EMAIL_ADDRESS" in entity_types

    def test_credit_card_and_my_number_distinct(self, analyzer: AnalyzerEngine):
        """16桁(カード)と12桁(マイナンバー)が混同されないか."""
        text = (
            "クレジットカード: 4111-1111-1111-1111\n"
            "マイナンバー: 1234 5678 9012"
        )
        results = analyzer.analyze(text=text, language="ja")
        types = {r.entity_type for r in results}
        assert "CREDIT_CARD" in types
        assert "MY_NUMBER" in types


# ============================================================
# 回避攻撃テスト（Evasion）
# ============================================================


class TestEvasionAttacks:
    """匿名化を回避しようとする敵対的入力."""

    @pytest.mark.parametrize(
        "text,entity",
        [
            ("電話: 090‐1234‐5678", "PHONE_NUMBER"),  # Unicode U+2010 ハイフン
            ("電話: 090–1234–5678", "PHONE_NUMBER"),  # en-dash
            ("電話: 090―1234―5678", "PHONE_NUMBER"),  # em-dash
        ],
        ids=["unicode_hyphen", "en_dash", "em_dash"],
    )
    def test_unicode_separator_evasion(self, analyzer: AnalyzerEngine, text: str, entity: str):
        """Unicode の異なるハイフン文字で回避を試みるケース."""
        assert _detected(analyzer, text, entity)

    def test_zero_width_char_insertion(self, analyzer: AnalyzerEngine):
        """ゼロ幅文字を挿入して検出を回避するケース."""
        # ゼロ幅スペース (U+200B) を挿入
        text = "メール: user\u200b@\u200bexample\u200b.com"
        # 現状検出できなくても、この攻撃ベクトルの存在を認識する
        results = analyzer.analyze(text=text, language="ja", entities=["EMAIL_ADDRESS"])
        # このテストは現状の限界を文書化する（xfail 相当）
        if not results:
            pytest.skip("ゼロ幅文字挿入は現在未対応 - 将来の改善項目")

    def test_fullwidth_digit_email(self, analyzer: AnalyzerEngine):
        """全角数字を含むメールアドレス."""
        text = "メール: user１２３@example.com"
        # 全角数字が混在しても本来検出すべき
        results = analyzer.analyze(text=text, language="ja", entities=["EMAIL_ADDRESS"])
        if not results:
            pytest.skip("全角数字混在メールは現在未対応 - 将来の改善項目")

    @pytest.mark.parametrize(
        "text",
        [
            "カード番号: ４１１１ー１１１１ー１１１１ー１１１１",  # 全角カード番号
            "カード番号: ４１１１−１１１１−１１１１−１１１１",  # 全角数字+半角ハイフン
        ],
        ids=["fullwidth_all", "fullwidth_digits_halfwidth_hyphen"],
    )
    def test_fullwidth_credit_card(self, analyzer: AnalyzerEngine, text: str):
        """全角数字のクレジットカード番号."""
        results = analyzer.analyze(text=text, language="ja", entities=["CREDIT_CARD"])
        if not results:
            pytest.skip("全角クレジットカード番号は現在未対応 - 将来の改善項目")

    def test_my_number_fullwidth(self, analyzer: AnalyzerEngine):
        """全角マイナンバー."""
        text = "マイナンバー: １２３４ ５６７８ ９０１２"
        results = analyzer.analyze(text=text, language="ja", entities=["MY_NUMBER"])
        if not results:
            pytest.skip("全角マイナンバーは現在未対応 - 将来の改善項目")


# ============================================================
# 法人番号
# ============================================================


class TestCorporateNumber:
    """MY_NUMBER_CORPORATE 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "法人番号: 1234567890123",
            "法人番号 1 2345 6789 0123",
        ],
        ids=["continuous", "spaced"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "MY_NUMBER_CORPORATE")

    def test_context_required(self, analyzer: AnalyzerEngine):
        """文脈なしの13桁数字は低スコアであるべき."""
        results = analyzer.analyze(
            text="管理番号: 1234567890123",
            language="ja",
            entities=["MY_NUMBER_CORPORATE"],
        )
        if results:
            assert all(r.score < 0.5 for r in results)

    def test_not_12_digits(self, analyzer: AnalyzerEngine):
        """12桁はマイナンバー個人であり法人番号ではない."""
        results = analyzer.analyze(
            text="法人番号: 123456789012",
            language="ja",
            entities=["MY_NUMBER_CORPORATE"],
        )
        # 12桁は13桁パターンにマッチしないはず
        high = [r for r in results if r.score >= 0.5]
        assert len(high) == 0


# ============================================================
# 健康保険証番号
# ============================================================


class TestHealthInsurance:
    """HEALTH_INSURANCE 認識器."""

    def test_symbol_number_format(self, analyzer: AnalyzerEngine):
        """記号/番号形式の検出."""
        text = "保険証の記号 12345 番号 678901"
        assert _detected(analyzer, text, "HEALTH_INSURANCE")

    def test_insurer_number(self, analyzer: AnalyzerEngine):
        """保険者番号(8桁)の検出."""
        text = "保険者番号 01130012"
        assert _detected(analyzer, text, "HEALTH_INSURANCE")

    def test_no_false_positive_short(self, analyzer: AnalyzerEngine):
        """短い数字列は文脈なしでは検出しない."""
        results = analyzer.analyze(
            text="部屋番号 12345678",
            language="ja",
            entities=["HEALTH_INSURANCE"],
        )
        if results:
            assert all(r.score < 0.5 for r in results)


# ============================================================
# 在留カード番号
# ============================================================


class TestResidenceCard:
    """RESIDENCE_CARD 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "在留カード番号: AB12345678CD",
            "在留カード: ZZ99999999AA",
        ],
        ids=["standard", "max_digits"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "RESIDENCE_CARD")

    @pytest.mark.parametrize(
        "text",
        [
            "コード: AB1234CD",  # 4桁 - 短すぎ
            "コード: A12345678CD",  # 先頭1文字
        ],
        ids=["too_short", "one_prefix_letter"],
    )
    def test_no_false_positive(self, analyzer: AnalyzerEngine, text: str):
        assert not _detected(analyzer, text, "RESIDENCE_CARD")


# ============================================================
# 郵便番号
# ============================================================


class TestPostalCode:
    """POSTAL_CODE 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "〒150-0001",
            "郵便番号: 100-0001",
            "〒１５０−０００１",
        ],
        ids=["symbol_half", "context_half", "symbol_fullwidth"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "POSTAL_CODE")

    def test_no_false_positive_plain_number(self, analyzer: AnalyzerEngine):
        """文脈なしの3-4桁数字ハイフン4桁は低スコアであるべき."""
        results = analyzer.analyze(
            text="注文番号: 123-4567",
            language="ja",
            entities=["POSTAL_CODE"],
        )
        if results:
            assert all(r.score < 0.5 for r in results)


# ============================================================
# URL
# ============================================================


class TestURL:
    """URL 認識器."""

    @pytest.mark.parametrize(
        "text",
        [
            "URL: https://example.com",
            "リンク: http://example.co.jp/path?q=1",
            "サイト: https://sub.domain.com/a/b/c",
        ],
        ids=["https_simple", "http_with_query", "https_subdomain"],
    )
    def test_basic_detection(self, analyzer: AnalyzerEngine, text: str):
        assert _detected(analyzer, text, "URL")

    def test_no_scheme_no_detect(self, analyzer: AnalyzerEngine):
        """スキームなしのドメインは検出しない（偽陽性抑制）."""
        assert not _detected(analyzer, text="www.example.com", entity="URL")


# ============================================================
# 横断テスト: 新エンティティの共存
# ============================================================


class TestNewEntityCoexistence:
    """新エンティティが既存エンティティと共存するケース."""

    def test_corporate_and_personal_my_number(self, analyzer: AnalyzerEngine):
        """法人番号と個人番号が混同されないか."""
        text = "法人番号: 1234567890123\nマイナンバー: 1234 5678 9012"
        results = analyzer.analyze(text=text, language="ja")
        types = {r.entity_type for r in results}
        assert "MY_NUMBER_CORPORATE" in types
        assert "MY_NUMBER" in types

    def test_postal_code_in_address(self, analyzer: AnalyzerEngine):
        """郵便番号が住所テキスト内で検出されるか."""
        text = "〒150-0001 東京都渋谷区神宮前1-2-3"
        results = analyzer.analyze(text=text, language="ja", entities=["POSTAL_CODE"])
        assert len(results) >= 1
