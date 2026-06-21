"""エンティティ定義: NERモデル担当 vs Presidio PatternRecognizer担当の分離."""

from dataclasses import dataclass


@dataclass(frozen=True)
class EntityType:
    label: str
    description_ja: str
    examples: tuple[str, ...]
    examples_en: tuple[str, ...] = ()


@dataclass(frozen=True)
class LangConfig:
    system_prompt: str
    prompts_subdir: str


LANG_CONFIGS: dict[str, LangConfig] = {
    "ja": LangConfig(
        system_prompt=(
            "あなたは日本語のPII（個人情報）を含むリアルなテキストを生成する専門家です。"
            "指定されたXMLタグ形式で正確にPIIエンティティをマークアップしてください。"
            "タグは必ず正しく閉じ、ネストしないでください。"
        ),
        prompts_subdir="ja",
    ),
    "en": LangConfig(
        system_prompt=(
            "You are an expert at generating realistic English text containing PII (personally identifiable information). "
            "Mark PII entities precisely using the specified XML tag format. "
            "Tags must be properly closed and must not be nested."
        ),
        prompts_subdir="en",
    ),
}


# NERモデルが担当する文脈依存エンティティ
NER_ENTITIES: tuple[EntityType, ...] = (
    EntityType(
        label="PERSON",
        description_ja="人名",
        examples=("山田太郎", "ヤマダ タロウ", "田中花子", "Yamada Taro"),
        examples_en=("John Smith", "Emily R. Johnson", "Jean-Pierre Dupont", "Michael O'Brien Jr."),
    ),
    EntityType(
        label="ADDRESS",
        description_ja="住所",
        examples=(
            "東京都渋谷区神宮前1-2-3",
            "大阪府大阪市北区梅田1丁目1-1",
            "〒150-0001 東京都渋谷区神宮前1-2-3 ABCビル5階",
        ),
        examples_en=(
            "123 Main Street, New York, NY 10001",
            "456 Oak Avenue, Suite 200, San Francisco, CA 94102",
            "10 Downing Street, London SW1A 2AA, United Kingdom",
        ),
    ),
    EntityType(
        label="ORGANIZATION",
        description_ja="組織名",
        examples=("株式会社プレノ", "プレノAI合同会社", "東京大学", "厚生労働省"),
        examples_en=("Acme Corporation", "MIT", "FDA", "Red Cross"),
    ),
    EntityType(
        label="DATE_OF_BIRTH",
        description_ja="生年月日",
        examples=("1990年1月15日", "平成2年1月15日", "昭和40年3月1日生まれ"),
        examples_en=(
            "January 15, 1990",
            "01/15/1990",
            "1990-01-15",
            "15 Jan 1990",
            "DOB: 03/15/1985",
        ),
    ),
    EntityType(
        label="BANK_ACCOUNT",
        description_ja="銀行口座情報",
        examples=(
            "三菱UFJ銀行 渋谷支店 普通 1234567",
            "みずほ銀行 本店 当座 9876543",
        ),
        examples_en=(
            "Chase Bank, Routing: 021000021, Account: 123456789, Checking",
            "Bank of America, ABA: 026009593, Acct: 987654321, Savings",
        ),
    ),
)

# APPI Art. 2(3) 要配慮個人情報 — context-dependent, NER model only
SPECIAL_CARE_ENTITIES: tuple[EntityType, ...] = (
    EntityType(
        label="RACE",
        description_ja="人種・民族",
        examples=("在日韓国人", "アイヌ民族", "中国系", "ブラジル系日系人"),
    ),
    EntityType(
        label="CREED",
        description_ja="信条・信仰",
        examples=("キリスト教徒", "イスラム教を信仰", "創価学会員", "共産主義者"),
    ),
    EntityType(
        label="SOCIAL_STATUS",
        description_ja="社会的身分",
        examples=("被差別部落出身", "非嫡出子", "婚外子"),
    ),
    EntityType(
        label="MEDICAL_HISTORY",
        description_ja="病歴・医療歴・治療歴",
        examples=(
            "うつ病と診断", "糖尿病の治療中", "胃がんの手術歴あり",
            "統合失調症で通院", "B型肝炎キャリア",
        ),
    ),
    EntityType(
        label="HEALTH_CHECKUP",
        description_ja="健康診断・検査結果",
        examples=(
            "HbA1c 7.2%", "血圧 150/95mmHg", "要精密検査",
            "心電図異常所見", "肝機能 GOT 45",
        ),
    ),
    EntityType(
        label="DISABILITY",
        description_ja="心身の機能の障害",
        examples=(
            "身体障害者手帳1級", "知的障害B判定", "精神障害者保健福祉手帳2級",
            "右下肢機能全廃", "視覚障害",
        ),
    ),
    EntityType(
        label="CRIMINAL_RECORD",
        description_ja="犯罪歴",
        examples=(
            "窃盗罪で起訴", "傷害罪の前科あり", "詐欺罪で懲役2年の判決",
            "少年院に送致", "執行猶予中",
        ),
    ),
    EntityType(
        label="CRIME_VICTIM",
        description_ja="犯罪被害の事実",
        examples=(
            "暴行の被害に遭った", "性犯罪の被害者", "DV被害を受けた",
            "ストーカー被害", "詐欺被害に遭った",
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
    EntityType(
        label="MY_NUMBER_CORPORATE",
        description_ja="法人番号",
        examples=("1234567890123", "1 2345 6789 0123"),
    ),
    EntityType(
        label="HEALTH_INSURANCE",
        description_ja="健康保険証番号（被保険者番号）",
        examples=("記号 12345 番号 678901", "保険者番号 01130012"),
    ),
    EntityType(
        label="RESIDENCE_CARD",
        description_ja="在留カード番号",
        examples=("AB12345678CD", "CD98765432EF"),
    ),
    EntityType(
        label="POSTAL_CODE",
        description_ja="郵便番号",
        examples=("〒150-0001", "150-0001", "１５０−０００１"),
    ),
    EntityType(
        label="URL",
        description_ja="URL",
        examples=("https://example.com", "http://example.co.jp/path?q=1"),
    ),
)

ALL_NER_ENTITIES = NER_ENTITIES + SPECIAL_CARE_ENTITIES
ALL_ENTITIES = ALL_NER_ENTITIES + PATTERN_ENTITIES
NER_LABELS: list[str] = [e.label for e in ALL_NER_ENTITIES]
SPECIAL_CARE_LABELS: list[str] = [e.label for e in SPECIAL_CARE_ENTITIES]
PATTERN_LABELS: list[str] = [e.label for e in PATTERN_ENTITIES]
