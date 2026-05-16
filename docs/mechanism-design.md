# Simula-style mechanism-design pipeline (epic #147)

Status: scaffolding (Simula 1/8 — taxonomy — committed)

## Rationale

`pleno-anonymize`'s legacy JP data generator (`packages/training/src/pleno_ner_training/prompts/ja/*.j2`) optimises diversity at the **sample** level: each template emits varied examples, but the dataset as a whole has no explicit coverage budget, no independent difficulty axis, and no verification loop. Google Research's [Simula](https://research.google/blog/designing-synthetic-datasets-for-the-real-world-mechanism-design-and-reasoning-from-first-principles/) reframes this as a dataset-level **mechanism design** problem with four stages:

1. **Global Diversification** — reasoning-model-driven hierarchical taxonomy as a sampling scaffold.
2. **Local Diversification** — meta-prompts per taxonomy node to prevent mode collapse.
3. **Complexification** — difficulty as an independent axis, with calibrated complexity scoring.
4. **Dual-critic loop** — two independent critics for label correctness and realism.

This document tracks how those stages map onto the JP NER pipeline. Each stage emits a stable on-disk artefact so downstream stages can run in isolation.

## Stage 1 — Global Diversification (Simula 1/8, issue #148)

Artefact: `packages/training/data/taxonomies/jp_pii_taxonomy.yaml`
Builder:  `packages/training/scripts/build_taxonomy.py`
Code:     `packages/training/src/pleno_ner_training/mechanism/taxonomy.py`

### Schema

```yaml
version: v1.0
language: ja
registers:        [formal, polite, casual, terse]
document_types:   [chat, email, form, transcript, ocr_residue,
                   code_comment, doc_export, post, note, voice_memo]
entity_densities: [sparse, medium, dense]
domains:
  - id: medical
    ja_name: 医療
    sub_domains:
      - id: clinical_record
        ja_name: 診療記録
        scenarios:
          - id: med.clinical.kanja_chart
            ja_name: 外来カルテ
            registers: [formal]
            document_type: doc_export
            entity_density: dense
            expected_entities: [PERSON, DATE_OF_BIRTH, HEALTH_INSURANCE, ADDRESS, PHONE_NUMBER]
```

### Mechanism

- The seed taxonomy is hard-coded in Python (`build_seed_taxonomy`) for **deterministic, reviewable** scaffolding. Re-running `make build-taxonomy` reproduces the YAML byte-for-byte.
- A second pass (`_expand_variants`) emits register/density/document-type variants of every leaf — surface variation only, `expected_entities` unchanged. This is the Simula "local-diversification axis" applied at scaffold-build time so the sampling tree itself is rich, rather than relying on downstream prompts to introduce variation.
- `--enrich` adds an **additive** LLM pass (`gpt-4o-mini` default) that proposes new scenarios. Existing scenario ids are never removed or mutated; only new ids are appended. Enrichment is logged to `output/taxonomy_enrichment.jsonl`.

### Acceptance criteria (issue #148)

| Criterion | Status |
|---|---|
| ≥ 30 domains | 35 |
| ≥ 200 scenarios | 382 |
| Per-leaf metadata (registers / document_type / entity_density / expected_entities) | yes |
| Idempotent regeneration | yes (`make build-taxonomy`) |
| Covers every canonical pleno entity label | 17 / 17 |

### Reproducibility

```bash
cd packages/training
make build-taxonomy                 # deterministic
make build-taxonomy-enrich          # additive LLM expansion (requires OPENAI_API_KEY in ../../.env)
uv run --extra training --with pytest pytest tests/test_taxonomy.py -v
```

## Stage 2 — Local Diversification (Simula 2/8, issue #149)

Artefact: `packages/training/data/meta_prompts/jp/all.jsonl`
Builder:  `packages/training/scripts/build_meta_prompts.py`
Code:     `packages/training/src/pleno_ner_training/mechanism/meta_prompts.py`

### Mechanism

Each taxonomy leaf is fanned out into ≥ 5 meta-prompts via five
canonical **lenses** that span orthogonal axes:

| Axis | Values |
|---|---|
| perspective | self · third_party · neutral |
| length_hint | short (<200) · medium (200-700) · long (700-1500) |
| opening_cue | mid_thread · header · salutation · abrupt · form_label |
| vocabulary | plain · jargon · dialect · mixed_script |
| twist | straight · redaction_attempt · partial_ocr · code_switch |

Registers from the leaf are cycled through the five lens applications.
The axes were chosen because they alter the **surface form** of the
generated sample without redefining the underlying scenario — Simula
prescribes that local diversification varies form while global
diversification varies semantics.

### Relationship to legacy `prompts/ja/*.j2`

The Jinja templates under `packages/training/src/pleno_ner_training/prompts/ja/`
remain on disk for historical traceability but **no longer drive the
default generation flow**. The new pipeline (Stages 1 → 4) replaces
them in `make generate`-style targets that will be wired up in #152.
They will be removed once the v2 model on RunPod has shipped (#155).

### Acceptance criteria (issue #149)

| Criterion | Target | Actual |
|---|---|---|
| Meta-prompts per leaf | ≥ 5 | 5 |
| Duplicate rate (lens fingerprint) | < 5 % | 0 % |
| Documented relationship to legacy `.j2` | yes | this section |

### Reproducibility

```bash
cd packages/training
make build-meta-prompts
uv run --extra training --with pytest pytest tests/test_meta_prompts.py -v
```

## Stage 3 — Complexification (planned, issue #150)

Independent difficulty operators (obfuscation, ambiguity, code-switching, multi-entity coupling, adversarial near-PII) plus Elo-calibrated complexity scores.

## Stage 4 — Dual-critic loop (planned, issue #151)

Two LLM critics (label correctness, realism + coverage) on every sample, with auto-correction → re-check or rejection.

## Downstream

- Stage 5 (#152): generate ≥ 30k JP samples via the full pipeline.
- Stage 6 (#153): train `ja_ner_ja` v2 on RunPod (no local training per CLAUDE.md).
- Stage 7 (#154): benchmark vs `0xhikae/pii-masking-300k-ja` validation (n=300, IoU ≥ 0.5).
- Stage 8 (#155): release to HF Hub (model + dataset).
