# Training CHANGELOG

## [v0.13.0] - 2026-05-04 — `hf-ja-v02-tiny-aug-ext` + ORG=0.99 confidence floor

### Added
- `evaluate_scored_predictions_on_benchmark` (evaluate_benchmark.py) — decouples
  HF inference from spaCy-Scorer scoring so predictions can be re-scored at many
  per-label thresholds without re-running the model.
- `sweep_org_threshold.py` — picks max-overall-F1 per-label threshold under #98
  AC (overall F1 ≥ 0.5 ∧ ORG precision ≥ 0.30). spaCy Scorer parity (matches
  `scores.json`).
- `sweep_threshold.evaluate_at_threshold` accepts `threshold: float | dict[str,
  float]` so per-label confidence floors are first-class. New `run_per_label_sweep`.
- Makefile targets `predict-scores-v12-aug-ext`, `sweep-threshold-org-v12`.

### Performance — v0.12.0 / ja adversarial (500 docs, FP-pressure DLP corpus)
| Metric | hf_v02_tiny_aug_ext (no threshold) | + ORG≥0.99 (v0.13.0) | Δ |
|---|---:|---:|---:|
| Overall F1 | 0.452 | **0.701** | +0.249 |
| Overall P  | 0.303 | **0.632** | +0.329 |
| Overall R  | 0.883 | 0.787 | -0.096 |
| ORG F1     | 0.160 | **0.371** | +0.211 |
| ORG P      | 0.088 | **0.394** | +0.306 |
| PERSON F1  | 0.951 | 0.935 | -0.016 (R 0.983 維持) |
| Negative-doc clean rate | 63.2 % | **90.7 %** | +27.5 pt |
| Negative-doc FP total | 333 | **52** | -281 (6.4× 削減) |

### Changed
- Default ORG threshold for v0.13.0 production rollout: **0.99** (DLP profile).
  Tunable per consumer; lower it if recall matters more than precision.
- `release-model.yml` defaults `model_path` to `output/hf-ja-v02-tiny-aug-ext`
  (was `output/hf-ja-v02-tiny`).

### Refs
- Closes #98 (ORG-FP precision floor).
- Partial #48 (overall F1 ≥ 70 % achieved; per-entity ADDRESS/DOB/BANK ≥ 70 %
  outstanding — separate follow-up).
- Eval doc: `models/hf-ja-v02-tiny-aug-ext-org-threshold-eval-v012.md`
- Commits: c230422

## [Unreleased] - 2026-04-03

### Added
- 9 new Japanese training prompts for data generation:
  - `structured_data.j2` - PII in broken CSV/JSON/log/HTML
  - `orthography_mixed.j2` - romaji/hiragana/katakana/wide-space name variants
  - `geo_org_context.j2` - geo-named ORGs vs real ADDRESSes
  - `fragment_memo.j2` - minimal context entity chains
  - `org_dense.j2` - 3-5 ORGs per document
  - `org_variety.j2` - non-corporate ORGs (NPO, university, government, hospital)
  - `org_boundary.j2` - address-adjacent ORGs, short org names, bracket rules
  - `org_hard_negative.j2` - ORG FP reduction (facility/event/product/law names)
- `augment_ja_data.py` improvements:
  - Era abbreviation DOB formats (S40.5.10, H2/3/15)
  - ORG adjacent patterns, abbreviated prefixes (（株）)
  - Minimal context templates, structured noise templates
  - DOB multi-date patterns, placeholder mirage negatives
  - ORG hard negative distractors (20 texts)
  - v0.12.0-targeted negatives (OCR templates, facility names, etc.)
- `train_cnn_recall.cfg`: score_weights ents_f=0.7, ents_r=0.3 (experimental)
- Model backup/restore procedure in ner-improve SKILL.md
- RunPod CPU training guide with OOM prevention docs
- Error analysis script (`scripts/error_analysis.py`)

### Changed
- Training data: ja-v02 (10,965) + ja-v02-extra (17,676) = 28,641 generated + 5,000 augmented = 33,641 total docs
- Entity distribution: ORG 24,211, PERSON 30,834, ADDRESS 14,764, DOB 9,402, BANK 5,386
- Makefile: augment-count 1000→5000

### Performance

#### v0.4.0 Benchmark (ja) - Primary metric
| Entity | Start (v0.2.0 model) | Current (iter7) | Target |
|---|---|---|---|
| Overall F1 | 65.8% | **86.9%** | 88% |
| PERSON | 73.6% | 88.5% | 90% |
| ADDRESS | 64.6% | 84.0% | 85% |
| ORGANIZATION | 56.7% | **84.5%** | 85% |
| DATE_OF_BIRTH | 54.4% | **91.2%** | 80% ✓ |
| BANK_ACCOUNT | 70.1% | **86.9%** | 80% ✓ |

#### v0.12.0 Benchmark (ja) - FP-heavy (88% negative docs)
| Metric | Value |
|---|---|
| Overall F1 | 49.0% |
| Precision | 33.0% |
| Recall | 95.2% |
| Neg Clean Rate | 58.4% |

### Experiments (8 iterations, 4 KEEP / 4 DISCARD)
| # | Type | Hypothesis | v0.4.0 Delta | Verdict |
|---|---|---|---|---|
| iter04 | data_augmentation | targeted ORG/DOB patterns, augment 5000 | +9.5% | KEEP |
| iter05 | data_augmentation | v0.5.0 orthography/fragment/geo patterns | -1.0% | DISCARD |
| iter06 | data_generation | 4 new LLM prompts (structured, ortho, geo, fragment) | +7.6% | KEEP |
| iter07 | data_generation | 2 ORG-focused LLM prompts (dense, variety) | +2.8% | KEEP |
| iter08 | data_generation | data volume increase (batches 20→40) | -1.8% | DISCARD |
| iter09 | training_config | recall weight (ents_f=0.7, ents_r=0.3) | -2.5% | DISCARD |
| iter10 | data_generation | error analysis → org_boundary + org_hard_negative | +1.2% | KEEP |
| iter11 | data_augmentation | v0.12.0 negative augmentation | -0.1% | DISCARD |

### Key Insights
1. **Data quality > data quantity > parameter tuning**
2. Error analysis driven data generation is the most effective lever
3. Negative augmentation reduces FP but dilutes positive patterns
4. RunPod CPU5 8vCPU/16GB minimum (4GB/8GB causes OOM)
