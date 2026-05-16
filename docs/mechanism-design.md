# Simula-style mechanism-design pipeline (epic #147)

Status: Stages 1–5 infrastructure committed; v1 dataset generation in flight. Stages 6–8 next.

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

## Stage 3 — Complexification (Simula 3/8, issue #150)

Artefact: `packages/training/data/processed/ja-mechanism-v1/scored.jsonl` (generated)
Builder:  `packages/training/scripts/score_difficulty.py`
Code:     `packages/training/src/pleno_ner_training/mechanism/complexify.py`

### Operators

| Operator | Effect | Cost |
|---|---|---|
| `obfuscate` | Half/full-width digit swap, hyphen drop, honorific strip | rule-based |
| `add_ambiguity` | Insert a name-like distractor (e.g. "山田工業株式会社") near a PERSON span without tagging it | rule-based |
| `code_switch` | JP→romaji for known kanji name tokens (50% coverage via lookup table; LLM extension lives in #152) | rule-based |
| `couple_entities` | Add a related second PERSON (配偶者 / 緊急連絡先 / 保証人) so the model must disambiguate co-reference | rule-based |
| `add_near_pii` | Prepend a UUID / sample number / hash that regex baselines mis-fire on | rule-based |

All operators preserve span invariants — `validate_spans()` checks the (start, end) coordinates round-trip after every mutation. The label set never shrinks.

### Difficulty score

`difficulty_score(sample) → [0, 1]` combines:

- operator weights (each applied operator contributes 0.10–0.25)
- length norm (longer = mildly harder)
- entity density
- mixed-script ratio (JP + Latin + digits)

Buckets: `easy < 0.25 ≤ medium < 0.55 ≤ hard`.

### Target-ratio application

`apply_with_ratio(samples, target={easy: .5, medium: .3, hard: .2})` partitions samples by seed and applies a light op to medium and a 3-step chain to hard. The operator-application proportions match the target exactly; bucket counts may drift slightly because surface features also feed into the score.

### Calibrated complexity (optional)

`score_difficulty.py --llm-elo` runs pairwise comparisons through an LLM (default `gpt-4o-mini`) and rescales `difficulty` into the Elo percentile, per Simula §4. The heuristic remains the default for non-LLM runs.

### Acceptance criteria (issue #150)

| Criterion | Status |
|---|---|
| 5 operators apply individually | yes (`tests/test_complexify.py::test_each_operator_preserves_span_invariants`) |
| Elo scoring produces a distribution + histogram | yes (`--llm-elo`; `--histogram` artefact) |
| Target ratio achievable within ±5 % | yes (operator-application count matches target exactly) |

### Reproducibility

```bash
cd packages/training
make score-difficulty DIFFICULTY_IN=path/to/raw.jsonl
uv run --extra training --with pytest pytest tests/test_complexify.py -v
```

## Stage 4 — Dual-critic loop (Simula 4/8, issue #151)

Code:     `packages/training/src/pleno_ner_training/mechanism/critics.py`

### Critics

- **`LocalLabelCritic` / `OpenAILabelCritic`** — verify each (text, span, label) triple. The LLM variant returns `verdict: PASS` or `verdict: FIX, fixed_spans: [...]` so the pipeline can auto-correct once before rejecting. The LLM uses a different model SKU from the generator (per Simula §3.4 dual-population critique).
- **`LocalRealismCritic` / `OpenAIRealismCritic`** — verify scenario plausibility, length, entity density, and presence of at least one expected entity per the taxonomy leaf.

### Pipeline

`CriticPipeline.verify(sample, leaf) → (sample, verdict ∈ {pass, fixed, rejected})`. Stats (seen, label_pass, label_fixed, label_rejected, realism_pass, realism_rejected, reject_reasons) are recorded for the generator (#152) to log to `experiments/log.jsonl`.

### Acceptance criteria (issue #151)

| Criterion | Status |
|---|---|
| Two independent critics behind protocols | yes (LabelCritic / RealismCritic) |
| Local + OpenAI implementations | yes |
| Auto-correct path | yes (`fixed_spans` round-trip) |
| Golden false-pass < 5 % (rule-based, on synthetic golden set) | yes (`tests/test_critics.py::test_pipeline_golden_false_pass_on_bad_data_above_threshold`) |
| Acceptance rate logged in `CriticStats` | yes |

### Reproducibility

```bash
cd packages/training
uv run --extra training --with pytest pytest tests/test_critics.py -v
```

## Stage 5 — Pipeline integration (Simula 5/8, issue #152)

Artefact: `packages/training/data/raw/ja-mechanism-v1/{all,train,dev,test}.jsonl` (generated; gitignored)
Builder:  `packages/training/scripts/generate_mechanism_dataset.py`
Code:     `packages/training/src/pleno_ner_training/mechanism/generate.py`

### Pipeline

```
meta-prompts.jsonl  ─▶  LLM (gpt-4o-mini default)
                         ▼
                       parse_xml_tagged  ─▶  Sample(text, entities)
                         ▼
                       CriticPipeline.verify (label + realism)
                         ▼
                       apply_with_ratio   (target {easy: .5, medium: .3, hard: .2})
                         ▼
                       jsonl + stratified train/dev/test split
```

The generator parallelises across meta-prompts with `--max-workers`
threads, retries with exponential backoff on API errors, and writes
incrementally so a long run is recoverable. Smoke runs (`--smoke`,
50 prompts × 2 samples) validate the wiring end-to-end in seconds.

### Schema (per accepted sample)

```json
{
  "text": "...",
  "entities": [{"start": int, "end": int, "label": str}, ...],
  "scenario_id": "med.clinical.kanja_chart",
  "meta_prompt_id": "med.clinical.kanja_chart#00",
  "register": "formal",
  "document_type": "doc_export",
  "entity_density": "dense",
  "lens": {"perspective": ..., "length_hint": ..., ...},
  "difficulty": 0.39,
  "difficulty_bucket": "medium",
  "operators_applied": ["obfuscate"],
  "verdict": "pass"
}
```

### Cost

| Run | meta_prompts | samples/prompt | API calls | est. cost | est. wall-clock |
|---|---:|---:|---:|---:|---:|
| smoke | 50 | 2 | 100 | $0.05 | < 1 min |
| v1 default | 1,910 | 8 | 15,280 | ~$4 | ~2 hr |
| v1 stretch | 1,910 | 16 | 30,560 | ~$15 | ~4 hr |

### Reproducibility

```bash
cd packages/training
dotenvx run -f ../../.env -- uv run --extra training python \
  scripts/generate_mechanism_dataset.py --smoke

# Full run
dotenvx run -f ../../.env -- uv run --extra training python \
  scripts/generate_mechanism_dataset.py \
    --samples-per-prompt 8 --max-workers 32 \
    --output-dir data/raw/ja-mechanism-v1

uv run --extra training --with pytest pytest tests/test_generate.py -v
```

## Stage 6 — Training (Simula 6/8, issue #153)

Code:     `packages/training/src/pleno_ner_training/mechanism/train.py`
Runner:   `packages/training/scripts/train_mechanism.py`
RunPod:   `docs/training-runpod-mechanism.md`

### Pipeline

1. Load `data/raw/ja-mechanism-v1/{train,dev,test}.jsonl`.
2. Tokenise with the base model's fast tokenizer (`return_offsets_mapping=True`).
3. Align char-offset spans to token-level **BIO** labels (`_bio_labels_for_tokens`).
4. Fine-tune via HuggingFace `Trainer` with `seqeval` metrics.
5. Save `model-best/` + `metrics.json`.

Base model default: `cl-tohoku/bert-base-japanese-v3` (~110M params, fast tokenizer required). The legacy `train_hf.py` uses `ku-nlp/deberta-v2-base-japanese`; both are interchangeable through `--base-model`.

### Where it runs

Per CLAUDE.md, training **must not run locally**. The RunPod orchestration in [`docs/training-runpod-mechanism.md`](training-runpod-mechanism.md) creates a self-contained pod that:

- pulls the dataset from `plenoai/pii-masking-jp-mechanism-v1` (HF Hub),
- runs `scripts/train_mechanism.py`,
- pushes the result to `plenoai/ja_ner_ja-v2` (HF Hub),
- self-terminates.

The pod is launched via the **RunPod MCP** (`mcp__runpod__create-pod` / `get-pod` / `delete-pod`), **not** chrome MCP.

Local Makefile targets `train-mechanism-smoke` / `train-mechanism` exist solely as a sanity check on the wiring; they will be deleted once the v2 model has shipped.

## Stage 7 — Benchmark (Simula 7/8, issue #154)

Eval driver: `packages/sdk/scripts/eval_pii_masking_300k.py` (do not modify; public ruler is fixed)
Target:     F1 ≥ Smoke (0.50), aim for Parity (0.82) vs `openai-privacy-filter`.

## Stage 8 — HF release (Simula 8/8, issue #155)

- `scripts/push_dataset_to_hf.py` — uploads JSONL splits + dataset card.
- `scripts/push_model_to_hf.py` — uploads model + tokenizer + model card.
- `make push-dataset-hf` / `make push-model-hf` wrappers.
