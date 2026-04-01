"""ベンチマーク v0.2.0 データ生成パイプライン.

学習テンプレートとは完全に異なるドメインのテンプレートから
評価専用データを生成し、DocBin形式に変換する。

使い方:
    python -m pleno_ner_training.generate_benchmark --language ja
    python -m pleno_ner_training.generate_benchmark --language en
"""

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from tqdm import tqdm

from pleno_ner_training.convert_to_docbin import convert_to_docs, save_docbin
from pleno_ner_training.entity_types import LANG_CONFIGS, NER_LABELS
from pleno_ner_training.generate_data import (
    DOC_SEPARATOR,
    parse_annotated_text,
    validate_annotations,
)

BENCHMARK_PROMPTS_DIR = Path(__file__).parent / "prompts" / "benchmark_v02"
BENCHMARK_VERSION = "v0.2.0"

# 高難度テンプレートはバッチ数を増やす（実環境の分布を反映）
# 実環境では PII を含まないドキュメントが 60-80% を占める
TEMPLATE_WEIGHT: dict[str, float] = {
    "negative_only.j2": 6.0,  # 実環境ではPII無しドキュメントが大半
    "distractor_heavy.j2": 2.5,  # 偽陽性の主要ソース（PII 1-2個 + 紛らわしい表現多数）
    "narrative_embedded.j2": 2.0,  # 学習データと最も異なる文体
    "mixed_language.j2": 1.5,  # 日英混在は実環境で頻出
}

TAG_PATTERN = re.compile(
    r"<(" + "|".join(NER_LABELS) + r")>(.*?)</\1>",
    re.DOTALL,
)


def generate_benchmark_batch(
    client,
    template_name: str,
    num_docs: int,
    language: str = "ja",
    model: str = "gpt-5.4-nano",
    max_retries: int = 3,
    batch_idx: int = 0,
) -> list[dict]:
    """ベンチマーク用バッチ生成。エンティティ0件のドキュメントも許可する."""
    lang_config = LANG_CONFIGS[language]
    prompts_dir = BENCHMARK_PROMPTS_DIR / language
    env = Environment(loader=FileSystemLoader(str(prompts_dir)))
    template = env.get_template(template_name)
    prompt = template.render(num_docs=num_docs)

    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": lang_config.system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.9,
                max_completion_tokens=16000,
            )
            break
        except Exception as e:
            if attempt < max_retries - 1 and ("401" in str(e) or "429" in str(e)):
                time.sleep(2**attempt)
                continue
            raise

    raw_text = response.choices[0].message.content or ""
    documents = []

    for doc_text in raw_text.split(DOC_SEPARATOR):
        doc_text = doc_text.strip()
        if not doc_text:
            continue

        parsed = parse_annotated_text(doc_text)
        if not parsed["text"]:
            continue

        # エンティティがある場合はバリデーション、ない場合もOK（陰性データ）
        if parsed["entities"] and not validate_annotations(parsed):
            continue

        parsed["_meta"] = {
            "template": template_name,
            "batch_idx": batch_idx,
        }
        documents.append(parsed)

    return documents


def generate_benchmark(
    language: str = "ja",
    docs_per_template: int = 20,
    batches_per_template: int = 10,
    model: str = "gpt-5.4-nano",
    max_workers: int = 5,
    output_dir: Path | None = None,
) -> Path:
    """ベンチマークデータを生成してDocBin形式で保存する."""
    from openai import OpenAI

    import spacy

    data_root = Path(__file__).parents[2] / "data"
    output_dir = output_dir or (data_root / "benchmark" / BENCHMARK_VERSION / language)
    raw_path = output_dir / "raw.json"
    docbin_path = output_dir / "test.spacy"

    output_dir.mkdir(parents=True, exist_ok=True)

    prompts_dir = BENCHMARK_PROMPTS_DIR / language
    templates = sorted(prompts_dir.glob("*.j2"))
    if not templates:
        print(f"[ERROR] No templates in {prompts_dir}", file=sys.stderr)
        raise SystemExit(1)

    print(f"=== Benchmark {BENCHMARK_VERSION} ({language}) ===")
    print(f"Templates: {[t.name for t in templates]}")
    print(f"Config: {docs_per_template} docs/template x {batches_per_template} batches x {len(templates)} templates")

    if raw_path.exists():
        with open(raw_path, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"Existing data found: {len(existing)} docs. Regenerating...")

    client = OpenAI()
    all_docs: list[dict] = []
    failed = 0

    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for template_path in templates:
            weight = TEMPLATE_WEIGHT.get(template_path.name, 1.0)
            n_batches = int(batches_per_template * weight)
            for batch_idx in range(n_batches):
                future = executor.submit(
                    generate_benchmark_batch,
                    client,
                    template_path.name,
                    docs_per_template,
                    language,
                    model,
                    batch_idx=batch_idx,
                )
                tasks.append((future, template_path.name, batch_idx))

        for future, template_name, batch_idx in tqdm(
            tasks,
            desc=f"Generating benchmark ({language})",
            total=len(tasks),
        ):
            try:
                docs = future.result(timeout=120)
                all_docs.extend(docs)
            except Exception as e:
                failed += 1
                print(f"[WARN] {template_name} batch {batch_idx}: {e}", file=sys.stderr)

    # 保存
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(all_docs, f, ensure_ascii=False, indent=2)

    # 統計
    entity_counts: dict[str, int] = {}
    neg_docs = 0
    for doc in all_docs:
        if not doc["entities"]:
            neg_docs += 1
        for ent in doc["entities"]:
            entity_counts[ent["label"]] = entity_counts.get(ent["label"], 0) + 1

    print(f"\n=== Generation Summary ===")
    print(f"Total documents: {len(all_docs)} (negative: {neg_docs})")
    print(f"Failed batches: {failed}")
    print(f"Entity counts:")
    for label, count in sorted(entity_counts.items()):
        print(f"  {label}: {count}")

    # DocBin変換
    print(f"\nConverting to DocBin...")
    nlp = spacy.blank(language)
    docs, total_ents, align_fail = convert_to_docs(nlp, all_docs)
    save_docbin(docs, docbin_path)
    print(f"Saved {len(docs)} docs to {docbin_path}")
    if total_ents > 0:
        print(f"Alignment failures: {align_fail}/{total_ents} ({align_fail / total_ents * 100:.1f}%)")

    return docbin_path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=f"Benchmark {BENCHMARK_VERSION} data generation")
    parser.add_argument("--language", default="ja", choices=["ja", "en"])
    parser.add_argument("--docs-per-template", type=int, default=20)
    parser.add_argument("--batches-per-template", type=int, default=10)
    parser.add_argument("--model", default="gpt-5.4-nano")
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    generate_benchmark(
        language=args.language,
        docs_per_template=args.docs_per_template,
        batches_per_template=args.batches_per_template,
        model=args.model,
        max_workers=args.max_workers,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
