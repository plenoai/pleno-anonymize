# Model Versioning

ONNX NER モデルを HuggingFace Hub (`0xhikae/ja-ner-onnx`) に release する手順と
バージョニング規約。Closes #71. CI 実装は
`.github/workflows/release-model.yml` を参照。

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

## 実 release は #48 完了後

scaffolding として #71 PR は merge する。実 push は trained artifact が
`packages/training/output/hf-ja-v02-tiny/` に生成された後 (#48 完了後) に
最初の `model/v*` tag を切って発火する。
