"""日本語PII パターンベース Recognizer.

Presidio PatternRecognizer を使い、正規表現で検出可能な
日本語固有のPIIパターンを定義する。
"""

from presidio_analyzer import Pattern, PatternRecognizer

# --- 電話番号 (全角/半角対応) ---
# 携帯: 0X0-XXXX-XXXX
# フリーダイヤル: 0120-XXX-XXX
# 固定電話: 0X-XXXX-XXXX, 0XX-XXX-XXXX
# 全角: ０Ｘ０−ＸＸＸＸ−ＸＸＸＸ
# 対応セパレータ: ハイフン(-), 全角ハイフン(‐), 長音(ー), 全角ダッシュ(－),
#                マイナス(−), en-dash(–), em-dash(―)
_SEP = r"[\-‐ー－−–―]"  # 全セパレータ文字クラス
_SEP_OPT = _SEP + r"?"
JA_PHONE_PATTERNS = [
    Pattern(
        name="ja_phone_mobile",
        regex=r"(?<!\d)0[789]0" + _SEP_OPT + r"\d{4}" + _SEP_OPT + r"\d{4}(?!\d)",
        score=0.7,
    ),
    Pattern(
        name="ja_phone_freephone",
        regex=r"(?<!\d)0120" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}(?!\d)",
        score=0.7,
    ),
    Pattern(
        name="ja_phone_fixed",
        regex=r"(?<!\d)0\d{1,4}" + _SEP + r"\d{1,4}" + _SEP + r"\d{4}(?!\d)",
        score=0.5,
    ),
    Pattern(
        name="ja_phone_fullwidth",
        regex=r"[０][０-９]{1,3}" + _SEP + r"[０-９]{1,4}" + _SEP + r"[０-９]{4}",
        score=0.7,
    ),
]

JapanesePhoneRecognizer = PatternRecognizer(
    supported_entity="PHONE_NUMBER",
    supported_language="ja",
    patterns=JA_PHONE_PATTERNS,
    context=["電話", "携帯", "TEL", "tel", "連絡先", "phone"],
)

# --- マイナンバー (12桁) ---
JA_MY_NUMBER_PATTERNS = [
    Pattern(
        name="my_number_spaced",
        regex=r"\b\d{4}[\s\-]\d{4}[\s\-]\d{4}\b",
        score=0.5,
    ),
    Pattern(
        name="my_number_continuous",
        regex=r"\b\d{12}\b",
        score=0.3,
    ),
]

MyNumberRecognizer = PatternRecognizer(
    supported_entity="MY_NUMBER",
    supported_language="ja",
    patterns=JA_MY_NUMBER_PATTERNS,
    context=["マイナンバー", "個人番号", "my number", "通知カード"],
)

# --- クレジットカード番号 ---
JA_CREDIT_CARD_PATTERNS = [
    Pattern(
        name="credit_card_dashed",
        regex=r"\b\d{4}[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}\b",
        score=0.6,
    ),
    Pattern(
        name="credit_card_continuous",
        regex=r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))\d{8,12}\b",
        score=0.5,
    ),
]

JapaneseCreditCardRecognizer = PatternRecognizer(
    supported_entity="CREDIT_CARD",
    supported_language="ja",
    patterns=JA_CREDIT_CARD_PATTERNS,
    context=["クレジットカード", "カード番号", "credit card", "VISA", "Mastercard"],
)

# --- パスポート番号 (日本) ---
# 形式: 2文字のアルファベット + 7桁の数字
JA_PASSPORT_PATTERNS = [
    Pattern(
        name="ja_passport",
        regex=r"\b[A-Z]{2}\d{7}\b",
        score=0.4,
    ),
]

JapanesePassportRecognizer = PatternRecognizer(
    supported_entity="PASSPORT",
    supported_language="ja",
    patterns=JA_PASSPORT_PATTERNS,
    context=["パスポート", "旅券", "passport", "旅券番号"],
)

# --- 運転免許証番号 (12桁数字) ---
JA_DRIVER_LICENSE_PATTERNS = [
    Pattern(
        name="ja_driver_license",
        regex=r"\b\d{12}\b",
        score=0.2,
    ),
]

JapaneseDriverLicenseRecognizer = PatternRecognizer(
    supported_entity="DRIVER_LICENSE",
    supported_language="ja",
    patterns=JA_DRIVER_LICENSE_PATTERNS,
    context=["運転免許", "免許証", "免許番号", "driver license"],
)

# --- IPアドレス ---
JA_IP_PATTERNS = [
    Pattern(
        name="ipv4",
        regex=r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
        score=0.6,
    ),
]

