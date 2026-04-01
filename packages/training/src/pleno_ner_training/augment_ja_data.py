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
COMPANY_PREFIXES = ["株式会社", "有限会社", "合同会社", "一般社団法人", "NPO法人"]
COMPANY_NAMES = [
    "プレノ", "テックソリューションズ", "サンライズ", "グローバルテック",
    "ネクストイノベーション", "デジタルフロンティア", "スマートシステムズ",
    "フューチャーラボ", "クリエイティブワークス", "アドバンストテクノロジー",
]
UNIVERSITIES = [
    "東京大学", "京都大学", "大阪大学", "早稲田大学", "慶應義塾大学",
    "東北大学", "九州大学", "北海道大学", "名古屋大学", "筑波大学",
]
GOVERNMENT = [
    "厚生労働省", "国土交通省", "総務省", "経済産業省", "法務省",
    "財務省", "文部科学省", "環境省", "防衛省", "外務省",
]
HOSPITALS = [
    "東京医科大学病院", "慶應義塾大学病院", "順天堂大学医学部附属病院",
    "国立がん研究センター", "聖路加国際病院",
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

DOB_CONTEXTS = [
    "生年月日: {dob}",
    "生年月日　{dob}",
    "{dob}生まれ",
    "{dob}生",
    "誕生日: {dob}",
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
    kind = random.choice(["company", "university", "government", "hospital"])
    if kind == "company":
        prefix = random.choice(COMPANY_PREFIXES)
        name = random.choice(COMPANY_NAMES)
        return f"{prefix}{name}" if random.random() < 0.5 else f"{name}{prefix[:-1] if prefix.endswith('人') else ''}"
    elif kind == "university":
        return random.choice(UNIVERSITIES)
    elif kind == "government":
        return random.choice(GOVERNMENT)
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


def _random_dob() -> str:
    use_era = random.random() < 0.5
    year = random.randint(1950, 2005)
    month = random.randint(1, 12)
    day = random.randint(1, 28)

    if use_era:
        for era, (start, end) in ERA_NAMES.items():
            if start <= year < end:
                era_year = year - start + 1
                return f"{era}{era_year}年{month}月{day}日"
        return f"昭和{year - 1926 + 1}年{month}月{day}日"
    else:
        fmt = random.choice([
            f"{year}年{month}月{day}日",
            f"{year}/{month:02d}/{day:02d}",
            f"{year}-{month:02d}-{day:02d}",
        ])
        return fmt


# --- テンプレート ---

PERSON_TEMPLATES = [
    "担当者: {person}",
    "申請者氏名: {person}",
    "{person}様にご連絡いたします。",
    "署名: {person}",
    "以上の通り報告いたします。\n報告者: {person}",
    "{person}（{person_kana}）",
]

ORG_TEMPLATES = [
    "{org}の発表によると、今期の業績は好調だった。",
    "{org}は新たなサービスを開始した。",
    "{org}に勤務する{person}氏が受賞した。",
    "{org}と{org2}が業務提携を発表した。",
    "問い合わせ先: {org}",
]

DOB_TEMPLATES = [
    "氏名: {person}\n生年月日: {dob}\n住所: {address}",
    "患者名: {person}\n{dob}生まれ\n{org}に通院中",
    "受験者: {person}\n生年月日 {dob}\n所属: {org}",
    "被保険者: {person}\n生年月日: {dob}\n{address}在住",
]

BANK_TEMPLATES = [
    "振込先: {bank}\n口座名義: {person}",
    "給与振込口座\n{bank}\n名義: {person}\n勤務先: {org}",
    "返金先口座: {bank}\n{person}様宛",
    "送金先情報\n{bank}\n受取人: {person}\n住所: {address}",
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
    """日本語拡張データを生成する。"""
    docs = []

    # PERSON多様化 (10%)
    for _ in range(int(count * 0.10)):
        name_type = random.choice(["kanji", "katakana", "foreign"])
        if name_type == "kanji":
            person = _random_person()
            template = random.choice(PERSON_TEMPLATES[:5])
            doc = _build_doc(template, person=person)
        elif name_type == "katakana":
            person = random.choice(KATAKANA_NAMES)
            kanji = _random_person()
            template = PERSON_TEMPLATES[5]  # {person}（{person_kana}）
            doc = _build_doc(template, person=kanji, person_kana=person)
        else:
            person = random.choice(FOREIGN_NAMES)
            template = random.choice(PERSON_TEMPLATES[:5])
            doc = _build_doc(template, person=person)
        if doc:
            docs.append(doc)

    # ORGANIZATION多様化 (15%)
    for _ in range(int(count * 0.15)):
        org = _random_org()
        person = _random_person()
        org2 = _random_org()
        template = random.choice(ORG_TEMPLATES)
        doc = _build_doc(template, org=org, person=person, org2=org2)
        if doc:
            docs.append(doc)

    # DATE_OF_BIRTH多様化 (15%)
    for _ in range(int(count * 0.15)):
        dob = _random_dob()
        person = _random_person()
        address = _random_address()
        org = _random_org()
        template = random.choice(DOB_TEMPLATES)
        doc = _build_doc(template, person=person, dob=dob, address=address, org=org)
        if doc:
            docs.append(doc)

    # BANK_ACCOUNT多様化 (10%)
    for _ in range(int(count * 0.10)):
        bank = _random_bank()
        person = _random_person()
        address = _random_address()
        org = _random_org()
        template = random.choice(BANK_TEMPLATES)
        doc = _build_doc(template, person=person, bank=bank, address=address, org=org)
        if doc:
            docs.append(doc)

    # 全エンティティ含む文書 (10%)
    for _ in range(int(count * 0.10)):
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

    # 負例: PIIなしテキスト (20%)
    neg_count = int(count * 0.20)
    for i in range(neg_count):
        text = NEGATIVE_TEXTS[i % len(NEGATIVE_TEXTS)]
        docs.append({"text": text, "entities": []})

    # ディストラクタ: PII風の非PIIテキスト (20%)
    dist_count = int(count * 0.20)
    for i in range(dist_count):
        text = DISTRACTOR_TEXTS[i % len(DISTRACTOR_TEXTS)]
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
