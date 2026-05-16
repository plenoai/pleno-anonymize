"""Unit tests for Simula 5/8 — generation pipeline (#152).

LLM call is mocked; the pipeline plumbing (parsing, complexification,
critics, split) must work without network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pleno_ner_training.mechanism.generate import (
    entity_histogram,
    parse_xml_tagged,
    split_dataset,
)


def test_parse_xml_tagged_extracts_spans_and_offsets():
    text = "お世話になっております。<PERSON>山田太郎</PERSON>さん、<PHONE_NUMBER>03-1234-5678</PHONE_NUMBER>に連絡。"
    s = parse_xml_tagged(text)
    expected_text = "お世話になっております。山田太郎さん、03-1234-5678に連絡。"
    assert s.text == expected_text
    assert len(s.entities) == 2
    assert s.entities[0].label == "PERSON"
    assert s.text[s.entities[0].start : s.entities[0].end] == "山田太郎"
    assert s.entities[1].label == "PHONE_NUMBER"
    assert s.text[s.entities[1].start : s.entities[1].end] == "03-1234-5678"


def test_parse_xml_tagged_handles_unmatched_tags_gracefully():
    text = "プレーンテキストのみ、タグなし。"
    s = parse_xml_tagged(text)
    assert s.text == text
    assert s.entities == []


def test_split_dataset_partitions_records_and_writes_files(tmp_path: Path):
    src = tmp_path / "all.jsonl"
    records = [
        {"text": f"sample {i}", "entities": [], "scenario_id": f"s{i % 10}"}
        for i in range(400)
    ]
    src.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
    splits = split_dataset(src, tmp_path / "train.jsonl", tmp_path / "dev.jsonl", tmp_path / "test.jsonl")
    assert splits["train"] + splits["dev"] + splits["test"] == 400
    # 90/5/5 ish, allow loose bounds because per-scenario stratification
    # may diverge from the global ratio when small leaves only have a
    # few records.
    assert splits["dev"] >= 10
    assert splits["test"] >= 10
    assert splits["train"] > splits["dev"] + splits["test"]


def test_entity_histogram_counts_labels(tmp_path: Path):
    src = tmp_path / "all.jsonl"
    src.write_text(
        json.dumps({"text": "x", "entities": [{"start": 0, "end": 1, "label": "PERSON"}]}, ensure_ascii=False) + "\n"
        + json.dumps({"text": "y", "entities": [{"start": 0, "end": 1, "label": "PHONE_NUMBER"}]}, ensure_ascii=False) + "\n"
        + json.dumps({"text": "z", "entities": [{"start": 0, "end": 1, "label": "PERSON"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    hist = entity_histogram(src)
    assert hist == {"PERSON": 2, "PHONE_NUMBER": 1}
