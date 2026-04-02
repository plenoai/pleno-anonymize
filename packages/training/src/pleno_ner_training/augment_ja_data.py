"""JA training data augmentation.

ベンチマーク v0.2.0 で明らかになった品質ギャップを補強:
- PERSON: 多様な姓名パターン、カタカナ名、外国人名
- ADDRESS: 省略形・ビル名付き住所
- ORGANIZATION: 略称・大学・省庁名
- DATE_OF_BIRTH: 和暦バリエーション、文脈付き
- BANK_ACCOUNT: 多様な銀行名・支店名
- 負例: PIIではない紛らわしいテキスト
"""

import json
import random
import copy
from pathlib import Path

random.seed(42)

# --- PERSON ---
LAST_NAMES = [
    "佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村",
    "小林", "加藤", "吉田", "山田", "松本", "井上", "木村", "林",
    "清水", "山口", "阿部", "池田", "橋本", "石川", "前田", "藤田",
]
FIRST_NAMES_M = [
    "太郎", "一郎", "健太", "大輔", "翔太", "拓也", "直樹", "雄太",
    "和也", "誠", "隆", "浩", "修", "豊", "学", "悟",
]
FIRST_NAMES_F = [
    "花子", "美咲", "さくら", "あかり", "結衣", "陽菜", "真由美", "恵子",
    "智子", "麻衣", "愛", "遥", "彩", "萌", "凛", "楓",
]
KATAKANA_NAMES = [
    "ヤマダ タロウ", "サトウ ハナコ", "タナカ ケンタ", "スズキ ミサキ",
    "イトウ ダイスケ", "ワタナベ ユイ", "コバヤシ ショウタ",
]
FOREIGN_NAMES = [
    "マイケル・ジョンソン", "エミリー・ウィリアムズ", "リー・ウェイ",
    "キム・ミンジュン", "アレクサンドラ・ペトロフ", "ジャン＝ピエール・デュポン",
]

# --- ADDRESS ---
PREFECTURES = [
    "東京都", "大阪府", "北海道", "愛知県", "福岡県", "神奈川県",
    "埼玉県", "千葉県", "兵庫県", "京都府", "広島県", "宮城県",
]
CITIES = [
    "渋谷区", "新宿区", "港区", "中央区", "千代田区", "品川区",
    "大阪市北区", "大阪市中央区", "名古屋市中区", "福岡市博多区",
    "札幌市中央区", "横浜市中区", "さいたま市大宮区", "神戸市中央区",
]
TOWNS = [
    "神宮前", "西新宿", "赤坂", "日本橋", "丸の内", "六本木",
    "梅田", "心斎橋", "栄", "天神", "大通", "元町",
]
BUILDINGS = [
    "ABCビル", "第一生命ビル", "○○タワー", "グランドマンション",
    "サンシャインハイツ", "プレミアムレジデンス", "ガーデンコート",
]

# --- ORGANIZATION ---
COMPANY_PREFIXES = ["株式会社", "有限会社", "合同会社", "一般社団法人", "NPO法人", "公益財団法人", "医療法人", "学校法人", "社会福祉法人"]
COMPANY_PREFIXES_ABBREV = ["（株）", "（有）", "（合）", "（一社）"]
COMPANY_NAMES = [
    "プレノ", "テックソリューションズ", "サンライズ", "グローバルテック",
    "ネクストイノベーション", "デジタルフロンティア", "スマートシステムズ",
    "フューチャーラボ", "クリエイティブワークス", "アドバンストテクノロジー",
    "日本製鉄", "トヨタ自動車", "ソニーグループ", "パナソニック", "日立製作所",
    "NTTデータ", "富士通", "三菱商事", "伊藤忠商事", "住友不動産",
    "セブン&アイ・ホールディングス", "ファーストリテイリング", "リクルート",
    "サイバーエージェント", "メルカリ", "ディー・エヌ・エー", "楽天グループ",
    "野村證券", "大和証券", "東京海上日動", "損保ジャパン",
]
COMPANY_SUFFIXES = ["ホールディングス", "グループ", "ジャパン", "インターナショナル"]
UNIVERSITIES = [
    "東京大学", "京都大学", "大阪大学", "早稲田大学", "慶應義塾大学",
    "東北大学", "九州大学", "北海道大学", "名古屋大学", "筑波大学",
    "一橋大学", "東京工業大学", "神戸大学", "横浜国立大学", "千葉大学",
    "明治大学", "立教大学", "中央大学", "法政大学", "上智大学",
    "同志社大学", "立命館大学", "関西学院大学", "青山学院大学",
]
GOVERNMENT = [
    "厚生労働省", "国土交通省", "総務省", "経済産業省", "法務省",
    "財務省", "文部科学省", "環境省", "防衛省", "外務省",
    "金融庁", "消費者庁", "デジタル庁", "警察庁", "国税庁",
    "東京都庁", "大阪府庁", "内閣府", "宮内庁",
]
HOSPITALS = [
    "東京医科大学病院", "慶應義塾大学病院", "順天堂大学医学部附属病院",
    "国立がん研究センター", "聖路加国際病院",
    "東京大学医学部附属病院", "大阪大学医学部附属病院", "虎の門病院",
    "国立国際医療研究センター", "東京女子医科大学病院",
]
RESEARCH_INSTITUTES = [
    "理化学研究所", "産業技術総合研究所", "国立情報学研究所",
    "宇宙航空研究開発機構", "日本原子力研究開発機構",
    "国立環境研究所", "物質・材料研究機構",
]

