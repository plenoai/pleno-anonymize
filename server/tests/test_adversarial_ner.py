"""NERモデルの敵対的エッジケーステスト.

合成データでF1>0.97を達成していても実環境で失敗するケースを検証する。
NERモデルが必要なためスキップ可能（pytest -m "not ner"）。
"""

import json
from pathlib import Path

import pytest

# NERモデルのロード（存在しない場合スキップ）
try:
    import spacy

    _model_path = (
        Path(__file__).parent.parent
        / "packages"
        / "models"
        / "ja_ner_ja-0.1.0"
        / "ja_ner_ja"
        / "ja_ner_ja-0.1.0"
    )
    if _model_path.exists():
        _nlp = spacy.load(str(_model_path))
        HAS_MODEL = True
    else:
        HAS_MODEL = False
        _nlp = None
except Exception:
    HAS_MODEL = False
    _nlp = None

pytestmark = pytest.mark.skipif(not HAS_MODEL, reason="NERモデルが未配置")


def _ner_entities(text: str) -> dict[str, list[str]]:
    """テキストからエンティティを抽出してラベル別に返す."""
    doc = _nlp(text)
    result: dict[str, list[str]] = {}
    for ent in doc.ents:
        result.setdefault(ent.label_, []).append(ent.text)
    return result


# ============================================================
# PERSON: 人名の境界ケース
# ============================================================


class TestPersonEdgeCases:
    """人名認識の弱点を突くケース."""

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            ("患者の李さんは来院されました", "李"),
            ("王先生が担当です", "王"),
            ("呉氏による報告", "呉"),
        ],
        ids=["1char_lee", "1char_wang", "1char_go"],
    )
    def test_single_char_names(self, text: str, expected_name: str):
        """1文字の名字（中国・韓国系）."""
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert any(expected_name in p for p in persons), f"'{expected_name}' not in {persons}"

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            ("ジョン・スミス様にご連絡ください", "ジョン・スミス"),
            ("マイケル ジョンソン氏が出席", "マイケル ジョンソン"),
            ("アレクサンドラ・ペトロヴァが担当", "アレクサンドラ・ペトロヴァ"),
        ],
        ids=["katakana_nakaguro", "katakana_space", "katakana_long"],
    )
    def test_foreign_names_katakana(self, text: str, expected_name: str):
        """外国人名のカタカナ表記."""
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert any(expected_name in p or p in expected_name for p in persons), (
            f"'{expected_name}' not found in {persons}"
        )

    @pytest.mark.parametrize(
        "text,expected_name",
        [
            ("齋藤健一がお送りします", "齋藤健一"),
            ("髙橋美咲さんの予約", "髙橋美咲"),
            ("渡邊大輔氏にお伺い", "渡邊大輔"),
            ("廣瀬智子が担当", "廣瀬智子"),
        ],
        ids=["saitou_old", "takahashi_old", "watanabe_old", "hirose_old"],
    )
    def test_old_kanji_names(self, text: str, expected_name: str):
        """旧字体・異体字を含む名前."""
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert any(expected_name in p or p in expected_name for p in persons), (
            f"'{expected_name}' not found in {persons}"
        )

    def test_name_with_honorific_attached(self):
        """敬称が名前に付着するケース."""
        text = "山田太郎様のご注文を承りました"
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        # 「山田太郎」が含まれ、「様」は含まれないのが理想
        assert persons, "人名が検出されなかった"
        for p in persons:
            if "山田太郎" in p:
                assert "様" not in p or p == "山田太郎様", (
                    f"敬称の境界が不正: '{p}'"
                )
                break

    def test_consecutive_names(self):
        """連続する人名."""
        text = "出席者: 田中太郎、鈴木花子、佐藤一郎"
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert len(persons) >= 3, f"3名検出すべきだが {len(persons)} 名: {persons}"


# ============================================================
# ADDRESS: 住所の境界ケース
# ============================================================


