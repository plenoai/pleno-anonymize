# Model Versioning

spaCy CNN の ONNX NER モデルを HuggingFace Hub (`0xhikae/ja-ner-onnx`) に
release する手順とバージョニング規約。Closes #71. CI 実装は
`.github/workflows/release-model.yml` を参照。

このリポジトリには他に 2 つの独立した HF 配布先があるが、いずれも本 doc の
tag-push フローの対象外 (下記「他の配布先との関係」参照): SDK が自動
ダウンロードする `pleno_anonymize_{ja,en}` wheel と、server の APPI エンジンが
使う `ja-ner-appi-v1-onnx`。

## TL;DR

```
git tag model/v0.13.0
git push origin model/v0.13.0
```

→ `release-model.yml` が発火し、`0xhikae/ja-ner-onnx` の `v0.13.0` revision
(branch) に ONNX 量子化済みモデルを push する。

## なぜ tag push なのか

CLAUDE.md 既定:
> package/library は全て tag push で発火する trusted publishing を採用する

モデル artifact も同 pattern を踏襲する。trunk-based development の
service デプロイ (main push) とは分離し、release は明示的な tag 操作で行う。

## Tag pattern

```
model/v<MAJOR>.<MINOR>.<PATCH>
```

例: `model/v0.13.0`, `model/v1.0.0`, `model/v0.14.1`.

- prefix `model/` で library/service の semver tag (`v*`) と namespace 分離。
- `v` は revision に伝播される (HF Hub の branch 名は `v0.13.0`)。
- pre-release は `model/v0.14.0-rc1` の形 (HF revision = `v0.14.0-rc1`)。

## Semver の解釈 (NER モデル文脈)

| 種別 | 例 | 該当する変更 |
|---|---|---|
| MAJOR | `v0.x` → `v1.0` | label schema 変更 / tokenizer 変更 / API 互換破壊 |
| MINOR | `v0.13` → `v0.14` | 新エンティティ追加 / 学習データ大幅増 / 評価指標 +X% |
| PATCH | `v0.13.0` → `v0.13.1` | 同 schema・同 data・hyperparameter 微調整のみ |

互換破壊 (MAJOR bump) の判定例:
- `LABEL_LIST` の要素を削除・改名
- tokenizer (base model) を `ku-nlp/deberta-v2-tiny-japanese` から差し替え
- 推論時の output shape / id2label が変わる

これらに該当する場合は CHANGELOG にも明記する。

## Revision の運用

- HF Hub 上の revision = tag の `v` 部分 (例: `model/v0.13.0` → `v0.13.0`)。
- 同名 revision が既存の場合、`export_onnx --revision` は **再 push を拒否する**
  (既存 revision を上書きしない). published 済みモデルを clobber しないため。
- やり直したい場合は patch を bump する (`v0.13.0` → `v0.13.1`).

## 必要な repo secret

- `HF_TOKEN` — HF Hub への write 権限を持つ token. Settings → Secrets and
  variables → Actions に追加する。未設定の場合は non-dry-run release が
  fail する設計 (silent skip しない)。

## Dry-run

実 artifact 無し・secret 無しでも yaml の挙動を試したい場合:

1. GitHub UI → Actions → "Release model to HF Hub" → Run workflow
2. revision に `v0.0.0-dryrun` 等を入力、`dry_run` を `true` のまま
3. export step は走るが `--dry-run` で push をスキップし、log のみ出す

## CI と Makefile の関係

`packages/training/Makefile` の `push-hf-v02-tiny` target はそのまま残し、
ローカルでの ad-hoc push 経路として保持する。CI は同等の `python -m
pleno_ner_training.export_onnx` 呼び出しに `--revision` / `--dry-run` を
追加した形で実行する (Makefile target の wrapper にはしない — workflow で
直接 flag を制御するほうが dry-run path が明示的になるため)。

## 現状 (2026-07 時点)

