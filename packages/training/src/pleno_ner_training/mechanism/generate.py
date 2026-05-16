"""Stage 5 — full-pipeline JP synthetic dataset generation.

Wires the four earlier stages together:

    taxonomy + meta-prompts  ->  LLM generation  ->  complexification
                                                 ->  dual-critic gate
                                                 ->  accepted JSONL

Each meta-prompt produces `--samples-per-prompt` candidates. Output
schema (per accepted sample):

    {
        "text": "...",
        "entities": [{"start", "end", "label"}, ...],
        "scenario_id": "...",
        "meta_prompt_id": "...",
        "register": "...",
        "document_type": "...",
        "entity_density": "...",
        "lens": {...},
        "difficulty": float | None,
        "difficulty_bucket": "easy|medium|hard",
        "operators_applied": [...],
        "verdict": "pass|fixed"
    }
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from pleno_ner_training.entity_types import NER_LABELS, PATTERN_LABELS
from pleno_ner_training.mechanism.complexify import (
    DEFAULT_TARGET,
    Sample,
    Span,
    apply_with_ratio,
    bucket,
    difficulty_score,
)
from pleno_ner_training.mechanism.critics import (
    CriticPipeline,
    LocalLabelCritic,
    LocalRealismCritic,
)

ALL_LABELS = list(NER_LABELS) + list(PATTERN_LABELS)
TAG_PATTERN = re.compile(r"<(" + "|".join(ALL_LABELS) + r")>(.*?)</\1>", re.DOTALL)


def parse_xml_tagged(text: str) -> Sample:
    """Convert <LABEL>...</LABEL>-tagged text into a Sample with char offsets."""
    plain_parts: list[str] = []
    entities: list[Span] = []
    last_end = 0
    offset = 0
    for m in TAG_PATTERN.finditer(text):
        before = text[last_end : m.start()]
        plain_parts.append(before)
        offset += len(before)
        label = m.group(1)
        surface = m.group(2)
        entities.append(Span(offset, offset + len(surface), label))
        plain_parts.append(surface)
        offset += len(surface)
        last_end = m.end()
    plain_parts.append(text[last_end:])
    return Sample(text="".join(plain_parts), entities=entities)


# --- Single-prompt generation --------------------------------------------

def _generate_one(client, model: str, meta_prompt: dict, max_retries: int = 3) -> Sample | None:
    """Call the LLM once for a meta-prompt and parse the XML-tagged response."""
    system = (
        "あなたは日本語のPII合成データ生成エージェントです。"
        "指定された XML タグでエンティティをマーキングし、本文のみを出力してください。"
    )
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.8,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": meta_prompt["instruction"]},
                ],
            )
            raw = (resp.choices[0].message.content or "").strip()
            sample = parse_xml_tagged(raw)
            if sample.entities and len(sample.text) >= 20:
                return sample
        except Exception as e:  # noqa: BLE001
            if attempt + 1 == max_retries:
                print(f"[generate] failed after {max_retries} retries: {e}", file=sys.stderr)
            time.sleep(2**attempt)
    return None


# --- Pipeline ------------------------------------------------------------

def _leaf_view(mp: dict) -> dict:
    """Make a taxonomy-leaf-shaped dict for the realism critic."""
    return {
        "id": mp["scenario_id"],
        "ja_name": mp.get("scenario_id", ""),
        "document_type": mp["document_type"],
        "entity_density": mp["entity_density"],
        "expected_entities": mp["expected_entities"],
    }


def generate_dataset(
    meta_prompts: list[dict],
    samples_per_prompt: int,
    model: str,
    max_workers: int,
    output_path: Path,
    log_path: Path,
    seed: int = 0,
    target_buckets: dict[str, float] | None = None,
    skip_complexification: bool = False,
    skip_critics: bool = False,
) -> dict:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai not installed; install training extras") from e
    if "OPENAI_API_KEY" not in os.environ:
        raise RuntimeError("OPENAI_API_KEY not set in environment")

    client = OpenAI()

    pipeline = CriticPipeline(label_critic=LocalLabelCritic(), realism_critic=LocalRealismCritic())

    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    accepted = 0
    total_attempts = 0
    rejected_per_reason: dict[str, int] = {}

    # Fan jobs out: (meta_prompt, attempt_idx) for each pair.
    jobs: list[tuple[dict, int]] = [
        (mp, i)
        for mp in meta_prompts
        for i in range(samples_per_prompt)
    ]

    f_out = output_path.open("w", encoding="utf-8")
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_generate_one, client, model, mp): (mp, i) for mp, i in jobs}
            for fut in as_completed(futures):
                mp, _idx = futures[fut]
                total_attempts += 1
                sample = fut.result()
                if sample is None:
                    rejected_per_reason["llm:no_output"] = rejected_per_reason.get("llm:no_output", 0) + 1
                    continue
                if not skip_critics:
                    sample, verdict = pipeline.verify(sample, _leaf_view(mp))
                    if verdict == "rejected":
                        continue
                else:
                    verdict = "pass"
                # difficulty annotation (without rewriting — that happens
                # downstream in batch via apply_with_ratio if requested)
                sample.difficulty = difficulty_score(sample)
                record = {
                    "text": sample.text,
                    "entities": [{"start": e.start, "end": e.end, "label": e.label} for e in sample.entities],
                    "scenario_id": mp["scenario_id"],
                    "meta_prompt_id": mp["id"],
                    "register": mp["register"],
                    "document_type": mp["document_type"],
                    "entity_density": mp["entity_density"],
                    "lens": mp["lens"],
                    "difficulty": sample.difficulty,
                    "difficulty_bucket": bucket(sample.difficulty),
                    "operators_applied": sample.operators_applied,
                    "verdict": verdict,
                }
                f_out.write(json.dumps(record, ensure_ascii=False) + "\n")
                f_out.flush()
                accepted += 1
    finally:
        f_out.close()

    # Apply complexification ratio to the accepted file in-place.
    if not skip_complexification and accepted > 0:
        samples: list[Sample] = []
        records: list[dict] = []
        with output_path.open("r", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                records.append(rec)
                samples.append(
                    Sample(
                        text=rec["text"],
                        entities=[Span(e["start"], e["end"], e["label"]) for e in rec["entities"]],
                    )
                )
        hardened = apply_with_ratio(samples, target=target_buckets or DEFAULT_TARGET, seed=seed)
        with output_path.open("w", encoding="utf-8") as f:
            for rec, s in zip(records, hardened):
                rec["text"] = s.text
                rec["entities"] = [{"start": e.start, "end": e.end, "label": e.label} for e in s.entities]
                rec["difficulty"] = s.difficulty
                rec["difficulty_bucket"] = bucket(s.difficulty or 0)
                rec["operators_applied"] = s.operators_applied
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    stats = {
        "meta_prompts": len(meta_prompts),
        "samples_per_prompt": samples_per_prompt,
        "attempted": total_attempts,
        "accepted": accepted,
        "accept_rate": accepted / max(total_attempts, 1),
        "critic_stats": {
            "seen": pipeline.stats.seen,
            "label_pass": pipeline.stats.label_pass,
            "label_fixed": pipeline.stats.label_fixed,
            "label_rejected": pipeline.stats.label_rejected,
            "realism_pass": pipeline.stats.realism_pass,
            "realism_rejected": pipeline.stats.realism_rejected,
            "reject_reasons": pipeline.stats.reject_reasons,
        },
        "model": model,
    }
    log_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def load_meta_prompts(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def split_dataset(path: Path, train_path: Path, dev_path: Path, test_path: Path, seed: int = 0) -> dict[str, int]:
    """Materialise a stratified train/dev/test split (90/5/5) on scenario_id."""
    import random

    records = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rng = random.Random(seed)
    by_scenario: dict[str, list[dict]] = {}
    for r in records:
        by_scenario.setdefault(r["scenario_id"], []).append(r)
    train: list[dict] = []
    dev: list[dict] = []
    test: list[dict] = []
    for scen, recs in by_scenario.items():
        rng.shuffle(recs)
        n = len(recs)
        n_dev = max(1, n // 20) if n >= 20 else 0
        n_test = max(1, n // 20) if n >= 20 else 0
        dev.extend(recs[:n_dev])
        test.extend(recs[n_dev : n_dev + n_test])
        train.extend(recs[n_dev + n_test :])

    for p, rs in [(train_path, train), (dev_path, dev), (test_path, test)]:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as f:
            for r in rs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return {"train": len(train), "dev": len(dev), "test": len(test)}


def entity_histogram(path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            for e in rec["entities"]:
                counts[e["label"]] = counts.get(e["label"], 0) + 1
    return counts
