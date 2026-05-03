"""日本語PII パターンベース Recognizer 定義.

presidio に依存しない純粋データ。サーバ側は `pleno_recognizers.presidio_adapter`
経由で `PatternRecognizer` に変換し、scanner 側は raw regex として直接使う。
"""

from pleno_recognizers.types import PiiPattern, PiiRecognizer

# --- 電話番号 (全角/半角対応) ---
_SEP = r"[\-‐ー－−–―]"
_SEP_OPT = _SEP + r"?"

JA_PHONE = PiiRecognizer(
    entity="PHONE_NUMBER",
    language="ja",
    patterns=(
        PiiPattern(
            "ja_phone_mobile",
            r"(?<!\d)0[789]0" + _SEP_OPT + r"\d{4}" + _SEP_OPT + r"\d{4}(?!\d)",
            0.7,
        ),
        PiiPattern(
            "ja_phone_freephone",
            r"(?<!\d)0120" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}(?!\d)",
            0.7,
        ),
        PiiPattern(
            "ja_phone_fixed",
            r"(?<!\d)0\d{1,4}" + _SEP + r"\d{1,4}" + _SEP + r"\d{4}(?!\d)",
            0.5,
        ),
        PiiPattern(
            "ja_phone_fullwidth",
            r"[０][０-９]{1,3}" + _SEP + r"[０-９]{1,4}" + _SEP + r"[０-９]{4}",
            0.7,
        ),
    ),
    context=("電話", "携帯", "TEL", "tel", "連絡先", "phone"),
)

# --- マイナンバー (12桁) ---
JA_MY_NUMBER = PiiRecognizer(
    entity="MY_NUMBER",
    language="ja",
    patterns=(
        PiiPattern("my_number_spaced", r"\b\d{4}[\s\-]\d{4}[\s\-]\d{4}\b", 0.5),
        PiiPattern("my_number_continuous", r"\b\d{12}\b", 0.3),
    ),
    context=("マイナンバー", "個人番号", "my number", "通知カード"),
)

# --- クレジットカード番号 ---
JA_CREDIT_CARD = PiiRecognizer(
    entity="CREDIT_CARD",
    language="ja",
    patterns=(
        PiiPattern(
            "credit_card_dashed",
            r"\b\d{4}[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}\b",
            0.6,
        ),
        PiiPattern(
            "credit_card_continuous",
            r"\b(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))\d{8,12}\b",
            0.5,
        ),
    ),
    context=("クレジットカード", "カード番号", "credit card", "VISA", "Mastercard"),
)

# --- パスポート番号 (日本) ---
JA_PASSPORT = PiiRecognizer(
    entity="PASSPORT",
    language="ja",
    patterns=(PiiPattern("ja_passport", r"\b[A-Z]{2}\d{7}\b", 0.4),),
    context=("パスポート", "旅券", "passport", "旅券番号"),
)

# --- 運転免許証番号 (12桁数字) ---
JA_DRIVER_LICENSE = PiiRecognizer(
    entity="DRIVER_LICENSE",
    language="ja",
    patterns=(PiiPattern("ja_driver_license", r"\b\d{12}\b", 0.2),),
    context=("運転免許", "免許証", "免許番号", "driver license"),
)

# --- IPアドレス ---
JA_IP_ADDRESS = PiiRecognizer(
    entity="IP_ADDRESS",
    language="ja",
    patterns=(
        PiiPattern(
            "ipv4",
            r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
            0.6,
        ),
    ),
    context=("IP", "IPアドレス", "ip address", "サーバー"),
)

# --- メールアドレス ---
JA_EMAIL = PiiRecognizer(
    entity="EMAIL_ADDRESS",
    language="ja",
    patterns=(
        PiiPattern(
            "email",
            r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b",
            0.9,
        ),
    ),
    context=("メール", "email", "Eメール", "メールアドレス"),
)

# --- 法人番号 (13桁) ---
JA_CORPORATE_NUMBER = PiiRecognizer(
    entity="MY_NUMBER_CORPORATE",
    language="ja",
    patterns=(
        PiiPattern(
            "corporate_number_spaced", r"\b\d[\s\-]\d{4}[\s\-]\d{4}[\s\-]\d{4}\b", 0.5
        ),
        PiiPattern("corporate_number_continuous", r"\b\d{13}\b", 0.3),
    ),
    context=("法人番号", "法人マイナンバー", "corporate number"),
)