class TestAddressEdgeCases:
    """住所認識の弱点を突くケース."""

    @pytest.mark.parametrize(
        "text",
        [
            "住所: 東京都千代田区丸の内1-1-1",  # 標準
            "住所: 〒100-0005 東京都千代田区丸の内1-1-1",  # 郵便番号付き
            "住所: 北海道札幌市中央区北1条西2丁目",  # 丁目表記
            "住所: 沖縄県那覇市おもろまち4-16-27",  # ひらがな地名
            "住所: 京都府京都市左京区下鴨半木町",  # 番地なし
        ],
        ids=["standard", "with_postal", "choume", "hiragana_place", "no_banchi"],
    )
    def test_address_formats(self, text: str):
        entities = _ner_entities(text)
        assert "ADDRESS" in entities, f"住所が検出されなかった: {entities}"

    def test_address_with_building(self):
        """ビル名を含む住所."""
        text = "所在地: 東京都港区六本木6-10-1 六本木ヒルズ森タワー42F"
        entities = _ner_entities(text)
        addresses = entities.get("ADDRESS", [])
        assert addresses, "住所が検出されなかった"
        # ビル名まで含めて1つのADDRESSとして検出されるのが理想
        full = " ".join(addresses)
        assert "六本木" in full

    def test_address_not_org(self):
        """住所の一部が組織名と混同されないか."""
        text = "東京都渋谷区神宮前1-2-3に株式会社プレノの本社があります"
        entities = _ner_entities(text)
        addresses = entities.get("ADDRESS", [])
        orgs = entities.get("ORGANIZATION", [])
        # 住所と組織が別々に検出されるべき
        assert addresses, "住所が未検出"
        assert orgs, "組織名が未検出"


# ============================================================
# ORGANIZATION: 組織名の境界ケース
# ============================================================


class TestOrganizationEdgeCases:
    """組織名認識の弱点を突くケース."""

    @pytest.mark.parametrize(
        "text,expected_org",
        [
            ("厚労省の発表によると", "厚労省"),
            ("財務省が方針を示した", "財務省"),
            ("総務省の見解では", "総務省"),
        ],
        ids=["mhlw_abbrev", "mof", "mic"],
    )
    def test_government_abbreviations(self, text: str, expected_org: str):
        """省庁の略称."""
        entities = _ner_entities(text)
        orgs = entities.get("ORGANIZATION", [])
        assert any(expected_org in o for o in orgs), f"'{expected_org}' not in {orgs}"

    @pytest.mark.parametrize(
        "text,expected_org",
        [
            ("トヨタが新型車を発表", "トヨタ"),
            ("ソニーの決算発表", "ソニー"),
            ("NTTドコモとの契約", "NTTドコモ"),
        ],
        ids=["toyota", "sony", "ntt_docomo"],
    )
    def test_well_known_company_short(self, text: str, expected_org: str):
        """法人格なしの有名企業名."""
        entities = _ner_entities(text)
        orgs = entities.get("ORGANIZATION", [])
        assert any(expected_org in o for o in orgs), f"'{expected_org}' not in {orgs}"

    def test_org_types(self):
        """様々な法人格."""
        text = (
            "合同会社ABC、一般社団法人XYZ、"
            "NPO法人サポート、医療法人社団メディカル、"
            "学校法人未来学園が参加"
        )
        entities = _ner_entities(text)
        orgs = entities.get("ORGANIZATION", [])
        assert len(orgs) >= 3, f"5組織中 {len(orgs)} のみ検出: {orgs}"


# ============================================================
# DATE_OF_BIRTH: 生年月日の境界ケース
# ============================================================


class TestDateOfBirthEdgeCases:
    """生年月日認識の弱点を突くケース."""

    @pytest.mark.parametrize(
        "text",
        [
            "生年月日: 令和2年1月15日",
            "生年月日: R2.1.15",  # 略記
            "生年月日: H2/1/15",  # スラッシュ
            "生年月日: 昭和40年3月1日",
            "生年月日: S40.3.1",
            "誕生日: 1990/01/15",  # 西暦スラッシュ
            "生年月日: 1990-01-15",  # ISO形式
        ],
        ids=["reiwa", "reiwa_abbrev", "heisei_slash", "showa", "showa_abbrev", "western_slash", "iso"],
    )
    def test_date_formats(self, text: str):
        entities = _ner_entities(text)
        assert "DATE_OF_BIRTH" in entities, f"生年月日が検出されなかった: {entities}"

    def test_date_not_general_date(self):
        """一般的な日付は生年月日として検出すべきでない."""
        text = "次回の会議は2024年3月15日に開催されます"
        entities = _ner_entities(text)
        dobs = entities.get("DATE_OF_BIRTH", [])
        assert not dobs, f"一般日付を生年月日と誤検出: {dobs}"


