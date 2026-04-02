"""Curated benchmark corpus loader."""

from __future__ import annotations

from pathlib import Path

from pleno_ner_training.generate_data import (
    DOC_SEPARATOR,
    parse_annotated_text,
    validate_annotations,
)


def load_curated_benchmark_corpus(
    version: str,
    language: str,
    source_paths: list[Path],
) -> list[dict]:
    """手書き corpus を読み込み、raw.json 用の構造へ変換する."""
    all_docs: list[dict] = []

    for source_path in source_paths:
        raw_text = source_path.read_text(encoding="utf-8")
        doc_idx = 0

        for doc_text in raw_text.split(DOC_SEPARATOR):
            doc_text = doc_text.strip()
            if not doc_text:
                continue

            parsed = parse_annotated_text(doc_text)
            if not parsed["text"]:
                continue
            if parsed["entities"] and not validate_annotations(parsed):
                raise ValueError(f"Invalid annotation in {source_path} at doc {doc_idx}")

            parsed["_meta"] = {
                "template": source_path.name,
                "doc_idx": doc_idx,
                "version": version,
                "language": language,
            }
            all_docs.append(parsed)
            doc_idx += 1

    return all_docs
