"""エンティティ定義: NERモデル担当 vs Presidio PatternRecognizer担当の分離."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EntityType:
    label: str
    description_ja: str
    examples: tuple[str, ...]


# NERモデルが担当する文脈依存エンティティ
NER_ENTITIES: tuple[EntityType, ...] = (
    EntityType(
        label="PERSON",
        description_ja="人名",
        examples=("山田太郎", "ヤマダ タロウ", "田中花子", "Yamada Taro"),
    ),
    EntityType(
        label="ADDRESS",
        description_ja="住所",
        examples=(
            "東京都渋谷区神宮前1-2-3",
            "大阪府大阪市北区梅田1丁目1-1",
            "〒150-0001 東京都渋谷区神宮前1-2-3 ABCビル5階",
        ),
    ),
    EntityType(
        label="ORGANIZATION",
        description_ja="組織名",
        examples=("株式会社プレノ", "プレノAI合同会社", "東京大学", "厚生労働省"),
    ),
    EntityType(
        label="DATE_OF_BIRTH",
        description_ja="生年月日",
        examples=("1990年1月15日", "平成2年1月15日", "昭和40年3月1日生まれ"),
    ),
    EntityType(
        label="BANK_ACCOUNT",
        description_ja="銀行口座情報",
        examples=(
            "三菱UFJ銀行 渋谷支店 普通 1234567",
            "みずほ銀行 本店 当座 9876543",
        ),
    ),
)

# Presidio PatternRecognizerが担当するパターンベースエンティティ
PATTERN_ENTITIES: tuple[EntityType, ...] = (
    EntityType(
        label="EMAIL_ADDRESS",
        description_ja="メールアドレス",
        examples=("user@example.co.jp", "taro.yamada@company.com"),
    ),
    EntityType(
        label="PHONE_NUMBER",
        description_ja="電話番号",
        examples=("03-1234-5678", "090-1234-5678", "０３−１２３４−５６７８"),
    ),
    EntityType(
        label="MY_NUMBER",
        description_ja="マイナンバー（個人番号）",
        examples=("1234 5678 9012", "123456789012"),
    ),
    EntityType(
        label="CREDIT_CARD",
        description_ja="クレジットカード番号",
        examples=("4111-1111-1111-1111", "4111111111111111"),
    ),
    EntityType(
        label="PASSPORT",
        description_ja="パスポート番号",
        examples=("TK1234567", "MZ9876543"),
    ),
    EntityType(
        label="DRIVER_LICENSE",
        description_ja="運転免許証番号",
        examples=("012345678901", "306789012345"),
    ),
    EntityType(
        label="IP_ADDRESS",
        description_ja="IPアドレス",
        examples=("192.168.1.1", "10.0.0.1"),
    ),
)

ALL_ENTITIES = NER_ENTITIES + PATTERN_ENTITIES
NER_LABELS: list[str] = [e.label for e in NER_ENTITIES]
PATTERN_LABELS: list[str] = [e.label for e in PATTERN_ENTITIES]
