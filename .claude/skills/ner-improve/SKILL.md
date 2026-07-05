---
name: ner-improve
description: |
  NER モデルを自律的に改善する実験ループ。
  ai4privacy/pii-masking-300k をベースラインに、仮説生成→学習→評価→判定を繰り返す。

  Trigger: ner-improve, NER改善, モデル改善, 精度向上, F1改善
---

# NER Autonomous Improvement Loop

Inspired by [karpathy/autoresearch](https://github.com/karpathy/autoresearch): autonomous experimentation loop for continuously strengthening the NER model against a **public, fixed baseline**.

## Trigger

ner-improve, NER改善, モデル改善, 精度向上, F1改善

## Invocation

`/ner-improve [language] [max-iterations]`

- language: `en` (default) / `fr` / `de` / `it` / `es` / `nl`
- max-iterations: number of improvement cycles (default: 3)

Example: `/ner-improve en 5`

> **Note on Japanese.** `ai4privacy/pii-masking-300k` contains **zero
> Japanese rows** (verified by enumerating the entire validation split
> and by the HF dataset card's `cardData.language` field). Japanese
> coverage is tracked separately via internal held-out corpora and is
> **not** driven by this loop. Run this loop on one of the dataset's
> supported languages; JP improvements ship behind a separate workflow.

## Context

This loop trains the NER components that back `pleno-anonymize` and measures every change against a public, immutable baseline so improvements are externally verifiable.

### Baseline (fixed ruler)

| Item | Value |
|---|---|
| Dataset | [`ai4privacy/pii-masking-300k`](https://huggingface.co/datasets/ai4privacy/pii-masking-300k) |
| Split | `validation` |
| Languages | English (primary), French, German, Italian, Spanish, Dutch |
| Sample size | `--limit 300` (primary); `--limit 50` (pilot, retained for traceability) |
| Scoring | character-IoU ≥ 0.5, **label-agnostic** span matching (see `eval_pii_masking_300k.py`) |
| Metric | overall F1 (primary), precision/recall, per-label recall, avg latency/doc |
| Reference report | [`docs/benchmark.md`](../../../docs/benchmark.md) |

The self-made benchmarks under `packages/training/data/benchmark/v0.*/` are **frozen as of v0.13.0** and kept for historical traceability only. Do not generate new versions; do not use them as the primary metric for this loop.

### Current pipeline

```
generate_data / augment → convert_to_docbin → spacy train → evaluate → eval_pii_masking_300k.py
```

### Key paths

- Training package: `packages/training/`
- Prompts: `packages/training/src/pleno_ner_training/prompts/`
- Training configs: `packages/training/configs/`
- Raw / processed training data: `packages/training/data/raw/{language}/`, `data/processed/{language}/`
- Model output: `packages/training/output/`
- Experiment log: `packages/training/experiments/log.jsonl`
- Baseline evaluator: `packages/sdk/scripts/eval_pii_masking_300k.py`
- Makefile: `packages/training/Makefile`

### Acceptance criteria (vs. ai4privacy/pii-masking-300k, EN validation, n = 300, IoU ≥ 0.5)

| Tier | Overall F1 | Notes |
|---|---:|---|
| **Smoke** | ≥ 0.50 | first meaningful improvement over the current `builtin` baseline (~0.315) |
| **Parity** | ≥ 0.82 | matches `openai-privacy-filter` on the same dataset |
| **Stretch** | ≥ 0.88 | exceeds OPF; ship as new default backend |

Per-label recall floors (any label with ≥ 30 gold spans in the sample):

| Label | Recall minimum |
|---|---:|
| `EMAIL` / `PHONE_NUMBER` / `IP` | 0.95 |
| `LASTNAME*` / `FIRSTNAME*` | 0.85 |
| All other labels with ≥ 30 gold spans | 0.70 |

Leakage-style regressions (any label dropping below its recall floor) override F1 gains — see Phase 3.

## Instructions

You are an autonomous NER research agent. Execute the improvement loop below. Each iteration is one atomic experiment with a clear hypothesis and a measurable outcome on the **public baseline**.

### Phase 0: Baseline assessment

1. Run the baseline evaluator on the current model:
   ```bash
   cd /Users/hikae/ghq/github.com/plenoai/pleno-anonymize
   uv run --with datasets python packages/sdk/scripts/eval_pii_masking_300k.py \
     --engines builtin \
     --language English --pleno-language en \
     --limit 300 \
     --output output/pii-300k-eval-en-300.json
   ```
2. Read the latest experiment log if it exists: `packages/training/experiments/log.jsonl`
3. Compute the gap between current overall F1 / per-label recall and the acceptance tiers above
4. Rank labels by improvement priority:
   - **Gap to recall floor** (positive = critical / leakage)
   - **Gap to parity F1** (Δ = 0.82 − current overall F1)
   - Leakage-style gaps take absolute priority over F1 gaps

Output a structured analysis table before proceeding.

### Phase 1: Hypothesis generation

Based on the weakness analysis, generate exactly ONE hypothesis for improvement. The hypothesis must be:

- **Specific**: "Add 1000 augmented `EMAIL_ADDRESS` examples with subaddress/plus-tag variants" — not "improve EMAIL recall"
- **Measurable**: predict the expected F1 / recall delta on the baseline (e.g., "+3-5% EMAIL recall, +1% overall F1")
- **Atomic**: change exactly one variable (prompts, augmentation, config, or training-data mix)

Intervention categories (try in this order):

1. **Recognizer / regex coverage** — add or refine Presidio recognizers for structured IDs the baseline labels but `builtin` misses
2. **Training data quality** — fix annotation errors, improve prompt templates
3. **Data augmentation** — add targeted examples for the weakest label
4. **Training data quantity** — generate more data for underrepresented patterns
5. **Training config** — adjust hyperparameters (learning rate, epochs, architecture width)
6. **Label mapping** — fix backend → pleno taxonomy edges (only if the gap is provably a mapping bug, not a model bug)

Do NOT propose changes to the baseline dataset, the evaluator, the IoU threshold, or the self-made (frozen) benchmark.

Log the hypothesis to the experiment log, then proceed immediately. Do NOT ask for user approval — this is a fully autonomous loop.

### Phase 2: Experiment execution

1. **Backup current model** before overwriting:
   ```bash
   cd packages/training
   EXPERIMENT_ID=$(date +%Y%m%d_%H%M%S)_$(echo "$HYPOTHESIS" | head -c 20 | tr ' ' '_')
   if [ -d output/en-transformer/model-best ]; then
     tar czf /tmp/model-best-backup-${EXPERIMENT_ID}.tar.gz output/en-transformer/model-best/
   fi
   ```

2. **Implement** the specific change (recognizer, prompts, augmented data, config).

3. **Train** the language being improved:
   ```bash
   cd packages/training && make train-en   # or the appropriate Makefile target
   ```
   Use the CNN config for rapid iteration (~5–10 min). Use RunPod via the `mcp__runpod__*` MCP tools (`create-pod`, `start-pod`, `get-pod`, `delete-pod`) for GPU training (per CLAUDE.md: do **not** train on the local machine).

4. **Evaluate against the baseline** (the only metric that gates Phase 3):
   ```bash
   cd /Users/hikae/ghq/github.com/plenoai/pleno-anonymize
   uv run --with datasets python packages/sdk/scripts/eval_pii_masking_300k.py \
     --engines builtin \
     --language English --pleno-language en \
     --limit 300 \
     --output output/pii-300k-eval-en-300-${EXPERIMENT_ID}.json
   ```

   The in-repo unit-style eval (`packages/training/Makefile :: evaluate-en`) may be run for regression visibility, but **the public baseline is the verdict-bearing metric**.

### Phase 3: Judgment

Compare results against the PREVIOUS best baseline run.

1. Read new scores from `output/pii-300k-eval-en-300-*.json`.
2. Compute deltas: overall F1 / P / R, per-label recall, avg latency/doc.
3. Apply the decision rule:

   **KEEP** if ALL of:
   - Overall baseline F1 improved (Δ ≥ 0)
   - No labeled recall dropped below its acceptance-criteria floor
   - Avg latency/doc did not regress by more than 25 %

   **DISCARD** otherwise.

4. Log the result (see Experiment log format below).

If **KEEP**: commit with message `exp: {hypothesis} → F1 {old}→{new}`.
If **DISCARD**: restore model-best from backup and re-evaluate to confirm scores match.

### Phase 4: Loop or stop

- If iteration count < max-iterations AND overall baseline F1 < the next tier (Smoke → Parity → Stretch): go to Phase 1
- If overall F1 ≥ 0.88 (Stretch): declare success, output final report
- If 3 consecutive experiments were DISCARD: switch intervention category and continue (do NOT ask the user)

### Experiment log format

Append to `packages/training/experiments/log.jsonl` (create if missing):

```json
{
  "id": "20260512_143000_add_email_subaddress_aug",
  "timestamp": "2026-05-12T14:30:00+09:00",
  "hypothesis": "Add 1000 EMAIL subaddress/plus-tag examples",
  "intervention_type": "data_augmentation",
  "language": "en",
  "baseline": "ai4privacy/pii-masking-300k @ validation, n=300, IoU=0.5",
  "changes": ["packages/training/src/pleno_ner_training/augment_en_data.py"],
  "metrics_before": {"overall_f1": 0.318, "EMAIL_recall": 0.71},
  "metrics_after":  {"overall_f1": 0.341, "EMAIL_recall": 0.93},
  "delta":          {"overall_f1": "+0.023", "EMAIL_recall": "+0.22"},
  "verdict": "KEEP",
  "reason": "EMAIL recall reached floor (within sample noise); overall F1 up 2.3pt",
  "duration_minutes": 12
}
```

### Final report

After all iterations, output:

```
=== NER Improvement Report ===
Language: en
Baseline: ai4privacy/pii-masking-300k @ validation, n=300, IoU=0.5
Iterations: N
Starting F1: X.XX
Final F1:    Y.YY
Delta:       +Z.ZZ

Recall floors:
  EMAIL          X.XX → Y.YY  (floor 0.95) [PASS|FAIL]
  PHONE_NUMBER   X.XX → Y.YY  (floor 0.95) [PASS|FAIL]
  …

Experiments:
  1. [KEEP]    hypothesis → +X.XX overall F1
  2. [DISCARD] hypothesis → reason
  …

Next steps:
  - Recommended next interventions
  - Remaining gaps to the next acceptance tier
```

## Constraints

- **Never** modify the baseline dataset, the evaluator (`packages/sdk/scripts/eval_pii_masking_300k.py`), or the IoU threshold — the public ruler is fixed.
- **Never** modify the self-made benchmark data under `packages/training/data/benchmark/v0.*/` — frozen as of v0.13.0.
- **Never** modify `packages/training/src/pleno_ner_training/entity_types.py` (entity definitions are stable).
- **Never** train on `ai4privacy/pii-masking-300k` or its local dumps (`data/raw/*-300k-supervised/`) — evaluation-only license; derivative models require written permission.
- Working directory for training commands: `packages/training/`.
- Use `dotenvx run -f ../../.env --` for commands that call the OpenAI API.
- Use `uv run` for all Python commands.
- Train on **RunPod** via the RunPod MCP (`mcp__runpod__create-pod` / `start-pod` / `get-pod` / `delete-pod`). Do not train on the local machine (per project CLAUDE.md).
- Each experiment should complete in < 15 minutes of training time. If a hypothesis cannot be evaluated in that budget, split it.
- Always report the public baseline number when claiming an improvement.
