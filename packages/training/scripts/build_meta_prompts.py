"""Emit per-leaf meta-prompts for the JP PII taxonomy.

The default run is deterministic: it consumes the committed taxonomy and
writes `data/meta_prompts/jp/all.jsonl` (≥ 5 meta-prompts × #leaves).
Idempotent — re-running with the same inputs reproduces the file
byte-for-byte.

Example:
    uv run python scripts/build_meta_prompts.py \\
        --taxonomy data/taxonomies/jp_pii_taxonomy.yaml \\
        --output data/meta_prompts/jp/all.jsonl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pleno_ner_training.mechanism.taxonomy import (  # noqa: E402
    Domain,
    Scenario,
    SubDomain,
    Taxonomy,
    build_seed_taxonomy,
    load_yaml,
)
from pleno_ner_training.mechanism.meta_prompts import (  # noqa: E402
    CANONICAL_LENSES,
    build_meta_prompts,
    estimate_dup_rate,
    save_jsonl,
)


def _from_yaml(payload: dict) -> Taxonomy:
    domains: list[Domain] = []
    for d in payload["domains"]:
        sub_domains: list[SubDomain] = []
        for sd in d["sub_domains"]:
            scenarios: list[Scenario] = []
            for s in sd["scenarios"]:
                scenarios.append(
                    Scenario(
                        id=s["id"],
                        ja_name=s["ja_name"],
                        registers=tuple(s["registers"]),
                        document_type=s["document_type"],
                        entity_density=s["entity_density"],
                        expected_entities=tuple(s["expected_entities"]),
                    )
                )
            sub_domains.append(SubDomain(id=sd["id"], ja_name=sd["ja_name"], scenarios=tuple(scenarios)))
        domains.append(Domain(id=d["id"], ja_name=d["ja_name"], sub_domains=tuple(sub_domains)))
    return Taxonomy(
        version=payload.get("version", "v1.0"),
        language=payload.get("language", "ja"),
        domains=tuple(domains),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=Path("data/taxonomies/jp_pii_taxonomy.yaml"),
        help="Taxonomy YAML (falls back to the in-code seed if missing).",
    )
    parser.add_argument("--output", type=Path, default=Path("data/meta_prompts/jp/all.jsonl"))
    parser.add_argument(
        "--max-dup-rate",
        type=float,
        default=0.05,
        help="Fail if the (scenario × register × lens) fingerprint collision rate exceeds this.",
    )
    args = parser.parse_args()

    if args.taxonomy.exists():
        tax = _from_yaml(load_yaml(args.taxonomy))
        print(f"[load] {args.taxonomy}")
    else:
        tax = build_seed_taxonomy()
        print(f"[load] seed (no YAML at {args.taxonomy})")

    stats = tax.stats()
    print(f"  scenarios:   {stats['scenarios']}")

    prompts = build_meta_prompts(tax, lenses=CANONICAL_LENSES)
    print(f"  meta-prompts (canonical lenses): {len(prompts)} (= {len(CANONICAL_LENSES)} × {stats['scenarios']})")

    n = save_jsonl(prompts, args.output)
    print(f"[write] {args.output} ({n} lines)")

    leaves_with_few = [s.id for s in tax.leaves() if len(CANONICAL_LENSES) < 5]
    if leaves_with_few:
        raise SystemExit(f"FAIL: {len(leaves_with_few)} leaves have < 5 meta-prompts")

    dup_rate = estimate_dup_rate(prompts)
    print(f"  duplicate rate (lens-fingerprint collisions): {dup_rate:.3%}")
    if dup_rate >= args.max_dup_rate:
        raise SystemExit(f"FAIL: duplicate rate {dup_rate:.3%} ≥ {args.max_dup_rate:.1%}")

    print(f"[ok] AC met: ≥ 5 meta-prompts per leaf, duplicate rate < {args.max_dup_rate:.0%}")


if __name__ == "__main__":
    main()
