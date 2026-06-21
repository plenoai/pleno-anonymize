"""日本語PII パターンベース Recognizer 定義.

presidio に依存しない純粋データ。サーバ/SDK は
`pleno_anonymize.recognizers.presidio_adapter` 経由で `PatternRecognizer` に
変換し、scanner 側は raw regex として直接使う。
"""

from pleno_anonymize.recognizers.types import PiiPattern, PiiRecognizer

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
        PiiPattern(
            "my_number_spaced", r"(?<!\d)\d{4}[ 　\-]\d{4}[ 　\-]\d{4}(?!\d)", 0.5
        ),
        PiiPattern("my_number_continuous", r"(?<!\d)\d{12}(?!\d)", 0.3),
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
            r"(?<!\d)\d{4}[ 　\-]\d{4}[ 　\-]\d{4}[ 　\-]\d{4}(?!\d)",
            0.6,
        ),
        PiiPattern(
            "credit_card_continuous",
            r"(?<!\d)(?:4\d{3}|5[1-5]\d{2}|3[47]\d{2}|6(?:011|5\d{2}))\d{8,12}(?!\d)",
            0.5,
        ),
    ),
    context=("クレジットカード", "カード番号", "credit card", "VISA", "Mastercard"),
)

# --- パスポート番号 (日本) ---
JA_PASSPORT = PiiRecognizer(
    entity="PASSPORT",
    language="ja",
    patterns=(
        PiiPattern(
            "ja_passport", r"(?<![A-Za-z])(?-i:[A-Z]{2})\d{7}(?![A-Za-z0-9])", 0.4
        ),
    ),
    context=("パスポート", "旅券", "passport", "旅券番号"),
)

# --- 運転免許証番号 (12桁数字) ---
JA_DRIVER_LICENSE = PiiRecognizer(
    entity="DRIVER_LICENSE",
    language="ja",
    patterns=(PiiPattern("ja_driver_license", r"(?<!\d)\d{12}(?!\d)", 0.2),),
    context=("運転免許", "免許証", "免許番号", "driver license"),
)

# --- IPアドレス ---
JA_IP_ADDRESS = PiiRecognizer(
    entity="IP_ADDRESS",
    language="ja",
    patterns=(
        PiiPattern(
            "ipv4",
            r"(?<!\d)(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?!\d)",
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
            r"(?<![a-zA-Z0-9._%+\-])[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}(?![a-zA-Z])",
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
            "corporate_number_spaced",
            r"(?<!\d)\d[ 　\-]\d{4}[ 　\-]\d{4}[ 　\-]\d{4}(?!\d)",
            0.5,
        ),
        PiiPattern("corporate_number_continuous", r"(?<!\d)\d{13}(?!\d)", 0.3),
    ),
    context=("法人番号", "法人マイナンバー", "corporate number"),
)

