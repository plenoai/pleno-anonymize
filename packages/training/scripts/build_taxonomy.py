"""Build the JP PII taxonomy artefact.

Default behaviour is deterministic: emit the seed taxonomy hard-coded in
`pleno_ner_training.mechanism.taxonomy.build_seed_taxonomy`. The optional
`--enrich` flag uses a reasoning model to widen the taxonomy further;
enrichment is additive only — seed scenarios are never removed or
modified. Idempotent: re-running with the same inputs reproduces the
artefact byte-for-byte (except `enrichment_log` if `--enrich` is used).

Example:
    uv run python scripts/build_taxonomy.py \\
        --output data/taxonomies/jp_pii_taxonomy.yaml

    # Enrichment (requires OPENAI_API_KEY)
    dotenvx run -f ../../.env -- \\
        uv run python scripts/build_taxonomy.py \\
            --output data/taxonomies/jp_pii_taxonomy.yaml \\
            --enrich --model gpt-4o-mini --max-new-scenarios 50
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

# Allow running as a script from packages/training/ without `pip install -e .`.
ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pleno_ner_training.mechanism.taxonomy import (  # noqa: E402
    DOCUMENT_TYPES,
    ENTITY_DENSITIES,
    REGISTERS,
    Domain,
    Scenario,
    SubDomain,
    Taxonomy,
    build_seed_taxonomy,
    save_json,
    save_yaml,
    to_dict,
)
from pleno_ner_training.entity_types import NER_LABELS, PATTERN_LABELS  # noqa: E402


def _ensure_unique_ids(t: Taxonomy) -> None:
    seen: dict[str, str] = {}
    for s in t.leaves():
        if s.id in seen:
            raise ValueError(f"duplicate scenario id {s.id!r}")
        seen[s.id] = s.ja_name


def _print_stats(t: Taxonomy) -> None:
    s = t.stats()
    print(f"  domains:      {s['domains']}")
    print(f"  sub_domains:  {s['sub_domains']}")
    print(f"  scenarios:    {s['scenarios']}")
    print(f"  entity types covered: {s['entity_coverage']} / {len(NER_LABELS) + len(PATTERN_LABELS)}")


def _enrich(t: Taxonomy, model: str, max_new: int) -> tuple[Taxonomy, list[dict]]:
    """Append LLM-proposed new scenarios. Pure addition — seeds untouched."""
    try:
        from openai import OpenAI
    except ImportError:
        print("openai package not installed; install training extras to enrich.", file=sys.stderr)
        return t, []

    if "OPENAI_API_KEY" not in os.environ:
        print("OPENAI_API_KEY missing; skipping enrichment.", file=sys.stderr)
        return t, []

    client = OpenAI()
    canonical = list(NER_LABELS) + list(PATTERN_LABELS)
    existing = sorted({d.ja_name for d in t.domains})

    user_prompt = (
        "あなたは日本語 PII データセット設計者です。Simula 方式に従い、既存の "
        "タクソノミーに **新しい未網羅シナリオ** を提案してください。\n\n"
        f"既存ドメイン: {existing}\n"
        f"利用可能なレジスタ: {list(REGISTERS)}\n"
        f"利用可能な文書タイプ: {list(DOCUMENT_TYPES)}\n"
        f"利用可能なエンティティ密度: {list(ENTITY_DENSITIES)}\n"
        f"利用可能なエンティティラベル: {canonical}\n\n"
        f"最大 {max_new} 件、以下 JSON Lines 形式 (1 行 1 シナリオ) で出力してください。\n"
        '{"id": "<domain>.<sub>.<slug>", "ja_name": "...", "domain_id": "...", '
        '"domain_ja": "...", "sub_id": "...", "sub_ja": "...", '
        '"registers": ["polite"], "document_type": "email", '
        '"entity_density": "medium", "expected_entities": ["PERSON", "EMAIL_ADDRESS"]}\n\n'
        "重要: 既存ドメイン名の重複は許可。**id はユニーク**。エンティティラベルは必ず上記から選ぶ。"
    )

    resp = client.chat.completions.create(
        model=model,
        temperature=0.7,
        messages=[
            {"role": "system", "content": "あなたは PII データセット設計の専門家。出力は JSON Lines のみ。"},
            {"role": "user", "content": user_prompt},
        ],
    )
    raw = resp.choices[0].message.content or ""

    added: list[Scenario] = []
    log: list[dict] = []
    by_domain: dict[str, list[Scenario]] = {}
    by_subdomain: dict[tuple[str, str], list[Scenario]] = {}
    domain_meta: dict[str, str] = {}
    sub_meta: dict[tuple[str, str], str] = {}

    for line in raw.splitlines():
        line = line.strip().lstrip("`").rstrip("`")
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            scen = Scenario(
                id=obj["id"],
                ja_name=obj["ja_name"],
                registers=tuple(obj["registers"]),
                document_type=obj["document_type"],
                entity_density=obj["entity_density"],
                expected_entities=tuple(obj["expected_entities"]),
            )
            scen.validate()
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            log.append({"line": line, "error": str(e)})
            continue
        added.append(scen)
        d_id = obj["domain_id"]
        s_id = obj["sub_id"]
        domain_meta[d_id] = obj["domain_ja"]
        sub_meta[(d_id, s_id)] = obj["sub_ja"]
        by_subdomain.setdefault((d_id, s_id), []).append(scen)
        by_domain.setdefault(d_id, [])

    existing_ids = {s.id for s in t.leaves()}
    dedup = [s for s in added if s.id not in existing_ids]
    if not dedup:
        return t, log

    domain_map = {d.id: d for d in t.domains}
    for (d_id, s_id), scens in by_subdomain.items():
        if d_id in domain_map:
            old = domain_map[d_id]
            existing_sub = {sd.id: sd for sd in old.sub_domains}
            if s_id in existing_sub:
                old_sub = existing_sub[s_id]
                new_sub = SubDomain(
                    id=old_sub.id,
                    ja_name=old_sub.ja_name,
                    scenarios=old_sub.scenarios + tuple(scens),
                )
            else:
                new_sub = SubDomain(id=s_id, ja_name=sub_meta[(d_id, s_id)], scenarios=tuple(scens))
                existing_sub[s_id] = new_sub
            existing_sub_list = []
            for sd in old.sub_domains:
                existing_sub_list.append(new_sub if sd.id == s_id else sd)
            if s_id not in {sd.id for sd in old.sub_domains}:
                existing_sub_list.append(new_sub)
            domain_map[d_id] = replace(old, sub_domains=tuple(existing_sub_list))
        else:
            new_sub = SubDomain(id=s_id, ja_name=sub_meta[(d_id, s_id)], scenarios=tuple(scens))
            domain_map[d_id] = Domain(id=d_id, ja_name=domain_meta[d_id], sub_domains=(new_sub,))

    merged = Taxonomy(version=t.version + "+enriched", language=t.language, domains=tuple(domain_map.values()))
    log.append({"added_scenarios": len(dedup)})
    return merged, log


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=Path("data/taxonomies/jp_pii_taxonomy.yaml"))
    parser.add_argument("--json-output", type=Path, default=None, help="Optional JSON mirror for diff tooling.")
    parser.add_argument("--enrich", action="store_true", help="Run LLM enrichment pass (additive).")
    parser.add_argument("--model", default="gpt-4o-mini", help="Model for enrichment.")
    parser.add_argument("--max-new-scenarios", type=int, default=50)
    parser.add_argument("--enrichment-log", type=Path, default=Path("output/taxonomy_enrichment.jsonl"))
    args = parser.parse_args()

    tax = build_seed_taxonomy()
    _ensure_unique_ids(tax)
    print(f"[seed] built taxonomy {tax.version}")
    _print_stats(tax)

    if args.enrich:
        before = tax.stats()["scenarios"]
        tax, log = _enrich(tax, args.model, args.max_new_scenarios)
        _ensure_unique_ids(tax)
        added = tax.stats()["scenarios"] - before
        print(f"[enrich] added {added} scenarios via {args.model}")
        _print_stats(tax)
        args.enrichment_log.parent.mkdir(parents=True, exist_ok=True)
        with args.enrichment_log.open("w", encoding="utf-8") as f:
            for entry in log:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    save_yaml(tax, args.output)
    print(f"[write] {args.output}")
    if args.json_output:
        save_json(tax, args.json_output)
        print(f"[write] {args.json_output}")

    s = tax.stats()
    if s["domains"] < 30:
        raise SystemExit(f"FAIL: {s['domains']} domains < 30 required")
    if s["scenarios"] < 200:
        raise SystemExit(f"FAIL: {s['scenarios']} scenarios < 200 required")
    print("[ok] AC met: ≥ 30 domains and ≥ 200 scenarios")


if __name__ == "__main__":
    main()