`model/v0.13.0` tag で `0xhikae/ja-ner-onnx` への実 push が 1 回発火済み。
このタグ運用は spaCy CNN (`ja-v02` / `hf-ja-v02-tiny*` 系) の ONNX 成果物専用
であり、以下の「他の配布先との関係」で説明する ja/en 0.3.0 の出荷経路とは
別系統である。

## 他の配布先との関係

同じ repo に、本 doc の tag-push フローとは別の HF 配布先が 2 つ存在する。
混同しないこと。

| 配布先 | 用途 | push 経路 | 参照元 |
|---|---|---|---|
| `0xhikae/ja-ner-onnx` | spaCy CNN の ONNX export (本 doc の対象) | `model/v*` tag → `release-model.yml` | `export_onnx.py` |
| `0xhikae/pleno_anonymize_ja` / `pleno_anonymize_en` | SDK が実行時に自動 download する model wheel。ja/en とも 0.3.0 を出荷済み | `make push-model-hf` / `push-model-hf-en` → `scripts/push_model_to_hf.py` (tag 起点ではない ad-hoc push) | `packages/sdk/src/pleno_anonymize/_models.py` の `MODEL_WHEELS` |
| `0xhikae/ja-ner-appi-v1-onnx` | server の APPI エンジン (要配慮個人情報, DeBERTa v2 ONNX) が読む transformer モデル。`APPI_MODEL_ID` 環境変数で上書き可能 | 単発 feature 実装時に手動 push (本 doc の release flow 対象外) | `server/src/app.py` の `_APPI_MODEL_ID` |

ja/en 0.3.0 の出荷は `pleno_anonymize_{ja,en}` 経路で完了済みであり、本 doc の
`model/v*` tag-push は spaCy CNN ONNX 成果物を追加 release したい場合にのみ
使う。

## SDK wheel (`pleno_anonymize_ja`/`_en`) の version single source of truth (#296)

上記の `model/v*` tag flow は ONNX artifact (`0xhikae/ja-ner-onnx`) 向けで、
SDK が pip install する spaCy wheel (`0xhikae/pleno_anonymize_ja` /
`_en`) とは別チャネル。こちらは出荷のたびに (1) `make package(-en)` の
`--version` 手打ち (2) `packages/sdk/src/pleno_anonymize/_models.py` の
`MODEL_WHEELS` URL 手編集、の2箇所が独立に乖離しうる問題があった (#296)。

`packages/models/versions.json` を言語ごとの `{version, hf_repo, wheel_url}`
の single source of truth とし、以下のフローに統一する:

0. `/release-gate` (`.claude/skills/release-gate/`) — academic-validity-reviewer
   subagent による敵対的学術査読。APPROVE が出るまで以降の手順に進まない。
   verdict は `packages/training/experiments/artifacts/release-gate/` に残る。
1. `make release-model MODEL_LANG=<ja|en> MODEL_VERSION=<x.y.z>`
   (`packages/training/` から実行) — versions.json を更新し、その version
   で該当言語の package target を実行し、SDK 側の整合性テスト
   (`packages/sdk/tests/test_model_versions.py`) を流す。
2. テストが落ちた場合 (version を上げた直後は必ず落ちる):
   `packages/sdk/src/pleno_anonymize/_models.py` の `MODEL_WHEELS` を
   versions.json の新しい wheel_url に手編集する。`_models.py` は SDK が
   standalone で動くために versions.json を実行時に読まない設計 (同ファイル
   23-25行のコメント参照) なので、生成ではなくこのテストで両者を拘束する。
3. 上の 1〜2 コマンドが最後に案内する既存フロー — `push_model_to_hf.py` で
   wheel を HF Hub に push し、`packages/sdk/pyproject.toml` の version を
   上げて `sdk/vX.Y.Z` tag を push (trusted publishing で PyPI に出荷) —
   を実行する。ここは #296 の変更範囲外で、既存の手順のまま。
