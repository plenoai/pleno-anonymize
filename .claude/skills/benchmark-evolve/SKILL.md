---
name: benchmark-evolve
description: |
  NERベンチマークを自律的に進化させるスキル。
  モデルが現行ベンチマークを突破したら、より困難なベンチマークを自動生成する。

  Trigger: benchmark-evolve, ベンチマーク進化, ベンチマーク強化, benchmark更新
---

# Benchmark Self-Evolution Loop

When the NER model beats the current benchmark, this skill autonomously designs and generates a harder benchmark version to keep the improvement cycle going.

## Trigger

benchmark-evolve, ベンチマーク進化, ベンチマーク強化, benchmark更新

## Invocation

`/benchmark-evolve [language]`

- language: `ja` (default) or `en`

Example: `/benchmark-evolve ja`

## Context

### Benchmark Architecture
Each benchmark version is defined by:
1. **Prompt templates**: `packages/training/src/pleno_ner_training/prompts/benchmark_v0X/{language}/*.j2`
2. **Config entry**: `BENCHMARK_CONFIGS` dict in `packages/training/src/pleno_ner_training/generate_benchmark.py`
3. **Generated data**: `packages/training/data/benchmark/v0.X.0/{language}/raw.json` + `test.spacy`
4. **Scores**: `packages/training/data/benchmark/v0.X.0/{language}/scores.json`

### Version History
| Version | Focus |
|---|---|
| v0.2.0 | False positives (negative-only, distractor, narrative, mixed-lang) |
| v0.3.0 | Boundary/type confusion (adversarial negative, type confusion, boundary ambiguity, corrupted, cross-sentence) |
| v0.4.0 | Semantic traps (adversarial negative v2, semantic trap, minimal context, redacted partial) + v0.3.0 carry-forward |

### Key Paths
- Prompts root: `packages/training/src/pleno_ner_training/prompts/`
- Benchmark configs: `packages/training/src/pleno_ner_training/generate_benchmark.py` (`BENCHMARK_CONFIGS`)
- Evaluate benchmark versions list: `packages/training/src/pleno_ner_training/evaluate_benchmark.py` (`BENCHMARK_VERSIONS`)
- Makefile: `packages/training/Makefile`
- Experiment log: `packages/training/experiments/log.jsonl`

### Acceptance Criteria (model must beat these to trigger evolution)
| Entity | F1 Threshold | Recall Minimum |
|---|---|---|
| PERSON | 0.90 | 0.85 |
| ADDRESS | 0.85 | 0.80 |
| ORGANIZATION | 0.85 | 0.80 |
| DATE_OF_BIRTH | 0.80 | 0.75 |
| BANK_ACCOUNT | 0.80 | 0.75 |
| **Overall F1** | **0.88** | - |

## Instructions

You are an autonomous benchmark evolution agent. Your job is to ensure the benchmark stays ahead of the model — a moving goalpost that drives continuous improvement.

### Phase 0: Readiness Check

1. Read the latest benchmark scores: `packages/training/data/benchmark/v0.4.0/{language}/scores.json` (or whatever the latest version is)
2. Read the test evaluation scores: `packages/training/output/*/scores.json`
3. Read experiment log: `packages/training/experiments/log.jsonl`
4. Determine the LATEST benchmark version by inspecting `BENCHMARK_CONFIGS` keys in `generate_benchmark.py`

**Proceed only if** the model's overall F1 on the latest benchmark >= 0.70 OR the model passes acceptance criteria on test data. If neither, output: "Model not ready for benchmark evolution. Run /ner-improve first." and stop.

The threshold is 0.70 (not 0.88) because benchmark data is intentionally adversarial — a 70% score on hard adversarial data indicates the model has substantially learned the patterns in the current benchmark.

### Phase 1: Error Taxonomy

Analyze the model's remaining weaknesses on the current benchmark:

1. **Per-entity analysis**: Which entities have the lowest F1? Lowest recall? Lowest precision?
2. **Error pattern classification**: Read `data/benchmark/{latest_version}/{language}/raw.json` and sample 20-30 documents where the model fails. Classify errors into:
   - **FP patterns**: What non-PII text is the model falsely detecting?
   - **FN patterns**: What real PII is the model missing?
   - **Boundary errors**: Where is the model getting span boundaries wrong?
   - **Type errors**: Where is the model confusing entity types?
3. **Saturation detection**: Which existing benchmark templates does the model now score > 85% on? These are "solved" — new templates should replace or supplement them.

Output a structured error taxonomy table.

### Phase 2: Design New Benchmark Version

Based on the error taxonomy, design the next benchmark version (v0.X+1.0):

#### Naming Convention
- If current latest is v0.4.0, new version is v0.5.0
- Prompt subdir: `benchmark_v05`

#### Template Design Principles
Each benchmark version should:
1. **Carry forward 2-3 hardest templates** from previous version (the ones model scores lowest on)
2. **Drop solved templates** (model scores > 85%)
3. **Add 3-5 new adversarial templates** targeting discovered weaknesses
4. **Maintain balance**: ~40% negative/FP pressure, ~30% boundary/type confusion, ~30% novel patterns

