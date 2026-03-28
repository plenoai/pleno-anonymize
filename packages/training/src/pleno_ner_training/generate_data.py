"""GPT-5.4-miniを用いた日本語PII合成データ生成パイプライン.

XMLタグ付きの日本語テキストを生成し、文字オフセットベースの
アノテーションJSONに変換する。
"""

import json
import re
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

from jinja2 import Environment, FileSystemLoader
from openai import OpenAI
from tqdm import tqdm

from pleno_ner_training.entity_types import NER_LABELS

PROMPTS_DIR = Path(__file__).parent / "prompts"
DOC_SEPARATOR = "---DOC_SEPARATOR---"

# XMLタグのパターン: <LABEL>text</LABEL>
TAG_PATTERN = re.compile(
    r"<(" + "|".join(NER_LABELS) + r")>(.*?)</\1>",
    re.DOTALL,
)


def parse_annotated_text(text: str) -> dict:
    """XMLタグ付きテキストを plain text + entity offsets に変換する.

    Returns:
        {"text": "plain text...", "entities": [{"start": 0, "end": 5, "label": "PERSON"}, ...]}
    """
    entities: list[dict] = []
    plain_parts: list[str] = []
    last_end = 0
    offset = 0

    for m in TAG_PATTERN.finditer(text):
        # タグの前のテキストを追加
        before = text[last_end : m.start()]
        plain_parts.append(before)
        offset += len(before)

        label = m.group(1)
        entity_text = m.group(2)

        entities.append(
            {
                "start": offset,
                "end": offset + len(entity_text),
                "label": label,
                "text": entity_text,
            }
        )

        plain_parts.append(entity_text)
        offset += len(entity_text)
        last_end = m.end()

    # 残りのテキスト
    plain_parts.append(text[last_end:])
    plain_text = "".join(plain_parts)

    return {"text": plain_text, "entities": entities}


def validate_annotations(doc: dict) -> bool:
    """アノテーションの整合性を検証する."""
    text = doc["text"]
    for ent in doc["entities"]:
        extracted = text[ent["start"] : ent["end"]]
        if extracted != ent["text"]:
            return False
        if ent["label"] not in NER_LABELS:
            return False
    # 重複チェック
    spans = [(e["start"], e["end"]) for e in doc["entities"]]
    for i, (s1, e1) in enumerate(spans):
        for s2, e2 in spans[i + 1 :]:
            if s1 < e2 and s2 < e1:
                return False
    return True


def generate_batch(
    client: OpenAI,
    template_name: str,
    num_docs: int,
    model: str = "gpt-5.4-mini",
    max_retries: int = 3,
) -> list[dict]:
    """1つのテンプレートからバッチ生成する."""
    import time

    env = Environment(loader=FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template(template_name)
    prompt = template.render(num_docs=num_docs)

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "あなたは日本語のPII（個人情報）を含むリアルなテキストを生成する専門家です。"
                            "指定されたXMLタグ形式で正確にPIIエンティティをマークアップしてください。"
                            "タグは必ず正しく閉じ、ネストしないでください。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
                max_completion_tokens=16000,
            )
            break
        except Exception as e:
            if attempt < max_retries - 1 and ("401" in str(e) or "429" in str(e)):
                time.sleep(2 ** attempt)
                continue
            raise

    raw_text = response.choices[0].message.content or ""
    documents = []

    for doc_text in raw_text.split(DOC_SEPARATOR):
        doc_text = doc_text.strip()
        if not doc_text:
            continue

        parsed = parse_annotated_text(doc_text)
        if parsed["text"] and parsed["entities"] and validate_annotations(parsed):
            documents.append(parsed)

    return documents


def _load_existing(path: Path) -> list[dict]:
    """既存の生成済みデータを読み込む."""
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_incremental(path: Path, all_docs: list[dict]) -> None:
    """データをインクリメンタルに保存する."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(all_docs, f, ensure_ascii=False, indent=2)


def generate_dataset(
    output_dir: Path,
    docs_per_template: int = 20,
    batches_per_template: int = 50,
    model: str = "gpt-5.4-mini",
    max_workers: int = 5,
) -> None:
    """全テンプレートからデータセットを生成する."""
    client = OpenAI()
    output_dir.mkdir(parents=True, exist_ok=True)

    output_path = output_dir / "generated.json"
    all_docs = _load_existing(output_path)
    initial_count = len(all_docs)
    if initial_count > 0:
        print(f"Resuming: {initial_count} existing documents loaded")

    templates = list(PROMPTS_DIR.glob("*.j2"))
    failed = 0
    save_interval = 5  # 5バッチごとに保存
    batch_count = 0

    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for template_path in templates:
            template_name = template_path.name
            for batch_idx in range(batches_per_template):
                future = executor.submit(
                    generate_batch,
                    client,
                    template_name,
                    docs_per_template,
                    model,
                )
                tasks.append((future, template_name, batch_idx))

        for future, template_name, batch_idx in tqdm(
            [(f, t, b) for f, t, b in tasks],
            desc="Generating",
            total=len(tasks),
        ):
            try:
                docs = future.result(timeout=120)
                all_docs.extend(docs)
                batch_count += 1
                if batch_count % save_interval == 0:
                    _save_incremental(output_path, all_docs)
            except Exception as e:
                failed += 1
                print(
                    f"[WARN] {template_name} batch {batch_idx} failed: {e}",
                    file=sys.stderr,
                )

    # 最終保存
    _save_incremental(output_path, all_docs)

    # 統計
    entity_counts: dict[str, int] = {}
    for doc in all_docs:
        for ent in doc["entities"]:
            entity_counts[ent["label"]] = entity_counts.get(ent["label"], 0) + 1

    new_docs = len(all_docs) - initial_count
    print(f"\n=== Generation Summary ===")
    print(f"Total documents: {len(all_docs)} (new: {new_docs})")
    print(f"Failed batches: {failed}")
    print(f"Entity counts:")
    for label, count in sorted(entity_counts.items()):
        print(f"  {label}: {count}")
    print(f"Output: {output_path}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="日本語PII合成データ生成")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parents[2] / "data" / "raw",
    )
    parser.add_argument("--docs-per-template", type=int, default=20)
    parser.add_argument("--batches-per-template", type=int, default=50)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--max-workers", type=int, default=5)
    args = parser.parse_args()

    generate_dataset(
        output_dir=args.output_dir,
        docs_per_template=args.docs_per_template,
        batches_per_template=args.batches_per_template,
        model=args.model,
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    main()
