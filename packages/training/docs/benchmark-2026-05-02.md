# Benchmark Report — 2026-05-02

GiNZA + Presidio (OSS hybrid) vs custom NER (pleno-anonymize) honest baseline comparison.

- Plan: [docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md](../../../docs/plans/2026-05-02-001-feat-ginza-presidio-baseline-measurement-plan.md)
- Origin brainstorm: [docs/brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md](../../../docs/brainstorms/2026-05-02-ginza-presidio-baseline-comparison-requirements.md)
- Raw artifacts: `packages/training/experiments/artifacts/measure-2026-05-02/` and `measure-heldout-2026-05-02/`
- Log: `packages/training/experiments/log.jsonl` (entries `baseline_comparison_2026_05_02_v0_12_1`, `baseline_comparison_2026_05_02_v0_13_0_heldout`)

## Verdict (Pre-Registration locked rule)

| Entity | Corpus | Verdict |
|---|---|---|
| ORGANIZATION | v0.12.1-leak-fixed (in-domain, 495 docs) | NO_DECISION |
| DATE_OF_BIRTH | v0.12.1-leak-fixed (in-domain, 495 docs) | NO_DECISION |
| ORGANIZATION | v0.13.0-held-out (held-out, 80 docs) | NO_DECISION |
| DATE_OF_BIRTH | v0.13.0-held-out (held-out, 80 docs) | NO_DECISION |

Rule-strict 4× NO_DECISION. Operational signal (separate field, not used for verdict) supports custom-side COMMIT — see "Operational signal" below.

## Held-out (v0.13.0, 80 docs, 3 unseen templates)

Templates excluded from training: `ocr_forms_a`, `logistics_labels_b`, `partially_redacted_public_a`.

### ORGANIZATION (16 gold spans, 5 eligible templates)

| Variant | Precision | Recall (overlap) | Recall (strict) | Notes |
|---|---|---|---|---|
| ja_core_news_trf + Presidio | < 0.7 | — | — | matched-precision floor not met |
| ja_ginza + Presidio | < 0.7 | — | — | matched-precision floor not met |
| ja_core_news_md + Presidio | < 0.7 | — | — | matched-precision floor not met |
| **custom_cnn (pleno-anonymize)** | **1.000** | **0.875** | **0.875** | **0 FP / 14 TP / 2 FN** |

OSS side returns no comparable variant — all three Japanese spaCy models drop below the p ≥ 0.7 budget on this slice. custom_cnn is the only model that produces a usable ORG signal.

### DATE_OF_BIRTH (10 gold spans, 1 eligible template)

| Variant | Precision | Recall | Notes |
|---|---|---|---|
| ja_core_news_trf + Presidio | < 0.7 | — | floor not met |
| **ja_ginza + Presidio** | **0.909** | **1.000** | only OSS variant clearing the floor |
| ja_core_news_md + Presidio | < 0.7 | — | floor not met |
| **custom_cnn (pleno-anonymize)** | **1.000** | **1.000** | perfect score |

mean_diff (ja_ginza − custom_cnn) = 0.0, bootstrap CI = (0.0, 0.0) → tied under the locked rule. Operationally custom_cnn wins on precision (+9.1pt).

## In-domain (v0.12.1-leak-fixed, 495 docs)

### ORGANIZATION (37 spans, 4 eligible templates)

| Variant | Precision | Recall (overlap) | Notes |
|---|---|---|---|
| OSS (best) | < 0.7 | — | floor not met |
| **custom_cnn** | **0.919** | **0.919** | 34 TP / 3 FP / 3 FN |

### DATE_OF_BIRTH (23 spans, 3 eligible templates)

| Variant | Precision | Recall (overlap) | Notes |
|---|---|---|---|
| **ja_ginza** | **0.958** | **1.000** | tied with custom on overlap |
| **custom_cnn** | **0.958** | **1.000** | tied |

mean_diff = 0.0, CI = (0.0, 0.0) → rule-strict tied.

## Generalization (in-domain → held-out)

| Entity | In-domain custom_cnn | Held-out custom_cnn | Δ |
|---|---|---|---|
| ORGANIZATION precision | 0.919 | 1.000 | +0.081 |
| ORGANIZATION recall | 0.919 | 0.875 | -0.044 |
| DATE_OF_BIRTH precision | 0.958 | 1.000 | +0.042 |
| DATE_OF_BIRTH recall | 1.000 | 1.000 | 0 |

Held-out scores do not collapse — recall drops 4.4pt on ORG while precision rises 8.1pt. The "model has only memorized training templates" hypothesis is empirically rejected for this entity set.

## Operational signal (informational, not part of locked verdict)

Each verdict block in the artifacts carries an `operational_signal` field. Across both corpora and both entities, the signal supports custom-side COMMIT:

- ORGANIZATION: only custom_cnn produces a usable score in either corpus. OSS hybrid is not a viable substitute for ORG redaction.
- DATE_OF_BIRTH: ja_ginza ties on overlap recall but loses on strict precision in held-out (-9.1pt). custom_cnn dominates on precision while matching recall.

The locked rule still records NO_DECISION because the matched-precision floor (p ≥ 0.7) blocks ORG OSS variants from entering the comparison and DOB CI = (0, 0). This is by design — it prevents "OSS lost on a budget it never reached" from auto-killing the custom path.

## Follow-on

- S1 brainstorm: redesign primary metric from F1 to recall@FP-budget (per plan, commit-path follow-on)
- ADR-0005: fill in `<!-- 結果記入: -->` placeholders with these numbers
- ADR-0004 補注: separate post-measurement PR
- custom_bert (5th variant): RunPod training pending; comparison will rerun once the artifact lands

## Reproducibility

```bash
# In-domain (v0.12.1-leak-fixed)
make verify-leakage-v12      # F0a — leakage check
make pin-noise-floor-v12     # F0c — bootstrap noise floor
make compare-baselines-v12   # F1 + F2 — comparison + verdict

# Held-out (v0.13.0)
# Same Makefile targets with CORPUS=v0.13.0-held-out
```

Manifest hashes:
- v0.12.1-leak-fixed: see `packages/training/data/benchmark/v0.12.1-leak-fixed/ja/training_corpus_manifest.json`
- v0.13.0-held-out: see `packages/training/data/benchmark/v0.13.0-held-out/ja/training_corpus_manifest.json`
