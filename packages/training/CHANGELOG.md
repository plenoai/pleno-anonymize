# Training CHANGELOG

## [Unreleased] - 2026-04-02

### Added
- 6 new Japanese training prompts for data generation:
  - `structured_data.j2` - PII in broken CSV/JSON/log/HTML
  - `orthography_mixed.j2` - romaji/hiragana/katakana/wide-space name variants
  - `geo_org_context.j2` - geo-named ORGs vs real ADDRESSes
  - `fragment_memo.j2` - minimal context entity chains
  - `org_dense.j2` - 3-5 ORGs per document
  - `org_variety.j2` - non-corporate ORGs (NPO, university, government, hospital)
- `augment_ja_data.py`: era abbreviation DOB formats (S40.5.10, H2/3/15), ORG adjacent patterns, abbreviated prefixes (（株）), minimal context templates, structured noise templates, DOB multi-date patterns
- `Makefile`: augment-count 1000→5000
- `train_cnn_recall.cfg`: score_weights ents_f=0.7, ents_r=0.3
- Model backup/restore procedure in ner-improve SKILL.md

### Changed
- Training data: ja-v02 (10,965) + ja-v02-extra (12,202) = 23,167 generated + 5,000 augmented = 28,167 total docs
- Entity distribution: ORG 18,216, PERSON 25,791, ADDRESS 12,165, DOB 8,123, BANK 4,407

### Performance (v0.4.0 benchmark, ja)
| Entity | v0.2.0 model | Current |
|---|---|---|
| Overall F1 | 65.8% | 85.7% |
| PERSON | 73.6% | 90.7% |
| ADDRESS | 64.6% | 83.6% |
| ORGANIZATION | 56.7% | 78.5% |
| DATE_OF_BIRTH | 54.4% | 87.6% |
| BANK_ACCOUNT | 70.1% | 82.8% |

### Experiments
| # | Type | Hypothesis | v0.4.0 Delta | Verdict |
|---|---|---|---|---|
| iter04 | data_augmentation | targeted ORG/DOB patterns, augment 5000 | +9.5% | KEEP |
| iter05 | data_augmentation | v0.5.0 orthography/fragment/geo patterns | -1.0% | DISCARD |
| iter06 | data_generation | 4 new LLM prompts (structured, ortho, geo, fragment) | +7.6% | KEEP |
| iter07 | data_generation | 2 ORG-focused LLM prompts (dense, variety) | +2.8% | KEEP |
| iter08 | data_generation | data volume increase (batches 20→40) | -1.8% | DISCARD |
