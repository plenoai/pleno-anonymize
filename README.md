# pleno-anonymize

日本語対応 PII（個人情報）匿名化サーバー。カスタム日本語NERモデル + Presidio を使用。

- **Website:** https://plenoai.com/pleno-anonymize/
- **Playground:** https://plenoai.com/pleno-anonymize/playground
- **Benchmark:** https://plenoai.com/pleno-anonymize/benchmark
- **API Docs:** https://anonymize.plenoai.com/docs
- **Production API:** https://anonymize.plenoai.com

## 対応エンティティ

### NERモデル (ja_ner_ja)
| エンティティ | 説明 |
|---|---|
| `PERSON` | 人名 |
| `ADDRESS` | 住所 |
| `ORGANIZATION` | 組織名 |
| `DATE_OF_BIRTH` | 生年月日 |
| `BANK_ACCOUNT` | 銀行口座 |

### パターンベース
| エンティティ | 説明 |
|---|---|
| `EMAIL_ADDRESS` | メールアドレス |
| `PHONE_NUMBER` | 電話番号（全角/半角） |
| `MY_NUMBER` | マイナンバー |
| `CREDIT_CARD` | クレジットカード番号 |
| `PASSPORT` | パスポート番号 |
| `DRIVER_LICENSE` | 運転免許証番号 |
| `IP_ADDRESS` | IPアドレス |

## API

### `POST /api/analyze` - PII検出

```bash
curl -X POST https://anonymize.plenoai.com/api/analyze \
  -H "Content-Type: application/json" \
  -d '{"text": "佐藤太郎の電話番号は090-1234-5678です。", "language": "ja"}'
```

### `POST /api/redact` - PII匿名化

```bash
curl -X POST https://anonymize.plenoai.com/api/redact \
  -H "Content-Type: application/json" \
  -d '{"text": "佐藤太郎の電話番号は090-1234-5678です。", "language": "ja"}'
```

### `POST /api/openai/*` - OpenAI APIプロキシ

リクエスト内のPIIを自動マスキングしてOpenAI APIに送信し、レスポンスで復元します。

```bash
curl -X POST https://anonymize.plenoai.com/api/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_OPENAI_API_KEY" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "佐藤太郎さんについて教えて"}]}'
```

## プロジェクト構成

```
app/
  server/        # FastAPI バックエンド
  website/       # React フロントエンド (GitHub Pages)
packages/
  models/        # 日本語NERモデル (CC0-1.0)
  training/      # モデル訓練パイプライン
```

## 開発

```bash
uv sync
uv run uvicorn app.server.app:app --port 8080
```
