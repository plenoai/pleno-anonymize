# Training CHANGELOG

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