# --- 健康保険証番号 ---
JA_HEALTH_INSURANCE = PiiRecognizer(
    entity="HEALTH_INSURANCE",
    language="ja",
    patterns=(
        PiiPattern("insurer_number", r"\b\d{8}\b", 0.1),
        PiiPattern(
            "insurance_symbol_number",
            r"記号[\s　]*\d{1,6}[\s　]*番号[\s　]*\d{1,7}",
            0.8,
        ),
    ),
    context=(
        "保険証",
        "健康保険",
        "被保険者",
        "保険者番号",
        "被保険者番号",
        "国民健康保険",
        "社会保険",
    ),
)

# --- 在留カード番号 ---
JA_RESIDENCE_CARD = PiiRecognizer(
    entity="RESIDENCE_CARD",
    language="ja",
    patterns=(PiiPattern("residence_card", r"\b[A-Z]{2}\d{8}[A-Z]{2}\b", 0.6),),
    context=("在留カード", "在留番号", "residence card", "在留資格"),
)

# --- 郵便番号 ---
JA_POSTAL_CODE = PiiRecognizer(
    entity="POSTAL_CODE",
    language="ja",
    patterns=(
        PiiPattern("postal_code_with_symbol", r"〒\d{3}[‐\-ー]\d{4}", 0.9),
        PiiPattern("postal_code_half", r"\b\d{3}[‐\-ー]\d{4}\b", 0.3),
        PiiPattern(
            "postal_code_fullwidth", r"〒[０-９]{3}[‐\-ー－−][０-９]{4}", 0.9
        ),
    ),
    context=("郵便番号", "〒", "zip", "postal"),
)

# --- URL ---
JA_URL = PiiRecognizer(
    entity="URL",
    language="ja",
    patterns=(PiiPattern("url_with_scheme", r"https?://[^\s<>\"']+", 0.8),),
    context=("URL", "リンク", "サイト", "ホームページ"),
)

# --- 銀行口座 (BANK_ACCOUNT) ---
_JA_BANK_NAMES = (
    "三菱UFJ", "三井住友", "みずほ", "りそな", "埼玉りそな",
    "三井住友信託", "三菱UFJ信託",
    "楽天", "PayPay", "ソニー", "住信SBIネット", "auじぶん",
    "セブン", "イオン", "GMOあおぞらネット", "あおぞら", "ローソン",
    "横浜", "千葉", "静岡", "常陽", "京都", "広島",
    "西日本シティ", "福岡", "北海道", "北陸", "群馬", "東邦",
    "山陰合同", "新生", "シティバンク", "信金中央",
)
_JA_BANK_ALT = "|".join(sorted(_JA_BANK_NAMES, key=len, reverse=True))
_JA_BANK_BRANCH_PART = (
    r"(?:[一-龥ぁ-んァ-ヶー々〆\d]{0,12}支店|本店営業部|本店|[一-龥ぁ-んァ-ヶー]{1,8}営業部)"
)

JA_BANK_ACCOUNT = PiiRecognizer(
    entity="BANK_ACCOUNT",
    language="ja",
    patterns=(
        PiiPattern(
            "bank_account_branch",
            r"(?:" + _JA_BANK_ALT + r")銀行" + _JA_BANK_BRANCH_PART + r"(?:普通|当座)\d{7,8}",
            0.85,
        ),
        PiiPattern(
            "bank_account_yucho",
            r"ゆうちょ銀行記号\d{5}番号\d{7,8}",
            0.9,
        ),
    ),
    context=("銀行", "口座", "振込", "振込先", "支店", "普通", "当座", "ゆうちょ"),
)

ALL_JA_RECOGNIZERS: tuple[PiiRecognizer, ...] = (
    JA_PHONE,
    JA_MY_NUMBER,
    JA_CREDIT_CARD,
    JA_PASSPORT,
    JA_DRIVER_LICENSE,
    JA_IP_ADDRESS,
    JA_EMAIL,
    JA_CORPORATE_NUMBER,
    JA_HEALTH_INSURANCE,
    JA_RESIDENCE_CARD,
    JA_POSTAL_CODE,
    JA_URL,
    JA_BANK_ACCOUNT,
)
