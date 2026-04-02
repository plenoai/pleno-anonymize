"""Deterministic curated benchmark builders."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from typing import Literal

import pleno_ner_training.augment_ja_data as aug

EntityLabel = Literal[
    "PERSON",
    "ADDRESS",
    "ORGANIZATION",
    "DATE_OF_BIRTH",
    "BANK_ACCOUNT",
]
Segment = tuple[str, EntityLabel | None]
DocGenerator = Callable[[random.Random], dict]

_FULL_WIDTH_SPACE = "\u3000"
_FIRST_NAMES = aug.FIRST_NAMES_M + aug.FIRST_NAMES_F
_GEO_ADDRESS_MAP = {
    "西新宿": "東京都新宿区西新宿2-8-1",
    "日本橋": "東京都中央区日本橋2-3-4",
    "港南": "東京都港区港南2-3-4",
    "渋谷": "東京都渋谷区桜丘町5-6",
    "青葉橋": "横浜市青葉区青葉台1-2-3",
    "札幌北一条": "札幌市中央区北1条西2-3",
}
_ORTHOGRAPHIC_PEOPLE = [
    "山田太郎",
    "山　田　太　郎",
    "やまだ たろう",
    "ヤマダ タロウ",
    "Taro YAMADA",
    "サトウ ハナコ",
    "マイケル・ジョンソン",
    "リー・ウェイ",
]
_ORTHOGRAPHIC_ADDRESSES = [
    "東京都渋谷区神宮前一丁目二番三号",
    "Shibuya-ku, Jingumae 1-2-3, Tokyo",
    "渋谷区神宮前1-2-3-405",
    "東京都渋谷区大字神宮前字北沢壱百弐拾参番地",
    "横浜市中区本町2-3-4",
]
_ORTHOGRAPHIC_ORGS = [
    "（株）サンプルホールディングス",
    "Sony Group Corporation",
    "株式會社三菱重工業",
    "特定非営利活動法人子ども支援ネットワーク",
    "一般社団法人青葉地域連携会",
]
_ORTHOGRAPHIC_DOBS = [
    "S40.5.10",
    "H2/3/15",
    "R1.5.1",
    "90.3.15",
    "（昭和四十年五月十日）",
]
_ORTHOGRAPHIC_BANKS = [
    "UFJ 新宿 普 1234567",
    "MUFGJPJT 普通 00-1234567",
    "ゆうちょ銀行 記号10100 番号12345671",
    "みずほ 渋谷 フツウ 7654321",
]
_PLACEHOLDER_VALUES = {
    "氏名": ["未入力", "sample_user", "sample_name_01", "<blank>"],
    "住所": ["登録待ち", "sample-pref-sample-city", "pref-city-town-1-2-3", "<pending>"],
    "生年月日": ["YYYY-MM-DD", "YYYY/MM/DD", "19XX-XX-XX", "<unknown>"],
    "口座": ["受付停止中", "bank-branch-kind-number", "TEMP-HOLD", "<masked>"],
    "所属": ["法人未登録", "外部委託", "UNASSIGNED", "org-unit-temp"],
}
_PLACEHOLDER_NOTES = [
    "この画面の表示値はすべて説明用ダミーです。",
    "実データは入力しないでください。",
    "申請時点では個人情報を受領していません。",
    "本入力例は検証用で、照合対象は存在しません。",
]
_LABEL_STUB_BLOCKS = [
    "CSVヘッダー: name,dob,address,organization,bank_account。",
    "画面定義: prefecture-city-block / branch-main ordinary 0000000 / sample_name_01。",
    "ログプレフィックス: organization=UNASSIGNED, account=TEMP-HOLD, address=pref-city-town-1-2-3。",
    "仕様値: route=branch-main, location=west-block-3, register=YYYY/MM/DD。",
    "帳票例: order=A-2026-0042, ref=03-1234-5678, label=org-unit-temp。",
]
_UNTAGGED_BRANCH_NAMES = [
    "西新宿本社前",
    "中央病院前交差点",
    "日本橋支店前",
    "渋谷オフィス街",
    "札幌北一条駅前",
]
_UNTAGGED_DUMMY_VALUES = [
    "氏名: 該当なし",
    "住所: ○○県○○市",
    "生年月日: 19XX年XX月XX日",
    "口座: 現金払い",
]
_STATUS_DATES = [
    "2026年4月1日",
    "2026年5月31日",
    "2026/04/12",
    "令和8年4月1日",
    "令和8年7月1日",
    "2026-08-20",
]
_PHONE_LIKE_VALUES = ["03-1234-5678", "06-9876-5432", "050-3124-7788"]
_ORDER_LIKE_VALUES = ["A-2026-0042", "INV-778812", "REQ-09-4412", "SN-20260402-00123"]


def _compose(segments: Sequence[Segment]) -> dict:
    text_parts: list[str] = []
    entities: list[dict] = []
    offset = 0

    for value, label in segments:
        text_parts.append(value)
        if label is not None:
            entities.append(
                {
                    "start": offset,
                    "end": offset + len(value),
                    "label": label,
                    "text": value,
                }
            )
        offset += len(value)

    return {"text": "".join(text_parts), "entities": entities}


def _seg(value: str, label: EntityLabel | None = None) -> Segment:
    return (value, label)


def _random_person(rng: random.Random) -> str:
    return f"{rng.choice(aug.LAST_NAMES)}{rng.choice(_FIRST_NAMES)}"


def _random_address(rng: random.Random) -> str:
    pref = rng.choice(aug.PREFECTURES)
    city = rng.choice(aug.CITIES)
    town = rng.choice(aug.TOWNS)
    banchi = f"{rng.randint(1, 30)}-{rng.randint(1, 30)}-{rng.randint(1, 30)}"
    parts = [pref, city, town, banchi]
    if rng.random() < 0.35:
        parts.insert(0, f"〒{rng.randint(100, 999)}-{rng.randint(1000, 9999)}")
    if rng.random() < 0.4:
        parts.append(f"{rng.choice(aug.BUILDINGS)}{rng.randint(2, 28)}F")
    return "".join(parts) if rng.random() < 0.6 else " ".join(parts)


def _random_org(rng: random.Random) -> str:
    kind = rng.choice(["company", "company", "university", "government", "hospital", "research"])
    if kind == "company":
        name = rng.choice(aug.COMPANY_NAMES)
        roll = rng.random()
        if roll < 0.2:
            return f"{rng.choice(aug.COMPANY_PREFIXES)}{name}"
        if roll < 0.4:
            return f"{rng.choice(aug.COMPANY_PREFIXES_ABBREV)}{name}"
        if roll < 0.55:
            return f"{name}{rng.choice(aug.COMPANY_SUFFIXES)}"
        if roll < 0.7:
            return f"{name}株式会社"
        return name
    if kind == "university":
        base = rng.choice(aug.UNIVERSITIES)
        if rng.random() < 0.35:
            return f"{base}{rng.choice(['法学部', '工学部', '医学部', '経済学部'])}"
        return base
    if kind == "government":
        return rng.choice(aug.GOVERNMENT)
    if kind == "hospital":
        return rng.choice(aug.HOSPITALS)
    return rng.choice(aug.RESEARCH_INSTITUTES)


def _num_to_kanji(value: int) -> str:
    if value == 0:
        return "〇"
    if value <= 9:
        return aug.KANJI_DIGITS[value]
    tens = (value // 10) * 10
    ones = value % 10
    text = aug.KANJI_DIGITS.get(tens, "")
    if ones:
        text += aug.KANJI_DIGITS[ones]
    return text


def _random_dob(rng: random.Random) -> str:
    year = rng.randint(1950, 2005)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)

    era_name = "昭和"
    era_year = year - 1926 + 1
    for name, (start, end) in aug.ERA_NAMES.items():
        if start <= year < end:
            era_name = name
            era_year = year - start + 1
            break

    roll = rng.random()
    if roll < 0.25:
        sep = rng.choice([".", "/", "-"])
        return f"{aug.ERA_ABBREV[era_name]}{era_year}{sep}{month}{sep}{day}"
    if roll < 0.45:
        return f"{era_name}{era_year}年{month}月{day}日"
    if roll < 0.55:
        return f"{era_name}{_num_to_kanji(era_year)}年{_num_to_kanji(month)}月{_num_to_kanji(day)}日"
    if roll < 0.65:
        return f"{year}{month:02d}{day:02d}"
    return rng.choice(
        [
            f"{year}年{month}月{day}日",
            f"{year}年{month:02d}月{day:02d}日",
            f"{year}/{month:02d}/{day:02d}",
            f"{year}-{month:02d}-{day:02d}",
            f"{year}.{month:02d}.{day:02d}",
        ]
    )


def _random_bank(rng: random.Random) -> str:
    bank = rng.choice(aug.BANK_NAMES)
    branch = rng.choice(aug.BRANCH_NAMES)
    acct_type = rng.choice(aug.ACCOUNT_TYPES_JA)
    acct_num = f"{rng.randint(1000000, 9999999)}"
    return rng.choice(
        [
            f"{bank} {branch} {acct_type} {acct_num}",
            f"{bank}{branch} {acct_type} {acct_num}",
            f"{bank}（{branch}）{acct_type} {acct_num}",
            f"{bank} {branch} {acct_type}口座 {acct_num}",
        ]
    )


def _random_geo_org(rng: random.Random) -> tuple[str, str, str]:
    geo = rng.choice(list(_GEO_ADDRESS_MAP))
    address = _GEO_ADDRESS_MAP[geo]
    suffix = rng.choice(
        [
            "データラボ株式会社",
            "支店サポート株式会社",
            "総合開発株式会社",
            "本社管理株式会社",
            "法律事務所",
            "運営機構",
        ]
    )
    return geo, address, f"{geo}{suffix}"


def _full_width_person(base: str) -> str:
    return _FULL_WIDTH_SPACE.join(base)


def _placeholder_registry_doc(rng: random.Random) -> dict:
    fragments = [
        f"氏名={rng.choice(_PLACEHOLDER_VALUES['氏名'])}",
        f"住所={rng.choice(_PLACEHOLDER_VALUES['住所'])}",
        f"生年月日={rng.choice(_PLACEHOLDER_VALUES['生年月日'])}",
        f"口座={rng.choice(_PLACEHOLDER_VALUES['口座'])}",
        f"organization={rng.choice(_PLACEHOLDER_VALUES['所属'])}",
        f"address={rng.choice(_PLACEHOLDER_VALUES['住所'])}",
        f"dob={rng.choice(_PLACEHOLDER_VALUES['生年月日'])}",
        f"bank_account={rng.choice(_PLACEHOLDER_VALUES['口座'])}",
    ]
    intro = rng.choice(["入力例", "仕様値", "仮登録", "ダミー表示", "検証サンプル"])
    text = f"{intro}: {' / '.join(rng.sample(fragments, k=4))}。{rng.choice(_PLACEHOLDER_NOTES)}"
    return {"text": text, "entities": []}


def _label_stub_catalog_doc(rng: random.Random) -> dict:
    blocks = rng.sample(_LABEL_STUB_BLOCKS, k=3)
    ending = rng.choice(
        [
            "値はすべてテンプレート用で、実体は存在しません。",
            "ダミー説明のため、実在情報との照合は行いません。",
            "この帳票は誤検出耐性の確認専用です。",
        ]
    )
    text = " ".join(blocks + [ending])
    return {"text": text, "entities": []}


def _fragment_chain_doc(rng: random.Random) -> dict:
    person1 = _random_person(rng)
    person2 = _random_person(rng)
    org = _random_org(rng)
    address = _random_address(rng)
    dob = _random_dob(rng)
    bank = _random_bank(rng)
    pattern = rng.randrange(4)
    if pattern == 0:
        return _compose(
            [
                _seg("控え ", None),
                _seg(person1, "PERSON"),
                _seg("/", None),
                _seg(person2, "PERSON"),
                _seg("/", None),
                _seg(address, "ADDRESS"),
                _seg("/", None),
                _seg(org, "ORGANIZATION"),
            ]
        )
    if pattern == 1:
        return _compose(
            [
                _seg(org, "ORGANIZATION"),
                _seg(person1, "PERSON"),
                _seg("担当 ", None),
                _seg(bank, "BANK_ACCOUNT"),
                _seg(" ", None),
                _seg(person2, "PERSON"),
            ]
        )
    if pattern == 2:
        return _compose(
            [
                _seg(person1, "PERSON"),
                _seg(person2, "PERSON"),
                _seg(" ", None),
                _seg(address, "ADDRESS"),
                _seg(" ", None),
                _seg(dob, "DATE_OF_BIRTH"),
            ]
        )
    return _compose(
        [
            _seg("一覧 ", None),
            _seg(person1, "PERSON"),
            _seg("・", None),
            _seg(person2, "PERSON"),
            _seg("・", None),
            _seg(org, "ORGANIZATION"),
            _seg("・", None),
            _seg(address, "ADDRESS"),
            _seg("・", None),
            _seg(bank, "BANK_ACCOUNT"),
        ]
    )


def _schema_bleed_doc(rng: random.Random) -> dict:
    person = _random_person(rng)
    address = _random_address(rng)
    org = _random_org(rng)
    dob = _random_dob(rng)
    bank = _random_bank(rng)
    pattern = rng.randrange(4)
    if pattern == 0:
        return _compose(
            [
                _seg('{"name":"', None),
                _seg(person, "PERSON"),
                _seg('","dob":"', None),
                _seg(dob, "DATE_OF_BIRTH"),
                _seg('","address":"', None),
                _seg(address, "ADDRESS"),
                _seg('","org":"', None),
                _seg(org, "ORGANIZATION"),
                _seg('"', None),
            ]
        )
    if pattern == 1:
        return _compose(
            [
                _seg('"row42","', None),
                _seg(person, "PERSON"),
                _seg('","', None),
                _seg(address, "ADDRESS"),
                _seg('","', None),
                _seg(bank, "BANK_ACCOUNT"),
                _seg('","status=open', None),
            ]
        )
    if pattern == 2:
        return _compose(
            [
                _seg("[INFO] sync user=", None),
                _seg(person, "PERSON"),
                _seg(" addr=", None),
                _seg(address, "ADDRESS"),
                _seg(" org=", None),
                _seg(org, "ORGANIZATION"),
                _seg(" dob=", None),
                _seg(dob, "DATE_OF_BIRTH"),
                _seg(" trace=retry", None),
            ]
        )
    return _compose(
        [
            _seg("<span>", None),
            _seg(person, "PERSON"),
            _seg("</span><br/>", None),
            _seg(org, "ORGANIZATION"),
            _seg("<br/>", None),
            _seg(address, "ADDRESS"),
            _seg("<li>", None),
            _seg(bank, "BANK_ACCOUNT"),
            _seg("</li>", None),
        ]
    )


def _orthography_shift_doc(rng: random.Random) -> dict:
    person = rng.choice(_ORTHOGRAPHIC_PEOPLE)
    if person == "山田太郎" and rng.random() < 0.5:
        person = _full_width_person(person)
    address = rng.choice(_ORTHOGRAPHIC_ADDRESSES)
    org = rng.choice(_ORTHOGRAPHIC_ORGS)
    dob = rng.choice(_ORTHOGRAPHIC_DOBS)
    bank = rng.choice(_ORTHOGRAPHIC_BANKS)
    pattern = rng.randrange(3)
    if pattern == 0:
        return _compose(
            [
                _seg(person, "PERSON"),
                _seg(" の送付先は ", None),
                _seg(address, "ADDRESS"),
                _seg("、所属は ", None),
                _seg(org, "ORGANIZATION"),
            ]
        )
    if pattern == 1:
        return _compose(
            [
                _seg("[", None),
                _seg(person, "PERSON"),
                _seg("] [", None),
                _seg(dob, "DATE_OF_BIRTH"),
                _seg("] [", None),
                _seg(bank, "BANK_ACCOUNT"),
                _seg("]", None),
            ]
        )
    return _compose(
        [
            _seg(person, "PERSON"),
            _seg(" / ", None),
            _seg(address, "ADDRESS"),
            _seg(" / ", None),
            _seg(org, "ORGANIZATION"),
            _seg(" / ", None),
            _seg(dob, "DATE_OF_BIRTH"),
        ]
    )


def _geo_org_refraction_doc(rng: random.Random) -> dict:
    geo, address, org = _random_geo_org(rng)
    person = _random_person(rng)
    bank = _random_bank(rng)
    untagged_place = rng.choice(_UNTAGGED_BRANCH_NAMES)
    pattern = rng.randrange(3)
    if pattern == 0:
        return _compose(
            [
                _seg("待ち合わせは", None),
                _seg(untagged_place, None),
                _seg("、所属は", None),
                _seg(org, "ORGANIZATION"),
                _seg("、送付先は", None),
                _seg(address, "ADDRESS"),
                _seg("。", None),
            ]
        )
    if pattern == 1:
        return _compose(
            [
                _seg(person, "PERSON"),
                _seg("は", None),
                _seg(org, "ORGANIZATION"),
                _seg("所属。集合場所は", None),
                _seg(f"{geo}本社前", None),
                _seg("、書類送付先は", None),
                _seg(address, "ADDRESS"),
            ]
        )
    return _compose(
        [
            _seg("振込先は", None),
            _seg(bank, "BANK_ACCOUNT"),
            _seg("、勤務先は", None),
            _seg(org, "ORGANIZATION"),
            _seg("、建物前は", None),
            _seg(f"{geo}オフィス街", None),
            _seg("、住所は", None),
            _seg(address, "ADDRESS"),
        ]
    )


def _date_switchyard_doc(rng: random.Random) -> dict:
    person = _random_person(rng)
    dob = _random_dob(rng)
    org = _random_org(rng)
    address = _random_address(rng)
    status_dates = rng.sample(_STATUS_DATES, k=3)
    pattern = rng.randrange(3)
    if pattern == 0:
        return _compose(
            [
                _seg("受付日: ", None),
                _seg(status_dates[0], None),
                _seg(" / 対象者: ", None),
                _seg(person, "PERSON"),
                _seg(" / 生年月日: ", None),
                _seg(dob, "DATE_OF_BIRTH"),
                _seg(" / 更新期限: ", None),
                _seg(status_dates[1], None),
                _seg(" / 勤務先: ", None),
                _seg(org, "ORGANIZATION"),
            ]
        )
    if pattern == 1:
        return _compose(
            [
                _seg("契約日: ", None),
                _seg(status_dates[0], None),
                _seg("。", None),
                _seg(person, "PERSON"),
                _seg("（", None),
                _seg(dob, "DATE_OF_BIRTH"),
                _seg("生）", None),
                _seg("は", None),
                _seg(org, "ORGANIZATION"),
                _seg("所属。次回確認日: ", None),
                _seg(status_dates[1], None),
            ]
        )
    return _compose(
        [
            _seg("診察日 ", None),
            _seg(status_dates[0], None),
            _seg("、患者 ", None),
            _seg(person, "PERSON"),
            _seg("、", None),
            _seg(dob, "DATE_OF_BIRTH"),
            _seg("生、予約日 ", None),
            _seg(status_dates[1], None),
            _seg("、住所 ", None),
            _seg(address, "ADDRESS"),
            _seg("、再確認日 ", None),
            _seg(status_dates[2], None),
        ]
    )


def _account_alias_junction_doc(rng: random.Random) -> dict:
    person = _random_person(rng)
    bank = _random_bank(rng)
    org = _random_org(rng)
    phone = rng.choice(_PHONE_LIKE_VALUES)
    order = rng.choice(_ORDER_LIKE_VALUES)
    pseudo = rng.choice(
        [
            "branch-main ordinary 0000000",
            "ACCOUNT=TEMP-HOLD",
            "bank-branch-kind-number",
            "routing-pending-0000",
        ]
    )
    pattern = rng.randrange(3)
    if pattern == 0:
        return _compose(
            [
                _seg("連絡先 ", None),
                _seg(phone, None),
                _seg(" / 注文番号 ", None),
                _seg(order, None),
                _seg(" / 振込先 ", None),
                _seg(bank, "BANK_ACCOUNT"),
                _seg(" / 担当 ", None),
                _seg(person, "PERSON"),
            ]
        )
    if pattern == 1:
        return _compose(
            [
                _seg("仮値 ", None),
                _seg(pseudo, None),
                _seg(" は無効、現在有効なのは ", None),
                _seg(bank, "BANK_ACCOUNT"),
                _seg("。所属 ", None),
                _seg(org, "ORGANIZATION"),
            ]
        )
    return _compose(
        [
            _seg("精算メモ: ref=", None),
            _seg(order, None),
            _seg(" / tel=", None),
            _seg(phone, None),
            _seg(" / 受取人 ", None),
            _seg(person, "PERSON"),
            _seg(" / 受取口座 ", None),
            _seg(bank, "BANK_ACCOUNT"),
        ]
    )


def _counterfactual_notice_doc(rng: random.Random) -> dict:
    person = _random_person(rng)
    address = _random_address(rng)
    org = _random_org(rng)
    dob = _random_dob(rng)
    bank = _random_bank(rng)
    pattern = rng.randrange(3)
    if pattern == 0:
        return _compose(
            [
                _seg(rng.choice(_UNTAGGED_DUMMY_VALUES[:2]), None),
                _seg("。ただし申請者は", None),
                _seg(person, "PERSON"),
                _seg("、送付先は", None),
                _seg(address, "ADDRESS"),
                _seg("。", None),
            ]
        )
    if pattern == 1:
        return _compose(
            [
                _seg("口座: 現金払い予定ではない。現在有効なのは ", None),
                _seg(bank, "BANK_ACCOUNT"),
                _seg("。担当は", None),
                _seg(person, "PERSON"),
                _seg("。", None),
            ]
        )
    return _compose(
        [
            _seg("生年月日: 19XX年XX月XX日 ではなく ", None),
            _seg(dob, "DATE_OF_BIRTH"),
            _seg("。所属は", None),
            _seg(org, "ORGANIZATION"),
            _seg("。", None),
        ]
    )


_JA_V010_GENERATORS: dict[str, DocGenerator] = {
    "placeholder_registry.j2": _placeholder_registry_doc,
    "label_stub_catalog.j2": _label_stub_catalog_doc,
    "fragment_chain.j2": _fragment_chain_doc,
    "schema_bleed.j2": _schema_bleed_doc,
    "orthography_shift.j2": _orthography_shift_doc,
    "geo_org_refraction.j2": _geo_org_refraction_doc,
    "date_switchyard.j2": _date_switchyard_doc,
    "account_alias_junction.j2": _account_alias_junction_doc,
    "counterfactual_notice.j2": _counterfactual_notice_doc,
}


def build_curated_benchmark(
    version: str,
    language: str,
    template_names: Sequence[str],
    template_weights: dict[str, float],
    docs_per_template: int,
    batches_per_template: int,
) -> list[dict]:
    """Build a deterministic benchmark without remote LLM calls."""
    if version != "v0.10.0" or language != "ja":
        raise ValueError(f"Unsupported curated benchmark target: {version}/{language}")

    missing = [name for name in template_names if name not in _JA_V010_GENERATORS]
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise ValueError(f"Missing curated generators for: {missing_names}")

    all_docs: list[dict] = []
    for template_name in template_names:
        generator = _JA_V010_GENERATORS[template_name]
        batches = max(1, int(batches_per_template * template_weights.get(template_name, 1.0)))
        target_docs = max(1, docs_per_template * batches)
        rng = random.Random(f"{version}:{language}:{template_name}")

        for doc_idx in range(target_docs):
            doc = generator(rng)
            doc["_meta"] = {"template": template_name, "doc_idx": doc_idx}
            all_docs.append(doc)

    random.Random(f"{version}:{language}:shuffle").shuffle(all_docs)
    return all_docs
