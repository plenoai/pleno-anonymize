"""JSON アノテーションデータを HuggingFace Dataset 形式（BIOタグ付き）に変換する.

- ku-nlp/deberta-v2-base-japanese のtokenizerでトークン化 (SentencePiece, ブラウザ互換)
- 文字オフセットのエンティティをトークンレベルのBIOタグに変換
- train/dev/test 分割 (80/10/10, seed=42)
- Arrow形式で保存
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path

from datasets import Dataset, DatasetDict, ClassLabel, Features, Sequence, Value
from transformers import AutoTokenizer, PreTrainedTokenizerFast

from pleno_ner_training.entity_types import NER_LABELS

# Heuristic patterns to drop zero-entity docs that actually contain PII text
# (label-miss noise). Keeping these as O-only examples would teach the model
# to NOT tag real PII -- the opposite of what we want.
#
# History: the original (v0) set caught only ~16% of zero-entity docs, leaking
# ADDRESS / BANK_ACCOUNT / DATE_OF_BIRTH look-alikes into the hard-negative
# pool and causing the regression observed in #66 (hardneg run vs. baseline:
# ADDRESS F1 0.692 → 0.667, BANK_ACCOUNT F1 0.615 → 0.558). The expanded set
# below (#67) lifts the catch rate to ~25% on `ja-v02/generated.json` by
# adding postal codes (〒), Japanese phone numbers, email, 16-digit card-like
# blocks, IBAN, 口座番号 / 普通 / 当座, bank+digits, the remaining 41
# prefectures, [市区町村]+丁目, ISO-style YYYY年M月D日, 名義+name and the
# residual XML/《》-tagged PII spans that leaked from prompt templates.
PII_HINT_RE = re.compile(
    "|".join(
        [
            # Generic digit clusters (legacy, kept for back-compat).
            r"\d{1,2}-\d{1,4}-\d{1,4}",
            r"\d{4,}\s*-?\s*\d{4,}",
            # Honorific-suffixed personal names.
            r"[一-龥]{2,4}\s*[一-龥]{1,3}\s*(さん|様|氏)",
            # Corporate entities.
            r"(株式会社|有限会社|合同会社)",
            # Era-formatted dates (legacy, narrow set).
            r"(平成|令和|昭和)\s*[一-龥0-9]+\s*年",
            # --- #67: address-bearing phrases ---------------------------------
            # Postal code 〒XXX-XXXX (with optional space / no hyphen).
            r"〒\s*\d{3}-?\d{4}",
            # All 47 prefectures (the original list only had 6).
            r"(東京都|大阪府|京都府|北海道|沖縄県"
            r"|神奈川県|埼玉県|千葉県|愛知県|兵庫県|福岡県|静岡県|広島県"
            r"|宮城県|新潟県|岡山県|長野県|岐阜県|茨城県|栃木県|群馬県"
            r"|三重県|奈良県|滋賀県|和歌山県|福井県|石川県|富山県|山梨県"
            r"|岩手県|青森県|秋田県|山形県|福島県|鳥取県|島根県|山口県"
            r"|徳島県|香川県|愛媛県|高知県|佐賀県|長崎県|熊本県|大分県"
            r"|宮崎県|鹿児島県"
            r"|千代田区|港区|渋谷区)",
            # 市/区/町/村 + 丁目 (chome) — strong address signal.
            r"[一-龥]{1,4}(市|区|町|村)[一-龥]{1,8}\d+丁目",
            # 1-2-3 / 1-2-3-4 banchi-go style.
            r"\d+\s*[-－]\s*\d+\s*[-－]\s*\d+",
            # --- #67: contact identifiers -------------------------------------
            # Japanese landline / mobile phone (0X-XXXX-XXXX, 0XXX-XX-XXXX, …).
            r"0\d{1,4}-\d{1,4}-\d{4}",
            # Email address.
            r"[\w.+-]+@[\w-]+\.[\w.-]+",
            # --- #67: financial identifiers -----------------------------------
            # 16-digit credit-card / IBAN-ish 4-block group.
            r"\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}",
            # IBAN (ISO 13616).
            r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}",
            # 口座番号 / 普通 / 当座 + digits.
            r"(口座番号|普通|当座)\s*[:：]?\s*\d{3,8}",
            # Bank/branch keyword followed by a 4+ digit run within 30 chars.
            r"(銀行|信金|信用金庫|信用組合|労金|農協|ゆうちょ)[^\n]{0,30}\d{4,}",
            # --- #67: birthdate / personal -----------------------------------
            # ISO-style YYYY年M月D日.
            r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日",
            # 漢字氏名 + (カナ読み) — the name+furigana pattern in dialogues.
            r"[一-龥]{1,4}\s*[一-龥]{1,4}[（(][ァ-ヶー\s]{2,15}[)）]",
            # 名義 + 漢字/カナ name.
            r"名義[はがでも:：\s]+[一-龥ァ-ヶー]{2,8}",
            # --- SPECIAL_CARE: medical / disability keywords --------------------
            r"(病歴|既往歴|診断名|主訴|現病歴|治療歴|手術歴)",
            r"(障害者手帳|身体障害|知的障害|精神障害|要介護|障害等級)",
            r"(健康診断|健診結果|人間ドック|検査結果|要精密検査|要再検査)",
            # --- SPECIAL_CARE: criminal keywords --------------------------------
            r"(前科|前歴|犯罪歴|逮捕歴|起訴|有罪|懲役|禁錮|執行猶予|少年院)",
            r"(被害届|被害に遭|DV被害|ストーカー被害|性犯罪被害)",
            # --- SPECIAL_CARE: race / creed / social status ---------------------
            r"(在日|系日本人|民族|人種|部落出身)",
            r"(信仰|信条|宗教[はをがで]|教徒|信者)",
            # --- #67: residual PII XML/《》 prompt-template artefacts ---------
            r"<(" + "|".join(NER_LABELS) + r")\b",
            r"《(" + "|".join(NER_LABELS) + r")>",
        ]
    )
)

ENTITY_LABELS = sorted(NER_LABELS)
BIO_LABELS = ["O"] + [
    f"{prefix}-{label}" for label in ENTITY_LABELS for prefix in ("B", "I")
]
LABEL2ID = {label: i for i, label in enumerate(BIO_LABELS)}
IGNORE_INDEX = -100


def align_labels_with_tokens(
    text: str,
    entities: list[dict],
    tokenizer: PreTrainedTokenizerFast,
    max_length: int = 512,
) -> dict | None:
    """テキストとエンティティをトークン化し、BIOタグにアライメントする.

    Returns:
        {"input_ids": [...], "attention_mask": [...], "labels": [...]} or None
    """
    encoding = tokenizer(
        text,
        max_length=max_length,
        truncation=True,
        padding=False,
        return_offsets_mapping=True,
    )
    offsets = encoding["offset_mapping"]
    input_ids = encoding["input_ids"]
    attention_mask = encoding["attention_mask"]

    # 文字位置 → エンティティラベルのマッピングを構築
    char_to_entity: dict[int, tuple[str, bool]] = {}
    for ent in entities:
        start, end, label = ent["start"], ent["end"], ent["label"]
        if label not in ENTITY_LABELS:
            continue
        for i in range(start, end):
            is_start = i == start
            char_to_entity[i] = (label, is_start)

    labels = []
    prev_label: str | None = None

    for idx, (tok_start, tok_end) in enumerate(offsets):
        # 特殊トークン → ignore
        if tok_start == 0 and tok_end == 0:
            labels.append(IGNORE_INDEX)
            prev_label = None
            continue

        # トークンがカバーする文字範囲のエンティティを判定
        # 最初の非空白文字のエンティティで決定
        token_label = None
        token_is_entity_start = False
        for char_idx in range(tok_start, tok_end):
            if char_idx in char_to_entity:
                entity_label, is_start = char_to_entity[char_idx]
                token_label = entity_label
                token_is_entity_start = is_start
                break

        if token_label is None:
            labels.append(LABEL2ID["O"])
            prev_label = None
        else:
            # B- if this is the start of entity OR the label changed from previous
            if token_is_entity_start or token_label != prev_label:
                labels.append(LABEL2ID[f"B-{token_label}"])
            else:
                labels.append(LABEL2ID[f"I-{token_label}"])
            prev_label = token_label

    if len(labels) != len(input_ids):
        return None

    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "labels": labels,
    }


def load_and_convert(
    input_path: Path,
    tokenizer: PreTrainedTokenizerFast,
    max_length: int = 512,
    include_negatives: bool = False,
) -> list[dict]:
    """JSONファイルを読み込み、トークン化してBIOタグ付きデータに変換する.

    include_negatives=True で、entities=[] の文書も全 O ラベルで取り込む
    (over-prediction を抑える hard-negative 訓練). PII らしき文字列を含む
    label-miss 候補はヒューリスティックで除外する.
    """
    with open(input_path, encoding="utf-8") as f:
        raw_data = json.load(f)

    converted = []
    skipped = 0
    negatives_kept = 0
    negatives_dropped = 0

    for item in raw_data:
        text = item.get("text", "")
        entities = item.get("entities", [])

        if not entities:
            if not include_negatives:
                skipped += 1
                continue
            if not text or PII_HINT_RE.search(text):
                negatives_dropped += 1
                continue
            negatives_kept += 1

        result = align_labels_with_tokens(text, entities, tokenizer, max_length)
        if result is not None:
            converted.append(result)
        else:
            skipped += 1

    if include_negatives:
        print(
            f"Converted: {len(converted)}, Skipped: {skipped}, "
            f"Negatives kept: {negatives_kept}, dropped (PII-suspicious): {negatives_dropped}"
        )
    else:
        print(f"Converted: {len(converted)}, Skipped: {skipped}")
    return converted


def split_data(
    data: list[dict],
    train_ratio: float = 0.8,
    dev_ratio: float = 0.1,
    seed: int = 42,
) -> tuple[list[dict], list[dict], list[dict]]:
    """train/dev/test に分割する."""
    random.seed(seed)
    shuffled = list(data)
    random.shuffle(shuffled)

    n = len(shuffled)
    train_end = int(n * train_ratio)
    dev_end = int(n * (train_ratio + dev_ratio))

    return shuffled[:train_end], shuffled[train_end:dev_end], shuffled[dev_end:]


def create_dataset(data: list[dict]) -> Dataset:
    """リストからHuggingFace Datasetを作成する."""
    if not data:
        return Dataset.from_dict(
            {"input_ids": [], "attention_mask": [], "labels": []}
        )

    features = Features(
        {
            "input_ids": Sequence(Value("int32")),
            "attention_mask": Sequence(Value("int8")),
            "labels": Sequence(
                ClassLabel(names=BIO_LABELS),
            ),
        }
    )

    return Dataset.from_dict(
        {
            "input_ids": [d["input_ids"] for d in data],
            "attention_mask": [d["attention_mask"] for d in data],
            "labels": [d["labels"] for d in data],
        },
        features=features,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="JSON → HuggingFace Dataset (BIOタグ) 変換"
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="入力JSONファイル",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="出力ディレクトリ (Arrow形式)",
    )
    parser.add_argument(
        "--tokenizer",
        type=str,
        default="ku-nlp/deberta-v2-base-japanese",
        help="HuggingFace tokenizer名",
    )
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--include-negatives",
        action="store_true",
        help="entities=[] doc を all-O 例として取り込み、過剰予測を抑える",
    )
    args = parser.parse_args()

    print(f"Loading tokenizer: {args.tokenizer}")
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)

    print(f"Loading and converting data from {args.input}...")
    data = load_and_convert(
        args.input,
        tokenizer,
        args.max_length,
        include_negatives=args.include_negatives,
    )

    if not data:
        print("ERROR: No data was converted. Check input file.")
        raise SystemExit(1)

    train, dev, test = split_data(data, seed=args.seed)
    print(f"Split: train={len(train)}, dev={len(dev)}, test={len(test)}")

    dataset_dict = DatasetDict(
        {
            "train": create_dataset(train),
            "validation": create_dataset(dev),
            "test": create_dataset(test),
        }
    )

    args.output.mkdir(parents=True, exist_ok=True)
    dataset_dict.save_to_disk(str(args.output))
    print(f"Saved to {args.output}")

    # ラベル情報も保存 (学習時に参照)
    label_info_path = args.output / "label_info.json"
    with open(label_info_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "labels": BIO_LABELS,
                "label2id": LABEL2ID,
                "id2label": {v: k for k, v in LABEL2ID.items()},
                "num_labels": len(BIO_LABELS),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"Label info saved to {label_info_path}")


if __name__ == "__main__":
    main()