# --- BANK ---
BANK_NAMES = [
    "三菱UFJ銀行", "三井住友銀行", "みずほ銀行", "りそな銀行",
    "ゆうちょ銀行", "横浜銀行", "千葉銀行", "静岡銀行",
    "福岡銀行", "北海道銀行", "楽天銀行", "住信SBIネット銀行",
]
BRANCH_NAMES = [
    "渋谷支店", "新宿支店", "本店", "東京営業部", "梅田支店",
    "名古屋支店", "横浜支店", "大宮支店", "博多支店", "札幌支店",
]
ACCOUNT_TYPES_JA = ["普通", "当座"]

# --- DATE_OF_BIRTH ---
ERA_NAMES = {
    "昭和": (1926, 1989),
    "平成": (1989, 2019),
    "令和": (2019, 2026),
}
ERA_ABBREV = {"昭和": "S", "平成": "H", "令和": "R"}
KANJI_DIGITS = {
    0: "〇", 1: "一", 2: "二", 3: "三", 4: "四",
    5: "五", 6: "六", 7: "七", 8: "八", 9: "九",
    10: "十", 20: "二十", 30: "三十", 40: "四十", 50: "五十", 60: "六十",
}

DOB_CONTEXTS = [
    "生年月日: {dob}",
    "生年月日　{dob}",
    "{dob}生まれ",
    "{dob}生",
    "誕生日: {dob}",
    "出生日: {dob}",
    "生年月日（西暦）: {dob}",
    "DOB: {dob}",
]


def _random_person() -> str:
    last = random.choice(LAST_NAMES)
    first = random.choice(FIRST_NAMES_M + FIRST_NAMES_F)
    return f"{last}{first}"


def _random_address() -> str:
    pref = random.choice(PREFECTURES)
    city = random.choice(CITIES)
    town = random.choice(TOWNS)
    banchi = f"{random.randint(1, 30)}-{random.randint(1, 30)}-{random.randint(1, 30)}"
    parts = [pref, city, town, banchi]
    if random.random() < 0.3:
        parts.insert(0, f"〒{random.randint(100, 999)}-{random.randint(1000, 9999)}")
    if random.random() < 0.3:
        parts.append(f"{random.choice(BUILDINGS)}{random.randint(1, 30)}階")
    return "".join(parts) if random.random() < 0.5 else " ".join(parts)


def _random_org() -> str:
    kind = random.choice(["company", "company", "university", "government", "hospital", "research"])
    if kind == "company":
        name = random.choice(COMPANY_NAMES)
        r = random.random()
        if r < 0.25:
            prefix = random.choice(COMPANY_PREFIXES)
            return f"{prefix}{name}"
        elif r < 0.40:
            return f"{name}"  # 略称パターン (NTTデータ, 日立製作所)
        elif r < 0.55:
            suffix = random.choice(COMPANY_SUFFIXES)
            return f"{name}{suffix}"
        elif r < 0.70:
            # 略称プレフィックス: （株）サンプル
            abbrev = random.choice(COMPANY_PREFIXES_ABBREV)
            return f"{abbrev}{name}"
        elif r < 0.80:
            # 後置法人格: サンプル株式会社
            prefix = random.choice(["株式会社", "有限会社", "合同会社"])
            return f"{name}{prefix}"
        else:
            prefix = random.choice(COMPANY_PREFIXES)
            return f"{prefix}{name}"
    elif kind == "university":
        uni = random.choice(UNIVERSITIES)
        if random.random() < 0.3:
            dept = random.choice(["法学部", "経済学部", "工学部", "医学部", "理学部", "文学部"])
            return f"{uni}{dept}"
        return uni
    elif kind == "government":
        return random.choice(GOVERNMENT)
    elif kind == "research":
        return random.choice(RESEARCH_INSTITUTES)
    else:
        return random.choice(HOSPITALS)