# --- 健康保険証番号 ---
JA_HEALTH_INSURANCE = PiiRecognizer(
    entity="HEALTH_INSURANCE",
    language="ja",
    patterns=(
        PiiPattern("insurer_number", r"(?<!\d)\d{8}(?!\d)", 0.1),
        PiiPattern(
            "insurance_symbol_number",
            r"記号[ 　]*\d{1,6}[ 　]*番号[ 　]*\d{1,7}",
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
    patterns=(
        PiiPattern(
            "residence_card",
            r"(?<![A-Za-z])(?-i:[A-Z]{2}\d{8}[A-Z]{2})(?![A-Za-z])",
            0.6,
        ),
    ),
    context=("在留カード", "在留番号", "residence card", "在留資格"),
)

# --- 郵便番号 ---
JA_POSTAL_CODE = PiiRecognizer(
    entity="POSTAL_CODE",
    language="ja",
    patterns=(
        PiiPattern("postal_code_with_symbol", r"〒\d{3}[‐\-ー]\d{4}", 0.9),
        PiiPattern("postal_code_half", r"(?<!\d)\d{3}[‐\-ー]\d{4}(?![‐\-ー\d])", 0.3),
        PiiPattern("postal_code_fullwidth", r"〒[０-９]{3}[‐\-ー－−][０-９]{4}", 0.9),
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

# --- 生年月日 (DATE_OF_BIRTH) ---
JA_DATE_OF_BIRTH = PiiRecognizer(
    entity="DATE_OF_BIRTH",
    language="ja",
    patterns=(
        PiiPattern(
            "ja_dob_jp_era",
            r"(?<!\d)(?:昭和|平成|令和)(?:\d{1,2}|元)年\d{1,2}月\d{1,2}日(?!\d)",
            0.8,
        ),
        PiiPattern(
            "ja_dob_western_year",
            r"(?<!\d)\d{4}年\d{1,2}月\d{1,2}日(?!\d)",
            0.4,
        ),
        PiiPattern(
            "ja_dob_slash",
            r"(?<!\d)\d{4}[/／]\d{1,2}[/／]\d{1,2}(?!\d)",
            0.3,
        ),
    ),
    context=(
        "生年月日",
        "誕生日",
        "生まれ",
        "birthday",
        "birth date",
        "date of birth",
        "生年月",
    ),
)

# --- 銀行口座 (BANK_ACCOUNT) ---
_JA_BANK_NAMES = (
    "三菱UFJ",
    "三井住友",
    "みずほ",
    "りそな",
    "埼玉りそな",
    "三井住友信託",
    "三菱UFJ信託",
    "楽天",
    "PayPay",
    "ソニー",
    "住信SBIネット",
    "auじぶん",
    "セブン",
    "イオン",
    "GMOあおぞらネット",
    "あおぞら",
    "ローソン",
    "横浜",
    "千葉",
    "静岡",
    "常陽",
    "京都",
    "広島",
    "西日本シティ",
    "福岡",
    "北海道",
    "北陸",
    "群馬",
    "東邦",
    "山陰合同",
    "新生",
    "シティバンク",
    "信金中央",
)
_JA_BANK_ALT = "|".join(sorted(_JA_BANK_NAMES, key=len, reverse=True))
_JA_BANK_BRANCH_PART = r"(?:[一-龥ぁ-んァ-ヶー々〆\d]{0,12}支店|本店営業部|本店|[一-龥ぁ-んァ-ヶー]{1,8}営業部)"

# --- Latin-script personal names (recall booster, issue #102) ---
# pleno_anonymize_ja は日本語まじり文中のLatin文字人名 (Yosuke Furukawa, Guido van Rossum,
# Barry Warsaw など) を PERSON として検出できないため、低スコアの正規表現
# recognizer を追加してリコールを補う。precision を犠牲にしないよう、
# noise_filters.py が author-context (email隣接, "(Name) [#PR]", "Author:" 等) を
# 持たない候補を落とし、verify.py が email隣接時にスコアを promote する。
JA_PERSON_LATIN = PiiRecognizer(
    entity="PERSON",
    language="ja",
    patterns=(
        PiiPattern(
            "person_latin_multi_word",
            # Title-case 2..4 word names with optional Dutch/Spanish/etc.
            # particle (``van``, ``von``, ``de``, ...) between the given and
            # family name. ``\b`` anchors avoid matching mid-word capitals.
            #
            # ``(?-i:...)`` disables IGNORECASE for the whole pattern. Presidio
            # passes ``re.IGNORECASE`` by default to PatternRecognizer regexes,
            # which would otherwise let ``[A-Z]`` match lowercase letters and
            # surface every ``def hello`` and ``import os`` in Python source.
            r"(?-i:"
            r"\b[A-Z][a-z]+"
            r"(?:\s+(?:van|von|de|der|den|del|della|du|di|da|le|la|el|al|bin|ibn))?"
            r"(?:\s+[A-Z][a-z]+\.?){1,3}\b"
            r")",
            0.3,
        ),
    ),
    # Keywords are matched via plain substring search by ``verify``, so they
    # MUST be specific enough not to collide with common English/code prose.
    # ``"by"`` was deliberately dropped after it boosted ``Android Studio``
    # via ``RubyMine``; ``"@"`` is dropped because the same boost triggers
    # for any URL (``git@github.com``). The strong attribution signals
    # below ("Author", "Translator", "©" etc.) plus the email-proximity
    # rule in ``verify`` cover the high-recall cases.
    context=(
        # Generic "Contributor"/"Maintainer" were dropped: they collide with
        # their own match strings ("Contributor Covenant") and self-promote
        # a Latin-name candidate via the keyword boost. The remaining list
        # is restricted to attribution prefixes that almost never appear as
        # the *content* of a name span.
        "Author",
        "Authored-by",
        "Authored by",
        "Translator",
        "Translated by",
        "Reviewed-by",
        "Signed-off-by",
        "Co-authored-by",
        "Copyright",
        "©",
        "翻訳",
        "監訳",
        "原著",
        "著者",
        "訳者",
    ),
)

JA_BANK_ACCOUNT = PiiRecognizer(
    entity="BANK_ACCOUNT",
    language="ja",
    patterns=(
        PiiPattern(
            "bank_account_branch",
            r"(?:"
            + _JA_BANK_ALT
            + r")銀行"
            + _JA_BANK_BRANCH_PART
            + r"(?:普通|当座)\d{7,8}",
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
    JA_DATE_OF_BIRTH,
    JA_BANK_ACCOUNT,
    JA_PERSON_LATIN,
)
