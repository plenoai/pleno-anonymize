# ADR-0004: カスタム日本語NERモデルによるPII検出

## ステータス

Accepted

## コンテキスト

ADR-0003で採用したspaCy-LLM + Presidioアーキテクチャでは、推論時にOpenAI APIへの毎回のラウンドトリップが必要であり、以下の課題があった:

- レイテンシ: API呼び出しによる数百ms〜数秒の遅延
- コスト: 従量課金の継続的な発生
- 日本語未対応: `lang = "en"` でハードコードされ日本語PII検出が機能しない
- オフライン不可: API障害時にサービス全体が停止

## 決定

GPT-5.4-miniで大量の日本語合成訓練データを生成し、spaCy + Transformerベースの独自NERモデルを訓練する。

### アーキテクチャ

- **NERモデル**: cl-tohoku/bert-base-japanese-v3をバックボーンとしたspaCy TransitionBasedParser
- **パターン認識**: Presidio PatternRecognizerによる正規表現ベースのPII検出
- **ハイブリッド**: 文脈依存エンティティ(人名、住所等)はNER、パターンベースエンティティ(電話番号、マイナンバー等)はRegexで分担

### エンティティ分類

| 担当 | エンティティ |
|------|------------|
| NER | PERSON, ADDRESS, ORGANIZATION, DATE_OF_BIRTH, BANK_ACCOUNT |
| Regex | EMAIL_ADDRESS, PHONE_NUMBER, MY_NUMBER, CREDIT_CARD, PASSPORT, DRIVER_LICENSE, IP_ADDRESS |

## 結果

### メリット
- オフライン推論: APIに依存せずローカルで完結
- 低レイテンシ: GPU/CPUでの推論はms単位
- 日本語ネイティブ: 日本語BERTと日本語固有パターンに最適化
- コスト削減: 推論時のAPI従量課金が不要

### デメリット
- 初期構築コスト: 訓練データ生成とモデル訓練に時間が必要
- モデルサイズ: BERT-base ~440MB (CNN版 ~50MBにフォールバック可能)
- メンテナンス: 新しいPIIパターンへの対応にはモデル再訓練が必要