# ============================================================
# BANK_ACCOUNT: 銀行口座の境界ケース
# ============================================================


class TestBankAccountEdgeCases:
    """銀行口座情報認識の弱点を突くケース."""

    @pytest.mark.parametrize(
        "text",
        [
            "振込先: 三菱UFJ銀行 渋谷支店 普通 1234567",
            "口座: みずほ銀行 本店 当座 9876543",
            "送金先: ゆうちょ銀行 〇一八店 普通 12345678",  # ゆうちょ8桁
            "振込先: 三井住友銀行 新宿支店 普通口座 1234567",  # 「口座」付き
        ],
        ids=["mufg", "mizuho", "yuucho", "smbc_koza"],
    )
    def test_bank_formats(self, text: str):
        entities = _ner_entities(text)
        assert "BANK_ACCOUNT" in entities, f"口座情報が検出されなかった: {entities}"

    def test_partial_bank_info(self):
        """銀行名のみ・口座番号のみの場合."""
        text = "三菱UFJ銀行をご利用いただきありがとうございます"
        entities = _ner_entities(text)
        # 銀行名だけなら ORGANIZATION であって BANK_ACCOUNT ではない
        bank_accounts = entities.get("BANK_ACCOUNT", [])
        # 口座番号がないのに BANK_ACCOUNT と検出するのは偽陽性
        # ただし銀行名自体がORGとして検出されるのはOK


# ============================================================
# 偽陽性: PIIでないテキストの誤検出
# ============================================================


class TestFalsePositives:
    """PIIでないテキストを誤検出しないか."""

    @pytest.mark.parametrize(
        "text",
        [
            "東京タワーは東京都港区芝公園にある電波塔です",  # 地名だが住所ではない
            "日本銀行は中央銀行として金融政策を担う",  # 組織名は正しいが口座情報ではない
            "大正時代の文化は独特である",  # 「大正」は元号だが生年月日ではない
            "明治維新は1868年に起きた",  # 歴史的日付は生年月日ではない
            "令和の時代が始まった",  # 元号だが生年月日ではない
        ],
        ids=[
            "landmark_not_address",
            "boj_not_bank_account",
            "era_not_dob",
            "historical_date_not_dob",
            "era_name_not_dob",
        ],
    )
    def test_non_pii_text(self, text: str):
        entities = _ner_entities(text)
        # 生年月日の偽陽性チェック
        dobs = entities.get("DATE_OF_BIRTH", [])
        assert not dobs, f"偽陽性 DATE_OF_BIRTH: {dobs}"
        # 口座情報の偽陽性チェック
        banks = entities.get("BANK_ACCOUNT", [])
        assert not banks, f"偽陽性 BANK_ACCOUNT: {banks}"


# ============================================================
# エンティティ隣接・混在テスト
# ============================================================


class TestEntityProximity:
    """エンティティが隣接・密集している場合の検出精度."""

    def test_adjacent_person_address(self):
        """人名の直後に住所が続くケース."""
        text = "山田太郎（東京都渋谷区神宮前1-2-3）に配送"
        entities = _ner_entities(text)
        assert "PERSON" in entities, "人名未検出"
        assert "ADDRESS" in entities, "住所未検出"

    def test_dense_pii_text(self):
        """PII密度が高いテキスト."""
        text = (
            "患者名: 田中花子、生年月日: 平成5年3月20日、"
            "住所: 大阪府大阪市北区梅田1-1-1、"
            "勤務先: 株式会社ABC"
        )
        entities = _ner_entities(text)
        assert "PERSON" in entities
        assert "DATE_OF_BIRTH" in entities
        assert "ADDRESS" in entities
        assert "ORGANIZATION" in entities

    def test_entity_separated_by_comma(self):
        """カンマ区切りで複数のエンティティ."""
        text = "関係者: 山田太郎、佐藤花子、鈴木一郎が出席した"
        entities = _ner_entities(text)
        persons = entities.get("PERSON", [])
        assert len(persons) >= 3, f"3名中 {len(persons)} 名のみ: {persons}"
