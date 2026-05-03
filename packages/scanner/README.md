# pleno-scan

リポジトリ・コミット履歴・staged hunk から日本語 PII を検出する CLI。

## セットアップ

```sh
uv sync
```

`ja_ner_ja` モデルも依存に含まれているので追加手順は不要。

## サブコマンド

```sh
pleno-scan dir <path>           # ディレクトリを走査
pleno-scan git <path>           # working tree + commit 履歴
pleno-scan github <owner>/<repo>  # shallow clone して走査
pleno-scan github --org <org>     # gh CLI で org の全 repo を列挙して走査
pleno-scan baseline <path>      # 現状の検出を suppression list として保存
pleno-scan protect              # staged hunks だけ走査 (pre-commit hook 用)
```

## ローカル / オフロード

デフォルトはローカルで Presidio + spaCy NER (`ja_ner_ja`) + 正規表現を実行。

```sh
pleno-scan dir ./my-repo --base-url https://pleno-anonymize.fly.dev
PLENO_BASE_URL=... pleno-scan dir ./my-repo
pleno-scan dir ./my-repo --base-url ... --api-key "$PLENO_API_KEY"
```

両モードとも返ってくるエンティティ集合は同じ。git 履歴のスキャンは行単位の NER オーバーヘッドが見合わないため常に正規表現のみ。

## 検出エンティティ

NER (`ja_ner_ja` + Presidio): `PERSON` `ADDRESS` `ORGANIZATION` `DATE_OF_BIRTH` `BANK_ACCOUNT`

正規表現 + checksum: `PHONE_NUMBER` `MY_NUMBER` `MY_NUMBER_CORPORATE` `CREDIT_CARD` `PASSPORT` `DRIVER_LICENSE` `HEALTH_INSURANCE` `RESIDENCE_CARD` `POSTAL_CODE` `EMAIL_ADDRESS` `IP_ADDRESS` `URL`

`URL` `HEALTH_INSURANCE` `DRIVER_LICENSE` はソースコード上で誤検出が多いためデフォルトプロファイルから除外。`--entities ALL` で全部、`--entities PHONE_NUMBER,EMAIL_ADDRESS` で個別指定。

## Verification

各 finding には `passed` / `failed` / `unverified` のラベルが付く。

- `passed` — checksum (Luhn / マイナンバー / 法人番号) を通過、または同行近傍に文脈キーワード
- `failed` — checksum 失敗 (誤検出の可能性が高い)
- `unverified` — 該当する validator が無く文脈ヒットも無い

`--only-verified` で `passed` のみに絞れる。

## 出力

| `--report-format` | 用途 |
|---|---|
| `human` (既定) | カラー付きテーブル |
| `json` | 機械可読 |
| `sarif` | GitHub Code Scanning 投入可能な SARIF 2.1.0 |

`--report-path FILE` でファイル出力。終了コードは `0`(検出なし) / `1`(検出あり) / `2`(usage error)。

## 抑制

リポルートに `.plenoignore` を置くと自動で読まれる:

```
docs/samples/**          # path glob (gitignore syntax)
PHONE_NUMBER             # entity 全体
finding:7a3b8c9d         # 特定 finding の fingerprint
```

行内ディレクティブ:

```py
SUPPORT_PHONE = "0120-123-456"  # pleno:ignore PHONE_NUMBER
EXAMPLE_EMAIL = "user@example.com"  # pleno:ignore
```

`pleno-scan baseline` で現状の検出を fingerprint 一覧として吐き出し、`--baseline FILE` で適用すれば既知の finding をまとめて無視できる。

## 主なフラグ

| フラグ | 既定 | 役割 |
|---|---|---|
| `--entities` | デフォルトプロファイル | 検出対象を絞る (`PHONE,EMAIL` / `ALL`) |
| `--language` | `ja` | 解析言語 (`ja` / `en`) |
| `--base-url` | (なし) | リモート pleno-anonymize へオフロード |
| `--api-key` | (なし) | オフロード時の Bearer token |
| `--concurrency` | 8 | オフロード時の並列リクエスト数 |
| `--include` / `--exclude` | (なし) | gitignore 風 glob でファイル絞り込み |
| `--max-file-size` | 1 MB | これを超えるファイルは skip |
| `--only-verified` | off | `passed` 以外を抑制 |
| `--report-format` | `human` | `human` / `json` / `sarif` |
| `--baseline` | (なし) | 既知 finding を抑制する fingerprint JSON |

`--gitignore` と built-in skip リスト (`.git`, `node_modules`, `.venv`, `dist`, `build`, `vendor`, …) と NUL byte によるバイナリ判定はデフォルトで効いている。
