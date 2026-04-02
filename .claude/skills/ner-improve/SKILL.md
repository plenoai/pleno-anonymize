---
name: ner-improve
description: |
  NERモデルを自律的に改善する実験ループ。
  ベンチマークスコア分析→仮説生成→学習→評価→判定を繰り返す。

  Trigger: ner-improve, NER改善, モデル改善, 精度向上, F1改善
---

# NER Autonomous Improvement Loop

Inspired by [karpathy/autoresearch](https://github.com/karpathy/autoresearch): autonomous experimentation loop for continuously strengthening the NER model.

## Trigger

ner-improve, NER改善, モデル改善, 精度向上, F1改善

## Invocation

`/ner-improve [language] [max-iterations]`

- language: `ja` (default) or `en`
- max-iterations: number of improvement cycles (default: 3)

Example: `/ner-improve ja 5`

## Context

This project trains spaCy NER models for PII detection (PERSON, ADDRESS, ORGANIZATION, DATE_OF_BIRTH, BANK_ACCOUNT).

### Current Pipeline
```
generate_data → augment → convert_to_docbin → spacy train → evaluate → evaluate_benchmark
```

### Key Paths
- Training package: `packages/training/`
- Prompts: `packages/training/src/pleno_ner_training/prompts/`
- Training configs: `packages/training/configs/`
- Benchmark data: `packages/training/data/benchmark/{version}/{language}/`
- Benchmark scores: `packages/training/data/benchmark/{version}/{language}/scores.json`
- Raw data: `packages/training/data/raw/{language}/`
- Processed data: `packages/training/data/processed/{language}/`
- Model output: `packages/training/output/`
- Experiment log: `packages/training/experiments/log.jsonl`
- Makefile: `packages/training/Makefile`

### Acceptance Criteria (from evaluate.py)
| Entity | F1 Threshold | Recall Minimum |
|---|---|---|
| PERSON | 0.90 | 0.85 |
| ADDRESS | 0.85 | 0.80 |
| ORGANIZATION | 0.85 | 0.80 |
| DATE_OF_BIRTH | 0.80 | 0.75 |
| BANK_ACCOUNT | 0.80 | 0.75 |
| **Overall F1** | **0.88** | - |

## Instructions

You are an autonomous NER research agent. Execute the improvement loop below. Each iteration is a single, atomic experiment with a clear hypothesis and measurable outcome.

### Phase 0: Baseline Assessment

1. Read ALL benchmark scores from `packages/training/data/benchmark/*/[language]/scores.json`
2. Read the latest experiment log if it exists: `packages/training/experiments/log.jsonl`
3. Compute the gap between current performance and acceptance criteria
4. Rank entity types by improvement priority:
   - **Gap size** = threshold - current F1 (larger gap = higher priority)
   - **Leakage risk** = recall_minimum - current recall (positive = critical)
   - Leakage risks take absolute priority over F1 gaps

Output a structured analysis table before proceeding.

### Phase 1: Hypothesis Generation

Based on the weakness analysis, generate exactly ONE hypothesis for improvement. The hypothesis must be:

- **Specific**: "Add 500 augmented ORGANIZATION examples with honorific-prefixed company names" not "improve ORGANIZATION detection"
- **Measurable**: predict the expected F1 delta (e.g., "+3-5% ORGANIZATION F1")
- **Atomic**: change exactly one variable (prompts, augmentation, config, or data mix)

Categories of interventions (try in this order):
1. **Training data quality** - fix annotation errors, improve prompt templates
2. **Data augmentation** - add targeted examples for weak entity types
3. **Training data quantity** - generate more data for underrepresented patterns
4. **Training config** - adjust hyperparameters (learning rate, epochs, architecture width)
5. **Benchmark prompts** - create harder benchmark cases to expose new weaknesses

Log the hypothesis to experiment log, then proceed immediately. Do NOT ask for user approval — this is a fully autonomous loop.

### Phase 2: Experiment Execution

Execute the experiment immediately:

1. **Backup model-best**: Before overwriting, always save the current best model
   ```bash
   cd packages/training
   EXPERIMENT_ID=$(date +%Y%m%d_%H%M%S)_$(echo $HYPOTHESIS | head -c 20 | tr ' ' '_')
   # Save current best model for rollback
   if [ -d output/ja-v02/model-best ]; then
     tar czf /tmp/model-best-backup-${EXPERIMENT_ID}.tar.gz output/ja-v02/model-best/
     echo "Backed up model-best to /tmp/model-best-backup-${EXPERIMENT_ID}.tar.gz"
   fi
   ```

2. **Implement**: Make the specific change (new prompts, augmented data, config change)

3. **Train**: Run the appropriate make target
   ```bash
   cd packages/training && make train-v02  # or appropriate target
   ```
   Training is the bottleneck. Use CNN config for rapid iteration (~5-10 min).
   Use cloud (RunPod CPU5 or vast.ai) for faster training. See cloud notes below.

4. **Evaluate**: Run BOTH test evaluation and benchmark evaluation
   ```bash
   cd packages/training && make evaluate-v02
   cd packages/training && make benchmark-v03-evaluate  # and v0.4.0, v0.5.0
   ```

### Phase 3: Judgment

Compare results against the PREVIOUS best scores:

1. Read new scores from `output/*/scores.json` and `data/benchmark/*/[language]/scores.json`
2. Compute deltas for every metric (per-entity F1, recall, precision, overall F1)
3. Apply the decision rule:

   **KEEP** if ALL of:
   - Overall benchmark F1 improved OR stayed within -0.5%
   - No entity recall dropped below its minimum threshold
   - No entity F1 regressed by more than 2%

   **DISCARD** otherwise.

4. Log the result (see Experiment Log Format below)

If KEEP: commit the changes with message `exp: {hypothesis} → F1 {old}→{new}`
If DISCARD: restore model-best from backup and re-evaluate to confirm scores match
   ```bash
   rm -rf output/ja-v02/model-best
   tar xzf /tmp/model-best-backup-${EXPERIMENT_ID}.tar.gz
   # Re-evaluate to restore scores.json
   ```

### Phase 4: Loop or Stop

- If iteration count < max-iterations AND overall F1 < 0.88: go to Phase 1
- If overall F1 >= 0.88: declare success, output final report
- If 3 consecutive experiments were DISCARD: switch intervention category and continue (do NOT ask user)

### Experiment Log Format

Append to `packages/training/experiments/log.jsonl` (create if missing):

```json
{
  "id": "20260402_143000_add_org_examples",
  "timestamp": "2026-04-02T14:30:00+09:00",
  "hypothesis": "Add 500 augmented ORGANIZATION examples with honorific prefixes",
  "intervention_type": "data_augmentation",
  "language": "ja",
  "changes": ["packages/training/src/pleno_ner_training/augment_ja_data.py"],
  "metrics_before": {"overall_f1": 0.584, "ORGANIZATION_f1": 0.445},
  "metrics_after": {"overall_f1": 0.601, "ORGANIZATION_f1": 0.512},
  "delta": {"overall_f1": "+0.017", "ORGANIZATION_f1": "+0.067"},
  "verdict": "KEEP",
  "reason": "ORGANIZATION F1 improved by 6.7%, no regressions",
  "duration_minutes": 12
}
```

### Final Report

After all iterations, output:

```
=== NER Improvement Report ===
Language: ja
Iterations: N
Starting F1: X.XX%
Final F1: Y.YY%
Delta: +Z.ZZ%

Entity Progress:
  PERSON:       X% → Y% (target: 90%)
  ADDRESS:      X% → Y% (target: 85%)
  ORGANIZATION: X% → Y% (target: 85%)
  DATE_OF_BIRTH: X% → Y% (target: 80%)
  BANK_ACCOUNT:  X% → Y% (target: 80%)

Experiments:
  1. [KEEP] hypothesis → +X% overall
  2. [DISCARD] hypothesis → reason
  ...

Next Steps:
  - Recommended next interventions
  - Remaining gaps to acceptance criteria
```

## Constraints

- NEVER modify benchmark data or evaluation scripts (those are the "fixed ruler")
- NEVER modify entity_types.py (entity definitions are stable)
- Working directory for all commands: `packages/training/`
- Use `dotenvx run -f ../../.env --` prefix for commands that call OpenAI API
- Use `uv run` for all Python commands
- Prefer CNN config (`train_cnn.cfg`) for rapid iteration; transformer config only for final validation
- Each experiment should complete in < 15 minutes (training time budget)
- Always evaluate on the LATEST benchmark version (v0.4.0) as the primary metric
