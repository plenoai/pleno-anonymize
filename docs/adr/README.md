# Architecture Decision Records

このディレクトリには、pleno-anonymizeプロジェクトのアーキテクチャ決定記録（ADR）が含まれています。

## ADR一覧

| ADR | タイトル | Status |
|-----|---------|--------|
| [0001](0001-aws-lambda-container-image.md) | AWS Lambda Container Image | Accepted |
| [0002](0002-api-url-structure.md) | API URL Structure | Accepted |
| [0003](0003-spacy-llm-presidio.md) | spaCy-LLM + Presidio for PII Detection | Accepted |
| [0004](0004-custom-ja-ner-model.md) | Custom Japanese NER Model | Accepted |
| [0004-invitely](0004-invitely-integration.md) | Invitely Integration | Accepted |
| [0005](0005-ginza-presidio-partial-supersede.md) | GiNZA + Presidio Partial Supersede of ADR-0004 | Superseded by ADR-0006 |
| [0006](0006-supersede-0005-with-phase2-numbers.md) | Supersede ADR-0005 with Phase 2 Measurement Triad | Proposed |

## ADRフォーマット

各ADRは以下の構造に従います:

- **Status**: Proposed / Accepted / Deprecated / Superseded
- **Context**: 決定が必要な背景
- **Decision**: 何を決定したか
- **Consequences**: 決定による影響（Positive/Negative）