def _random_bank() -> str:
    bank = random.choice(BANK_NAMES)
    branch = random.choice(BRANCH_NAMES)
    acct_type = random.choice(ACCOUNT_TYPES_JA)
    acct_num = str(random.randint(1000000, 9999999))
    fmt = random.choice([
        "{bank} {branch} {type} {num}",
        "{bank}{branch} {type} {num}",
        "{bank} {branch} {type}口座 {num}",
        "{bank}（{branch}）{type} {num}",
    ])
    return fmt.format(bank=bank, branch=branch, type=acct_type, num=acct_num)


def _num_to_kanji(n: int) -> str:
    """数値を漢数字に変換する (1-99)。"""
    if n == 0:
        return "〇"
    if n <= 9:
        return KANJI_DIGITS[n]
    tens = (n // 10) * 10
    ones = n % 10
    result = KANJI_DIGITS.get(tens, "")
    if ones:
        result += KANJI_DIGITS[ones]
    return result


def _random_dob() -> str:
    year = random.randint(1950, 2005)
    month = random.randint(1, 12)
    day = random.randint(1, 28)

    # 30% era abbreviation (S40.5.10, H2/3/15, R1.5.1)
    # 20% full era (昭和40年5月10日)
    # 5% kanji numerals (昭和四十年五月十日)
    # 5% compact YYYYMMDD
    # 40% western formats
    r = random.random()

    # Find matching era
    era_name = "昭和"
    era_year = year - 1926 + 1
    for era, (start, end) in ERA_NAMES.items():
        if start <= year < end:
            era_name = era
            era_year = year - start + 1
            break

    if r < 0.30:
        # Era abbreviation formats (S40.5.10, H2/3/15, R1.5.1)
        abbrev = ERA_ABBREV[era_name]
        sep = random.choice([".", "/", "-"])
        if random.random() < 0.5:
            return f"{abbrev}{era_year}{sep}{month}{sep}{day}"
        else:
            return f"{abbrev}{era_year:02d}{sep}{month:02d}{sep}{day:02d}"
    elif r < 0.50:
        # Full era formats
        fmt_r = random.random()
        if fmt_r < 0.4:
            return f"{era_name}{era_year}年{month}月{day}日"
        elif fmt_r < 0.7:
            return f"{era_name}{era_year}年{month:02d}月{day:02d}日"
        else:
            return f"{era_name}{era_year}/{month:02d}/{day:02d}"
    elif r < 0.55:
        # Kanji numeral format (昭和四十年五月十日)
        ey = _num_to_kanji(era_year)
        em = _num_to_kanji(month)
        ed = _num_to_kanji(day)
        return f"{era_name}{ey}年{em}月{ed}日"
    elif r < 0.60:
        # Compact YYYYMMDD
        return f"{year}{month:02d}{day:02d}"
    else:
        # Western formats
        return random.choice([
            f"{year}年{month}月{day}日",
            f"{year}年{month:02d}月{day:02d}日",
            f"{year}/{month:02d}/{day:02d}",
            f"{year}-{month:02d}-{day:02d}",
            f"{year}.{month:02d}.{day:02d}",
        ])


# --- テンプレート ---

PERSON_TEMPLATES = [
    "担当者: {person}",
    "申請者氏名: {person}",
    "{person}様にご連絡いたします。",
    "署名: {person}",
    "以上の通り報告いたします。\n報告者: {person}",
    "{person}（{person_kana}）",
    "患者名: {person}\n診察券番号: 12345",
    "{person}先生にご相談ください。",
    "面接官: {person}\n日時: 2024年4月1日 14:00",
    "保証人: {person}\n住所: {address}",
    "代表者: {person}\n{org}",
    "被保険者名: {person}\n被保険者番号: 1234567890",
]

ORG_TEMPLATES = [
    "{org}の発表によると、今期の業績は好調だった。",
    "{org}は新たなサービスを開始した。",
    "{org}に勤務する{person}氏が受賞した。",
    "{org}と{org2}が業務提携を発表した。",
    "問い合わせ先: {org}",
    "{person}は{org}の代表取締役を務めている。",
    "{org}（以下「当社」）は、下記の通りお知らせいたします。",
    "勤務先: {org}\n役職: 部長\n氏名: {person}",
    "{org}の{person}部長より連絡がありました。",
    "{org}への転職を希望しています。現在は{org2}に在籍中です。",
    "契約先: {org}\n担当: {person}\n契約日: 2024年4月1日",
    "所属: {org}\n社員番号: E-12345\n氏名: {person}",
    "{person}様\n{org}人事部より内定通知をお送りします。",
    "発注先: {org}\n発注番号: PO-2024-001",
    "取引先: {org}\n振込先口座: {bank}",
]

DOB_TEMPLATES = [
    "氏名: {person}\n生年月日: {dob}\n住所: {address}",
    "患者名: {person}\n{dob}生まれ\n{org}に通院中",
    "受験者: {person}\n生年月日 {dob}\n所属: {org}",
    "被保険者: {person}\n生年月日: {dob}\n{address}在住",
    "申請者: {person}\n出生日: {dob}\n連絡先住所: {address}",
    "利用者名: {person}\n生年月日: {dob}\n勤務先: {org}",
    "契約者: {person}（{dob}生）\n住所: {address}\n勤務先: {org}",
    "入居者: {person}\n生年月日: {dob}\n口座: {bank}",
    "児童名: {person}\n生年月日 {dob}\n保護者連絡先: {address}",
    "被験者ID: SBJ-001\n氏名: {person}\n生年月日: {dob}",
]

BANK_TEMPLATES = [
    "振込先: {bank}\n口座名義: {person}",
    "給与振込口座\n{bank}\n名義: {person}\n勤務先: {org}",
    "返金先口座: {bank}\n{person}様宛",
    "送金先情報\n{bank}\n受取人: {person}\n住所: {address}",
    "お振込先\n{bank}\nお受取人名: {person}",
    "口座情報: {bank}\n名義人: {person}\n住所: {address}",
    "報酬振込先: {bank}\n受取人: {person}\n所属: {org}",
    "引落口座: {bank}\n契約者: {person}",
    "還付金振込先口座\n{bank}\n申請者: {person}\n生年月日: {dob}",
]

# --- ORG隣接パターン（スペースなしで人名と隣接） ---
ORG_ADJACENT_TEMPLATES = [
    "{org}{person}代表取締役",
    "{org}取締役{person}",
    "{person}{org}所属",
    "{org}{person}部長より報告",
    "送付先: {address}\n{org}{person}宛",
    "{person}（{org}）に連絡してください。",
    "担当: {org}{person}（内線1234）",
    "{org}の{person}課長が{org2}との連携を発表した。",
    "発信元: {person}（{org}営業部）\n宛先: {address}",
    "{person}氏は{org}を退職し、{org2}に移籍した。",
]

# --- DOB複数日付パターン（1文書に複数日付、DOBは1つだけ） ---
DOB_MULTIDATE_TEMPLATES = [
    "報告日: 2024年4月1日\n対象者: {person}（{dob}生まれ）\n面談実施日: 2024年3月15日",
    "入社日: 2010年4月1日\n{person}（{dob}生）は2024年3月31日付で退職届を提出した。",
    "契約日: 令和6年4月1日\n契約者: {person}\n生年月日: {dob}\n更新日: 令和7年3月31日",
    "作成日: 2024年1月15日\n被保険者: {person}\n生年月日: {dob}\n資格取得日: 2020年4月1日",
    "受付日: 令和5年12月20日\n申請者: {person}\n{dob}生\n交付予定日: 令和6年1月10日",
    "診察日: 2024年2月14日\n患者: {person}（{dob}生まれ）\n次回予約: 2024年3月14日\n住所: {address}",
    "面接日: 2024年6月1日\n応募者: {person}\n生年月日 {dob}\n所属: {org}\n入社希望日: 2024年8月1日",
    "発行日: 令和6年3月1日\n被験者: {person}\n生年月日: {dob}\n試験開始日: 令和6年4月15日\n試験終了日: 令和6年10月31日",
]

# --- 最小コンテキスト/構造化ノイズパターン ---
MINIMAL_CONTEXT_TEMPLATES = [
    "{person}/{org}/{address}/{dob}",
    "{person} {dob} {address} {org}",
    "{person}　{org}　{address}　{dob}",
    "名前:{person}\n会社:{org}\n生年月日:{dob}",
    "[{person}] [{org}] [{dob}] [{address}]",
]

# --- 構造化ノイズ内のエンティティ ---
STRUCTURED_NOISE_TEMPLATES = [
    '{{"name": "{person}", "company": "{org}", "dob": "{dob}"}}',
    "name,company,dob,address\n{person},{org},{dob},{address}",
    "2024-03-15 10:30:22 [INFO] customer_update: name={person} org={org} dob={dob}",
    "<div class=\"profile\">{person} | {org} | {dob} | {address}</div>",
]

COMBINED_TEMPLATES = [
    "氏名: {person}\n生年月日: {dob}\n住所: {address}\n勤務先: {org}\n口座: {bank}",
    "{person}（{dob}生）\n{address}\n{org}所属\n給与口座: {bank}",
]

# --- 負例テキスト ---
NEGATIVE_TEXTS = [
    "本日の天気は晴れ時々曇り、最高気温は28度の見込みです。",
    "4月1日より新年度が始まります。各部署の目標を確認してください。",
    "受付番号: 12345678。お呼びするまでお待ちください。",
    "渋谷駅周辺で大規模な再開発工事が進んでいます。",
    "注文番号A-987654の商品は本日発送済みです。",
    "会議は15時から3階の大会議室で行います。部長も出席予定です。",
    "先月の売上は前年比120%で好調でした。",
    "新しいシステムのログインIDは社員番号と同じです。",
    "東京マラソンの参加者は今年も3万人を超えました。",
    "委員会の報告書は来週金曜日までに提出してください。",
    "プロジェクトのマイルストーンは6月30日に設定されています。",
    "サーバーのCPU使用率が80%を超えています。確認をお願いします。",
    "田中方式による分析の結果、品質基準を満たしていることが確認されました。",
    "太郎くんのプログラミング入門は初心者にもおすすめの一冊です。",
    "渋谷のあたりで待ち合わせしよう。3丁目の交差点のところ。",
    "東京タワーは1958年に完成した日本を代表するランドマークです。",
    "シリアル番号: SN-20240101-00123。保証期間は2年間です。",
    "当ビルの管理組合総会は毎年5月に開催されます。",
    "レシピ: 材料は鶏もも肉300g、玉ねぎ1個、にんじん1本。",
    "本日のセミナーは定員に達したため受付を終了しました。",
]

# --- ディストラクタテキスト（PIIに似ているが違うもの） ---
DISTRACTOR_TEXTS = [
    "山田式トレーニングは週3回の実施が推奨されています。",
    "東京都の人口は約1400万人です。",
    "製品番号: 03-1234-5678。在庫をご確認ください。",
    "令和6年度の予算案が国会で審議されています。",
    "銀行の営業時間は9:00〜15:00です。",
    "渋谷区の人口密度は全国でもトップクラスです。",
    "2024年3月15日は確定申告の締め切りです。",
    "佐藤記念病院は地域の中核医療機関です。",
    "三菱重工業は航空宇宙分野でも実績があります。",
    "平成の時代は1989年から2019年まで続きました。",
]

# --- ORGハードネガティブ（ORGに似ているが非ORG） ---
ORG_HARD_NEGATIVE_TEXTS = [
    "東京マラソンのエントリーは11月1日に開始されます。国立競技場がゴール地点です。",
    "渋谷ヒカリエの8階にあるイベントスペースで発表会が開催された。",
    "PayPayの利用者数が5000万人を突破。キャッシュバックキャンペーンも実施中。",
    "ISO9001の認証を更新するため、品質管理部が監査対応にあたっている。",
    "個人情報保護法の改正により、クッキー同意の取得が義務化される見通し。",
    "マイナンバー制度の利用範囲が拡大され、健康保険証との一体化が進んでいる。",
    "成田空港第3ターミナルの改修工事は来年3月に完了予定です。",
    "ふるさと納税の返礼品として、地元産の和牛が人気を集めている。",
    "営業部の月次会議は毎月第2月曜日に開催。総務課からの連絡事項も確認する。",
    "六本木ヒルズ森タワーの展望台からは東京タワーとスカイツリーが一望できる。",
    "TOEIC800点以上が応募条件。簿記検定2級も歓迎スキルとして記載されている。",
    "基幹システムの移行プロジェクトが第3四半期に開始。SAPシステムへの切り替えを予定。",
    "グランフロント大阪で開催されたAI展示会に5万人が来場した。",
    "労働基準法に基づき、残業時間の上限は月45時間と定められている。",
    "東京駅丸の内口から徒歩5分。東京国際フォーラムの隣に位置する。",
    "確定申告説明会は税務署の1階ホールで2月16日から3月15日まで開催。",
    "クローズアップ現代で特集された働き方改革の現状について議論が続いている。",
    "Suicaの残高が不足しています。チャージは駅の券売機またはコンビニで可能です。",
    "開発チームのスプリントレビューは金曜日の15時から。QAチームも参加予定。",
    "羽田空港国際線ターミナルのラウンジが改装オープン。新しい搭乗ゲートも完成した。",
]


def _build_doc(template: str, **kwargs) -> dict | None:
    """テンプレートから文書を構築する。"""
    entities = []
    result = template

    tag_map = {
        "person": "PERSON",
        "person_kana": "PERSON",
        "dob": "DATE_OF_BIRTH",
        "address": "ADDRESS",
        "org": "ORGANIZATION",
        "org2": "ORGANIZATION",
        "bank": "BANK_ACCOUNT",
    }

    # プレースホルダーを検出して順番に置換
    import re
    placeholders = list(re.finditer(r"\{(\w+)\}", template))

    offset_adjust = 0
    for m in placeholders:
        key = m.group(1)
        if key not in kwargs or not kwargs[key]:
            continue

        label = tag_map.get(key)
        value = kwargs[key]
        placeholder = m.group(0)

        pos = result.find(placeholder)
        if pos == -1:
            continue

        result = result[:pos] + value + result[pos + len(placeholder):]

        if label:
            entities.append({
                "start": pos,
                "end": pos + len(value),
                "label": label,
                "text": value,
            })

    # まだ未置換のプレースホルダーがあれば削除
    result = re.sub(r"\{\w+\}", "", result)

    if not entities and "{" not in template:
        return {"text": result, "entities": []}

    if not entities:
        return None

    return {"text": result, "entities": entities}


def generate_augmented_docs(count: int = 1000) -> list[dict]:
    """日本語拡張データを生成する。

    v0.4.0ベンチマーク弱点を重点強化:
    - ORGANIZATION: 隣接パターン、略称、構造化ノイズ
    - DATE_OF_BIRTH: 元号略記、複数日付、最小コンテキスト
    """
    docs = []

    # PERSON多様化 (8%)
    for _ in range(int(count * 0.08)):
        name_type = random.choice(["kanji", "kanji", "katakana", "foreign"])
        if name_type == "kanji":
            person = _random_person()
            template = random.choice([t for t in PERSON_TEMPLATES if t != "{person}（{person_kana}）"])
            doc = _build_doc(template, person=person, address=_random_address(), org=_random_org())
        elif name_type == "katakana":
            person = random.choice(KATAKANA_NAMES)
            kanji = _random_person()
            template = "{person}（{person_kana}）"
            doc = _build_doc(template, person=kanji, person_kana=person)
        else:
            person = random.choice(FOREIGN_NAMES)
            template = random.choice([t for t in PERSON_TEMPLATES if t != "{person}（{person_kana}）"])
            doc = _build_doc(template, person=person, address=_random_address(), org=_random_org())
        if doc:
            docs.append(doc)

    # ORGANIZATION標準テンプレート (12%)
    for _ in range(int(count * 0.12)):
        org = _random_org()
        person = _random_person()
        org2 = _random_org()
        bank = _random_bank()
        template = random.choice(ORG_TEMPLATES)
        doc = _build_doc(template, org=org, person=person, org2=org2, bank=bank)
        if doc:
            docs.append(doc)

    # ORGANIZATION隣接パターン (10% - v0.4.0弱点)
    for _ in range(int(count * 0.10)):
        org = _random_org()
        org2 = _random_org()
        person = _random_person()
        address = _random_address()
        template = random.choice(ORG_ADJACENT_TEMPLATES)
        doc = _build_doc(template, org=org, org2=org2, person=person, address=address)
        if doc:
            docs.append(doc)

    # DATE_OF_BIRTH標準テンプレート (8%)
    for _ in range(int(count * 0.08)):
        dob = _random_dob()
        person = _random_person()
        address = _random_address()
        org = _random_org()
        template = random.choice(DOB_TEMPLATES)
        doc = _build_doc(template, person=person, dob=dob, address=address, org=org)
        if doc:
            docs.append(doc)

    # DATE_OF_BIRTH複数日付パターン (10% - v0.4.0弱点)
    for _ in range(int(count * 0.10)):
        dob = _random_dob()
        person = _random_person()
        address = _random_address()
        org = _random_org()
        template = random.choice(DOB_MULTIDATE_TEMPLATES)
        doc = _build_doc(template, person=person, dob=dob, address=address, org=org)
        if doc:
            docs.append(doc)

    # BANK_ACCOUNT多様化 (10%)
    for _ in range(int(count * 0.10)):
        bank = _random_bank()
        person = _random_person()
        address = _random_address()
        org = _random_org()
        dob = _random_dob()
        template = random.choice(BANK_TEMPLATES)
        doc = _build_doc(template, person=person, bank=bank, address=address, org=org, dob=dob)
        if doc:
            docs.append(doc)

    # 全エンティティ含む文書 (8%)
    for _ in range(int(count * 0.08)):
        template = random.choice(COMBINED_TEMPLATES)
        doc = _build_doc(
            template,
            person=_random_person(),
            dob=_random_dob(),
            address=_random_address(),
            org=_random_org(),
            bank=_random_bank(),
        )
        if doc:
            docs.append(doc)

    # 最小コンテキスト (6% - v0.4.0弱点)
    for _ in range(int(count * 0.06)):
        template = random.choice(MINIMAL_CONTEXT_TEMPLATES)
        doc = _build_doc(
            template,
            person=_random_person(),
            dob=_random_dob(),
            address=_random_address(),
            org=_random_org(),
        )
        if doc:
            docs.append(doc)

    # 構造化ノイズ (6% - v0.4.0弱点)
    for _ in range(int(count * 0.06)):
        template = random.choice(STRUCTURED_NOISE_TEMPLATES)
        doc = _build_doc(
            template,
            person=_random_person(),
            dob=_random_dob(),
            address=_random_address(),
            org=_random_org(),
        )
        if doc:
            docs.append(doc)

    # 負例: PIIなしテキスト (12%)
    neg_count = int(count * 0.12)
    for i in range(neg_count):
        text = NEGATIVE_TEXTS[i % len(NEGATIVE_TEXTS)]
        docs.append({"text": text, "entities": []})

    # ディストラクタ: PII風の非PIIテキスト (7%)
    dist_count = int(count * 0.07)
    for i in range(dist_count):
        text = DISTRACTOR_TEXTS[i % len(DISTRACTOR_TEXTS)]
        docs.append({"text": text, "entities": []})

    # ORGハードネガティブ: ORG風だが非ORGのテキスト (8%)
    org_neg_count = int(count * 0.08)
    for i in range(org_neg_count):
        text = ORG_HARD_NEGATIVE_TEXTS[i % len(ORG_HARD_NEGATIVE_TEXTS)]
        docs.append({"text": text, "entities": []})

    random.shuffle(docs)
    return docs


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="JA data augmentation")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path(__file__).parents[2] / "data" / "raw" / "ja" / "generated.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    parser.add_argument("--augment-count", type=int, default=1000)
    args = parser.parse_args()

    output = args.output or args.input.parent / "augmented.json"

    print(f"Loading {args.input}...")
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)
    print(f"Original: {len(data)} documents")

    augmented = generate_augmented_docs(args.augment_count)
    print(f"Generated {len(augmented)} augmented documents")

    data.extend(augmented)
    print(f"Total: {len(data)} documents")

    # Stats
    from collections import Counter
    labels = Counter()
    neg = 0
    for doc in data:
        if not doc["entities"]:
            neg += 1
        for ent in doc["entities"]:
            labels[ent["label"]] += 1

    print(f"\nNegative documents: {neg}")
    print("Entity counts:")
    for label, count in sorted(labels.items()):
        print(f"  {label}: {count}")

    with open(output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\nSaved to {output}")


if __name__ == "__main__":
    main()