#### New Template Requirements
Each `.j2` template must:
- Have a clear `## 目的` (purpose) section
- Specify exact PII tag format with examples
- Include 3+ concrete example patterns for the LLM to follow
- Specify output format: `---DOC_SEPARATOR---` separated, with character count range
- Target a specific weakness category

#### Weight Assignment
Assign weights based on priority:
- High priority weaknesses (leakage risk): weight 3.0-4.0
- Medium priority (F1 gap > 10%): weight 2.0-2.5
- Carried forward templates: weight 1.0-2.0
- Novel exploration: weight 1.0

### Phase 3: Implement

Execute all changes atomically:

1. **Create prompt directory**:
   ```
   packages/training/src/pleno_ner_training/prompts/benchmark_v0X/{language}/
   ```

2. **Write all `.j2` template files** based on the design

3. **Update `generate_benchmark.py`**:
   - Add new `BenchmarkConfig` entry to `BENCHMARK_CONFIGS`
   - Do NOT modify existing version configs

4. **Update `evaluate_benchmark.py`**:
   - Add new version to `BENCHMARK_VERSIONS` list

5. **Update `Makefile`**:
   - Add `benchmark-v0X-generate`, `benchmark-v0X-generate-en`, `benchmark-v0X-evaluate`, `benchmark-v0X-evaluate-en` targets
   - Follow existing Makefile pattern exactly

### Phase 4: Generate & Validate

1. **Generate benchmark data**:
   ```bash
   cd packages/training
   dotenvx run -f ../../.env -- uv run python -m pleno_ner_training.generate_benchmark \
     --version v0.X.0 --language {language} \
     --docs-per-template 20 --batches-per-template 10
   ```

2. **Validate data quality**:
   - Total docs >= 500
   - Each template contributed docs (no empty templates)
   - Entity distribution covers all 5 types
   - Negative docs exist (for FP testing)
   - Alignment failure rate < 5%

3. **Evaluate model on new benchmark**:
   ```bash
   cd packages/training
   uv run python -m pleno_ner_training.evaluate_benchmark \
     --model output/ja-v02/model-best --language {language} --version v0.X.0
   ```

4. **Sanity check scores**:
   - New benchmark F1 should be LOWER than previous benchmark F1 (it's supposed to be harder)
   - If new benchmark F1 >= previous benchmark F1: the benchmark is not harder, redesign templates
   - If new benchmark F1 < 0.20: the benchmark may be broken (bad templates), investigate

### Phase 5: Finalize

1. **Log the evolution** to `packages/training/experiments/log.jsonl`:
   ```json
   {
     "id": "benchmark_v050_20260402",
     "timestamp": "2026-04-02T15:00:00+09:00",
     "type": "benchmark_evolution",
     "from_version": "v0.4.0",
     "to_version": "v0.5.0",
     "language": "ja",
     "templates_added": ["template1.j2", "template2.j2"],
     "templates_carried": ["boundary_ambiguity.j2"],
     "templates_dropped": ["dense_multi_entity.j2"],
     "model_score_old_benchmark": 0.72,
     "model_score_new_benchmark": 0.55,
     "difficulty_delta": -0.17,
     "rationale": "Model saturated on dense_multi_entity (92% F1). New templates target ORGANIZATION type confusion and cross-entity boundary errors."
   }
   ```

2. **Commit** all changes:
   ```
   feat: benchmark v0.X.0 - {one-line summary of new adversarial focus}
   ```

3. **Output evolution report**:
   ```
   === Benchmark Evolution Report ===
   Previous: v0.4.0 (model F1: XX.X%)
   New:      v0.5.0 (model F1: YY.Y%)
   Difficulty increase: -ZZ.Z%

   New templates:
     - template1.j2 (weight: X.X) — purpose
     - template2.j2 (weight: X.X) — purpose

   Carried forward:
     - boundary_ambiguity.j2 (weight: X.X)

   Dropped (solved):
     - dense_multi_entity.j2 (was XX% F1)

   Next: Run /ner-improve to close the gap on v0.5.0
   ```

## Constraints

- NEVER modify existing benchmark version data or prompts (v0.2.0, v0.3.0, v0.4.0 are immutable history)
- NEVER modify entity_types.py
- NEVER modify evaluate.py acceptance criteria
- New benchmark must be strictly harder than current (model F1 must drop)
- Working directory: `packages/training/`
- Use `dotenvx run -f ../../.env --` for OpenAI API calls
- Use `uv run` for all Python commands
- Template language must match the target language (ja templates in Japanese, en templates in English)
- Each template must produce valid XML-tagged output parseable by `parse_annotated_text()`

## Relationship with /ner-improve

These two skills form an infinite improvement spiral:

```
/ner-improve → model beats benchmark → /benchmark-evolve → harder benchmark → /ner-improve → ...
```

The `autotrain.sh` script can chain them:
```bash
./autotrain.sh ja 5          # improve model
./autotrain.sh ja 0 --evolve # evolve benchmark
./autotrain.sh ja 5          # improve against new benchmark
```
