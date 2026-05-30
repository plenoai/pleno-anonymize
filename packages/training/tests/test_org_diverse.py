"""ORG positive 多様化 (issue #65) のスキーマ検証 + augment 経路 smoke test."""

from __future__ import annotations

import json

import pytest

from pleno_ner_training.augment_ja_data import (
    ORG_ABBREV,
    ORG_BRANCH_SUFFIX,
    ORG_DIVERSE_SEED_PATH,
    ORG_GAKKOU,
    ORG_IRYOU,
    ORG_KATAKANA_CORP,
    ORG_KOUEKI,
    _random_org_diverse,
    generate_augmented_docs,
    load_org_diverse_seeds,
)

REQUIRED_CATEGORIES = (
    "koueki",
    "gakkou",
    "iryou",
    "abbrev",
    "katakana_corp",
    "branch_suffix",
)
MIN_PER_CATEGORY = 10


def test_seed_json_exists():
    assert ORG_DIVERSE_SEED_PATH.exists(), f"missing seed: {ORG_DIVERSE_SEED_PATH}"


def test_seed_json_valid():
    raw = json.loads(ORG_DIVERSE_SEED_PATH.read_text(encoding="utf-8"))
    assert "categories" in raw
    cats = raw["categories"]
    for name in REQUIRED_CATEGORIES:
        assert name in cats, f"missing category {name}"
        assert isinstance(cats[name], list)
        assert len(cats[name]) >= MIN_PER_CATEGORY, (
            f"category {name} has {len(cats[name])} entries (< {MIN_PER_CATEGORY})"
        )
        assert all(isinstance(s, str) and s.strip() for s in cats[name])
        # 重複なし
        assert len(set(cats[name])) == len(cats[name]), f"duplicates in {name}"


def test_loader_returns_all_categories():
    seeds = load_org_diverse_seeds()
    for name in REQUIRED_CATEGORIES:
        assert name in seeds
        assert len(seeds[name]) >= MIN_PER_CATEGORY


@pytest.mark.parametrize(
    "pool",
    [ORG_KOUEKI, ORG_GAKKOU, ORG_IRYOU, ORG_ABBREV, ORG_KATAKANA_CORP, ORG_BRANCH_SUFFIX],
)
def test_module_level_pools_loaded(pool):
    assert len(pool) >= MIN_PER_CATEGORY


def test_random_org_diverse_returns_known_seed():
    """seed JSON 由来の値以外は返さないこと。"""
    known = set(
        ORG_KOUEKI
        + ORG_GAKKOU
        + ORG_IRYOU
        + ORG_ABBREV
        + ORG_KATAKANA_CORP
        + ORG_BRANCH_SUFFIX
    )
    for _ in range(50):
        v = _random_org_diverse()
        assert v in known


def test_augment_generates_at_least_1000_org_positives():
    """AC: augment された ORG positive ≥ 1000 doc 追加 (#65)."""
    # 比率系 count=0 にして diverse 系のみで 1000 件出力を確認
    docs = generate_augmented_docs(count=0, org_diverse_count=1000)
    assert len(docs) >= 1000
    # 各 doc は ORGANIZATION エンティティを含み、seed 由来の文字列が必ず1つ以上ある
    known = set(
        ORG_KOUEKI
        + ORG_GAKKOU
        + ORG_IRYOU
        + ORG_ABBREV
        + ORG_KATAKANA_CORP
        + ORG_BRANCH_SUFFIX
    )
    org_docs = 0
    seed_hits = 0
    for d in docs:
        labels = {e["label"] for e in d.get("entities", [])}
        if "ORGANIZATION" in labels:
            org_docs += 1
            for e in d["entities"]:
                if e["label"] == "ORGANIZATION" and e["text"] in known:
                    seed_hits += 1
                    break
    assert org_docs >= 1000
    # 全てが seed 由来（diverse 経路から）
    assert seed_hits >= 1000


def test_smoke_full_augment_with_diverse():
    """通常 augment + diverse パイプの統合 smoke test."""
    docs = generate_augmented_docs(count=100, org_diverse_count=50)
    assert len(docs) >= 50
    # スキーマ整合性
    for d in docs:
        assert "text" in d and isinstance(d["text"], str)
        assert "entities" in d and isinstance(d["entities"], list)
        for e in d["entities"]:
            assert {"start", "end", "label", "text"} <= set(e)
            assert d["text"][e["start"] : e["end"]] == e["text"]