IPAddressRecognizer = PatternRecognizer(
    supported_entity="IP_ADDRESS",
    supported_language="ja",
    patterns=JA_IP_PATTERNS,
    context=["IP", "IPアドレス", "ip address", "サーバー"],
)

# --- メールアドレス ---
JA_EMAIL_PATTERNS = [
    Pattern(
        name="email",
        regex=r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
        score=0.9,
    ),
]

JapaneseEmailRecognizer = PatternRecognizer(
    supported_entity="EMAIL_ADDRESS",
    supported_language="ja",
    patterns=JA_EMAIL_PATTERNS,
    context=["メール", "email", "Eメール", "メールアドレス"],
)

# --- 法人番号 (13桁) ---
JA_CORPORATE_NUMBER_PATTERNS = [
    Pattern(
        name="corporate_number_spaced",
        regex=r"\b\d[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}\b",
        score=0.5,
    ),
    Pattern(
        name="corporate_number_continuous",
        regex=r"\b\d{13}\b",
        score=0.3,
    ),
]

CorporateNumberRecognizer = PatternRecognizer(
    supported_entity="MY_NUMBER_CORPORATE",
    supported_language="ja",
    patterns=JA_CORPORATE_NUMBER_PATTERNS,
    context=["法人番号", "法人マイナンバー", "corporate number"],
)

# --- 健康保険証番号 ---
# 被保険者番号: 保険者番号(8桁) + 被保険者記号・番号
JA_HEALTH_INSURANCE_PATTERNS = [
    Pattern(
        name="insurer_number",
        regex=r"\b\d{8}\b",
        score=0.1,
    ),
    Pattern(
        name="insurance_symbol_number",
        regex=r"記号[\s　]*\d{1,6}[\s　]*番号[\s　]*\d{1,7}",
        score=0.8,
    ),
]

HealthInsuranceRecognizer = PatternRecognizer(
    supported_entity="HEALTH_INSURANCE",
    supported_language="ja",
    patterns=JA_HEALTH_INSURANCE_PATTERNS,
    context=[
        "保険証",
        "健康保険",
        "被保険者",
        "保険者番号",
        "被保険者番号",
        "国民健康保険",
        "社会保険",
    ],
)

# --- 在留カード番号 ---
# 形式: 2文字 + 8桁数字 + 2文字 (例: AB12345678CD)
JA_RESIDENCE_CARD_PATTERNS = [
    Pattern(
        name="residence_card",
        regex=r"\b[A-Z]{2}\d{8}[A-Z]{2}\b",
        score=0.6,
    ),
]

ResidenceCardRecognizer = PatternRecognizer(
    supported_entity="RESIDENCE_CARD",
    supported_language="ja",
    patterns=JA_RESIDENCE_CARD_PATTERNS,
    context=["在留カード", "在留番号", "residence card", "在留資格"],
)

# --- 郵便番号 ---
JA_POSTAL_CODE_PATTERNS = [
    Pattern(
        name="postal_code_with_symbol",
        regex=r"〒\d{3}[‐\-ー]\d{4}",
        score=0.9,
    ),
    Pattern(
        name="postal_code_half",
        regex=r"\b\d{3}[‐\-ー]\d{4}\b",
        score=0.3,
    ),
    Pattern(
        name="postal_code_fullwidth",
        regex=r"〒[０-９]{3}[‐\-ー－−][０-９]{4}",
        score=0.9,
    ),
]

PostalCodeRecognizer = PatternRecognizer(
    supported_entity="POSTAL_CODE",
    supported_language="ja",
    patterns=JA_POSTAL_CODE_PATTERNS,
    context=["郵便番号", "〒", "zip", "postal"],
)

# --- URL ---
JA_URL_PATTERNS = [
    Pattern(
        name="url_with_scheme",
        regex=r"https?://[^\s<>\"']+",
        score=0.8,
    ),
]

URLRecognizer = PatternRecognizer(
    supported_entity="URL",
    supported_language="ja",
    patterns=JA_URL_PATTERNS,
    context=["URL", "リンク", "サイト", "ホームページ"],
)

# 全Recognizerのリスト
ALL_JA_RECOGNIZERS = [
    JapanesePhoneRecognizer,
    MyNumberRecognizer,
    JapaneseCreditCardRecognizer,
    JapanesePassportRecognizer,
    JapaneseDriverLicenseRecognizer,
    IPAddressRecognizer,
    JapaneseEmailRecognizer,
    CorporateNumberRecognizer,
    HealthInsuranceRecognizer,
    ResidenceCardRecognizer,
    PostalCodeRecognizer,
    URLRecognizer,
]
