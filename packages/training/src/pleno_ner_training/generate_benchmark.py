"""ベンチマークデータ生成パイプライン（マルチバージョン対応）.

学習テンプレートとは完全に異なるドメインのテンプレートから
評価専用データを生成し、DocBin形式に変換する。

使い方:
    python -m pleno_ner_training.generate_benchmark --version v0.2.0 --language ja
    python -m pleno_ner_training.generate_benchmark --version v0.3.0 --language en
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from tqdm import tqdm

from pleno_ner_training.benchmark_config import (
    BENCHMARK_CONFIGS,
    LATEST_BENCHMARK_VERSION,
    resolve_benchmark_template_paths,
)
from pleno_ner_training.convert_to_docbin import convert_to_docs, save_docbin
from pleno_ner_training.entity_types import LANG_CONFIGS
from pleno_ner_training.generate_data import (
    DOC_SEPARATOR,
    parse_annotated_text,
    validate_annotations,
)


def generate_benchmark_batch(
    client,
    template_name: str,
    num_docs: int,
    language: str,
    prompts_dir: Path,
    model: str = "gpt-5.4-nano",
    max_retries: int = 3,
    batch_idx: int = 0,
) -> list[dict]:
    """ベンチマーク用バッチ生成。エンティティ0件のドキュメントも許可する."""
    lang_config = LANG_CONFIGS[language]
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

        if parsed["entities"] and not validate_annotations(parsed):
            continue

        parsed["_meta"] = {
            "template": template_name,
            "batch_idx": batch_idx,
        }
        documents.append(parsed)

    return documents


def _generate_benchmark_via_openai(
    config,
    language: str,
    docs_per_template: int,
    batches_per_template: int,
    model: str,
    max_workers: int,
    template_names: list[str],
    prompts_dir: Path,
) -> tuple[list[dict], int]:
    from openai import OpenAI

    client = OpenAI()
    all_docs: list[dict] = []
    failed = 0

    tasks = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for template_name in template_names:
            weight = config.template_weights.get(template_name, 1.0)
            n_batches = int(batches_per_template * weight)
            for batch_idx in range(n_batches):
                future = executor.submit(
                    generate_benchmark_batch,
                    client,
                    template_name,
                    docs_per_template,
                    language,
                    prompts_dir,
                    model,
                    batch_idx=batch_idx,
                )
                tasks.append((future, template_name, batch_idx))

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

    return all_docs, failed


def generate_benchmark(
    version: str = LATEST_BENCHMARK_VERSION,
    language: str = "ja",
    docs_per_template: int = 20,
    batches_per_template: int = 10,
    model: str = "gpt-5.4-nano",
    max_workers: int = 5,
    output_dir: Path | None = None,
) -> Path:
    """ベンチマークデータを生成してDocBin形式で保存する."""
    import spacy

    config = BENCHMARK_CONFIGS[version]
    data_root = Path(__file__).parents[2] / "data"
    output_dir = output_dir or (data_root / "benchmark" / version / language)
    raw_path = output_dir / "raw.json"
    docbin_path = output_dir / "test.spacy"

    output_dir.mkdir(parents=True, exist_ok=True)

    templates = resolve_benchmark_template_paths(config, language)
    if not templates:
        prompts_dir = Path(__file__).parent / "prompts" / config.prompts_subdir / language
        print(f"[ERROR] No templates in {prompts_dir}", file=sys.stderr)
        raise SystemExit(1)
    template_names = [template_path.name for template_path in templates]

    print(f"=== Benchmark {version} ({language}) ===")
    print(f"Templates: {template_names}")
    print(f"Config: {docs_per_template} docs/template x {batches_per_template} batches x {len(templates)} templates")
    print(f"Backend: {config.generation_backend}")

    if raw_path.exists():
        with open(raw_path, encoding="utf-8") as f:
            existing = json.load(f)
        print(f"Existing data found: {len(existing)} docs. Regenerating...")

    if config.generation_backend == "curated":
        from pleno_ner_training.curated_benchmark import build_curated_benchmark

        all_docs = build_curated_benchmark(
            version=version,
            language=language,
            template_names=template_names,
            template_weights=config.template_weights,
            docs_per_template=docs_per_template,
            batches_per_template=batches_per_template,
        )
        failed = 0
    else:
        prompts_dir = Path(__file__).parent / "prompts" / config.prompts_subdir / language
        all_docs, failed = _generate_benchmark_via_openai(
            config=config,
            language=language,
            docs_per_template=docs_per_template,
            batches_per_template=batches_per_template,
            model=model,
            max_workers=max_workers,
            template_names=template_names,
            prompts_dir=prompts_dir,
        )

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(all_docs, f, ensure_ascii=False, indent=2)

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

    parser = argparse.ArgumentParser(description="Benchmark data generation")
    parser.add_argument("--version", default=LATEST_BENCHMARK_VERSION, choices=list(BENCHMARK_CONFIGS))
    parser.add_argument("--language", default="ja", choices=["ja", "en"])
    parser.add_argument("--docs-per-template", type=int, default=20)
    parser.add_argument("--batches-per-template", type=int, default=10)
    parser.add_argument("--model", default="gpt-5.4-nano")
    parser.add_argument("--max-workers", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    generate_benchmark(
        version=args.version,
        language=args.language,
        docs_per_template=args.docs_per_template,
        batches_per_template=args.batches_per_template,
        model=args.model,
        max_workers=args.max_workers,
        output_dir=args.output_dir,
    )


if __name__ == "__main__":
    main()
