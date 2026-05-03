# pleno-anonymize

日本語に強い PII 匿名化基盤。2 つの使い方で同じ NER + Presidio パイプラインを共有する。

- **`pleno-anonymize` server** — `/api/analyze` `/api/redact` と OpenAI/Anthropic/Gemini プロキシを公開する HTTP サービス
- **`pleno-scan` CLI** — リポジトリやコミット履歴を走査して PII を検出する gitleaks/trufflehog 風スキャナー

エンドポイント: https://pleno-anonymize.fly.dev (`/docs` で API リファレンス)
プレイグラウンド: https://plenoai.com/pleno-anonymize/playground

## サービスを使う (HTTP)

```bash
curl -X POST https://pleno-anonymize.fly.dev/api/analyze \
  -H "content-type: application/json" \
  -d '{"text":"連絡先 山田太郎 090-1234-5678","language":"ja"}'
```

LLM プロキシ経由で送ると、プロバイダに到達する前に PII をマスクし、レスポンスで元の値に復元する。

| エンドポイント | 上流 |
|---|---|
| `POST /api/openai/*` | OpenAI Chat Completions / Responses |
| `POST /api/anthropic/*` | Anthropic Messages |
| `POST /api/gemini/*` | Google Gemini |

## リポジトリをスキャンする (CLI)

```bash
uv sync                                   # ja_ner_ja モデル含めてセットアップ
uv run pleno-scan dir ./my-repo           # ディレクトリ
uv run pleno-scan git ./my-repo           # working tree + commit 履歴
uv run pleno-scan github owner/repo       # clone してスキャン
uv run pleno-scan protect                 # pre-commit (staged hunks のみ)
```

サーバを別マシンで動かしている場合は `--base-url` でオフロードできる:

```bash
uv run pleno-scan dir ./my-repo --base-url https://pleno-anonymize.fly.dev
```

詳細は [`packages/scanner/README.md`](packages/scanner/README.md)。

## 検出できる PII

| 種別 | 検出方法 | エンティティ |
|---|---|---|
| 自由文 | spaCy NER (`ja_ner_ja`) + Presidio | `PERSON` `ADDRESS` `ORGANIZATION` `DATE_OF_BIRTH` `BANK_ACCOUNT` |
| 構造化 | 正規表現 + checksum (Luhn / マイナンバー / 法人番号) | `PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL` |

サーバ・CLI どちらも同じレジストリを読み込むため、同じ入力に対して同じエンティティ集合を返す。

## セルフホスト

```bash
docker build -t pleno-anonymize .
docker run -p 8080:8080 pleno-anonymize
```

`fly.toml` 同梱。`flyctl deploy --local-only` でそのまま fly.io に上がる。

## ライセンス

[AGPL-3.0](LICENSE) · [Privacy Policy](docs/PRIVACY.md) · [DPIA](docs/DPIA.md)
