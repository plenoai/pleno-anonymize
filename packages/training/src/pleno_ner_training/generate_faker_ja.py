"""License-clean JA NER training data via Faker (ja_JP locale).

Follows the existing JA annotation conventions (see data/raw/ja-v02 and
benchmark v0.4.0): full personal names, full postal addresses and full
organization names are each ONE span — unlike the EN generator, which uses
component granularity to match ai4privacy's span style.

All strings come from Faker plus hand-written templates; nothing derives
from third-party datasets, so trained models ship under Apache-2.0.
"""

import argparse
import json
import random

from faker import Faker


class DocBuilder:
    def __init__(self):
        self.parts: list[str] = []
        self.entities: list[dict] = []
        self.length = 0

    def text(self, s: str):
        self.parts.append(s)
        self.length += len(s)

    def ent(self, s: str, label: str):
        self.entities.append(
            {"start": self.length, "end": self.length + len(s), "label": label}
        )
        self.text(s)

    def build(self) -> dict:
        return {"text": "".join(self.parts), "entities": self.entities}


ERAS = [("昭和", 1926), ("平成", 1989), ("令和", 2019)]


def ja_dob(f: Faker) -> str:
    d = f.date_of_birth(minimum_age=18, maximum_age=90)
    style = random.random()
    if style < 0.45:
        return f"{d.year}年{d.month}月{d.day}日"
    if style < 0.65:
        return d.strftime("%Y/%m/%d")
    if style < 0.75:
        return d.strftime("%Y-%m-%d")
    for era, start in reversed(ERAS):
        if d.year >= start:
            y = d.year - start + 1
            return f"{era}{'元' if y == 1 else y}年{d.month}月{d.day}日"
    return f"{d.year}年{d.month}月{d.day}日"


def ja_address(f: Faker) -> str:
    addr = f.address().replace("\n", "")
    if random.random() < 0.35:
        addr = f"〒{f.postcode()} {addr}"
    return addr


def ja_bank(f: Faker) -> str:
    n = f"{random.randint(0, 9999999):07d}"
    return random.choice(
        [
            f"口座番号 {n}",
            f"普通 {n}",
            f"普通口座{n}",
            n,
            f"{random.randint(100,999)}-{n}",
        ]
    )


def person(f: Faker) -> str:
    style = random.random()
    if style < 0.55:
        return f.name()
    if style < 0.7:
        return f.name().replace(" ", "")
    if style < 0.85:
        return f.last_name()
    return f.romanized_name()


def tpl_form(f: Faker) -> dict:
    b = DocBuilder()
    b.text(random.choice(["【会員情報】", "■ お客様情報", "――― 応募者情報 ―――", "登録内容の確認"]) + "\n")
    b.text("氏名: ")
    b.ent(person(f), "PERSON")
    if random.random() < 0.6:
        b.text("\n生年月日: ")
        b.ent(ja_dob(f), "DATE_OF_BIRTH")
    b.text("\n住所: ")
    b.ent(ja_address(f), "ADDRESS")
    if random.random() < 0.5:
        b.text("\n勤務先: ")
        b.ent(f.company(), "ORGANIZATION")
    if random.random() < 0.4:
        b.text("\n振込先: ")
        b.ent(f.company(), "ORGANIZATION")
        b.text(" ")
        b.ent(ja_bank(f), "BANK_ACCOUNT")
    b.text("\n")
    return b.build()


def tpl_email(f: Faker) -> dict:
    b = DocBuilder()
    b.text(random.choice(["件名: 手続き完了のお知らせ\n\n", "件名: ご登録内容の確認\n\n", "件名: 面談日程のご案内\n\n"]))
    b.ent(person(f), "PERSON")
    b.text(" 様\n\nいつもお世話になっております。")
    b.ent(f.company(), "ORGANIZATION")
    b.text("の")
    b.ent(person(f), "PERSON")
    b.text("です。\n")
    b.text(random.choice(["ご登録の住所(", "お届け先(", "現住所("]))
    b.ent(ja_address(f), "ADDRESS")
    b.text(")宛に書類をお送りいたします。\n")
    if random.random() < 0.5:
        b.text("ご本人確認のため、生年月日(")
        b.ent(ja_dob(f), "DATE_OF_BIRTH")
        b.text(")の記載をお願いいたします。\n")
    if random.random() < 0.3:
        b.text("返金は ")
        b.ent(ja_bank(f), "BANK_ACCOUNT")
        b.text(" へお振込みいたします。\n")
    b.text("\nよろしくお願いいたします。\n")
    return b.build()


def tpl_narrative(f: Faker) -> dict:
    b = DocBuilder()
    b.ent(person(f), "PERSON")
    b.text(random.choice(["さんは", "氏は", "は"]))
    b.ent(ja_dob(f), "DATE_OF_BIRTH")
    b.text("生まれ。現在は")
    b.ent(ja_address(f), "ADDRESS")
    b.text("に在住し、")
    b.ent(f.company(), "ORGANIZATION")
    b.text(random.choice(["に勤務している。", "で働いている。", "に所属。"]))
    if random.random() < 0.5:
        b.text("同僚の")
        b.ent(person(f), "PERSON")
        b.text(random.choice(["さんとは同期入社である。", "氏と共同で案件を担当。"]))
    return b.build()


def tpl_ticket(f: Faker) -> dict:
    b = DocBuilder()
    b.text(f"問い合わせ #{random.randint(1000, 99999)}\n")
    b.text("お客様: ")
    b.ent(person(f), "PERSON")
    b.text("\n配送先: ")
    b.ent(ja_address(f), "ADDRESS")
    b.text("\n内容: " + random.choice([
        "商品が届かないとのお問い合わせ。",
        "返品と返金のご依頼。",
        "住所変更の手続き依頼。",
        "請求金額に関する確認。",
    ]) + "\n")
    if random.random() < 0.4:
        b.text("返金先: ")
        b.ent(ja_bank(f), "BANK_ACCOUNT")
        b.text("\n")
    b.text("担当: ")
    b.ent(person(f), "PERSON")
    b.text("\n")
    return b.build()


def tpl_negative(f: Faker) -> dict:
    b = DocBuilder()
    b.text(random.choice([
        "四半期レポートでは全部門で堅調な進捗が報告された。",
        "週末にサーバーメンテナンスを予定しており、一時的にサービスが利用できない場合があります。",
        "新しいカリキュラムは長い審議の末に承認された。",
        "提出手順については添付のガイドラインをご参照ください。",
        "本日の会議は資料の共有のみで終了しました。次回は進捗確認を行います。",
        "こちらのプランには月額料金のほか初期費用がかかります。詳細は料金表をご覧ください。",
    ]))
    return b.build()


TEMPLATES = [
    (tpl_form, 0.28),
    (tpl_email, 0.25),
    (tpl_narrative, 0.22),
    (tpl_ticket, 0.15),
    (tpl_negative, 0.10),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    random.seed(args.seed)
    Faker.seed(args.seed)
    f = Faker("ja_JP")

    fns = [t for t, _ in TEMPLATES]
    weights = [w for _, w in TEMPLATES]
    docs = [random.choices(fns, weights=weights, k=1)[0](f) for _ in range(args.count)]

    n_ents = sum(len(d["entities"]) for d in docs)
    with open(args.output, "w", encoding="utf-8") as fh:
        json.dump(docs, fh, ensure_ascii=False)
    print(f"wrote {len(docs)} docs, {n_ents} entities to {args.output}")


if __name__ == "__main__":
    main()
