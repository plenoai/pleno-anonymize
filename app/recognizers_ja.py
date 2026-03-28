"""日本語PII パターンベース Recognizer.

Presidio PatternRecognizer を使い、正規表現で検出可能な
日本語固有のPIIパターンを定義する。
"""

from presidio_analyzer import Pattern, PatternRecognizer

# --- 電話番号 (全角/半角対応) ---
# 固定電話: 0X-XXXX-XXXX, 0XX-XXX-XXXX
# 携帯: 0X0-XXXX-XXXX
# 全角: ０Ｘ０−ＸＸＸＸ−ＸＸＸＸ
JA_PHONE_PATTERNS = [
    Pattern(
        name="ja_phone_mobile",
        regex=r"0[789]0[‐\-ー]?\d{4}[‐\-ー]?\d{4}",
        score=0.7,
    ),
    Pattern(
        name="ja_phone_fixed",
        regex=r"0\d{1,4}[‐\-ー]?\d{1,4}[‐\-ー]?\d{4}",
        score=0.5,
    ),
    Pattern(
        name="ja_phone_fullwidth",
        regex=r"[０][０-９]{1,3}[‐\-ー－][０-９]{1,4}[‐\-ー－][０-９]{4}",
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

# 全Recognizerのリスト
ALL_JA_RECOGNIZERS = [
    JapanesePhoneRecognizer,
    MyNumberRecognizer,
    JapaneseCreditCardRecognizer,
    JapanesePassportRecognizer,
    JapaneseDriverLicenseRecognizer,
    IPAddressRecognizer,
    JapaneseEmailRecognizer,
]
