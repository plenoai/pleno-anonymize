---
name: release-gate
description: |
  モデル/SDK リリース前に academic-validity-reviewer subagent で敵対的評価を行う
  必須ゲート。make release-model・HF push・model/v*・sdk/v* tag push の前に
  自律的に発火する。

  Trigger: release, リリース, 出荷, HF push, tag push, release-model, 公開前評価
---

# Release Gate: 敵対的学術査読

リリース候補の実験主張を `academic-validity-reviewer` subagent (.claude/agents/)
で敵対的に査読し、APPROVE が揃うまで出荷を止めるゲート。ner-improve ループの
KEEP 判定は「実験として成功」を意味するだけで「出荷してよい」を意味しない。

## 発火条件

以下のいずれかを実行する**前**に必ずこの skill を完了する:

- `make release-model MODEL_LANG=... MODEL_VERSION=...`
- HF Hub への wheel / model push (`push_model_to_hf.py`, `hf upload`)
- `model/v*` または `sdk/v*` tag push

## Phase 1: リリース主張の収集

1. 対象言語・version・出荷経路 (packages/models/versions.json との差分) を特定
2. 根拠となる experiment を `packages/training/experiments/log.jsonl` と
   `experiments/best.json` から特定 (id のリスト)
3. 主張指標 (overall F1/P/R、per-label recall、latency) と対応する eval JSON
   のパスを列挙

## Phase 2: 敵対的査読 (並列)

`academic-validity-reviewer` subagent を **3レンズ並列**で起動する。各レンズに
Phase 1 の情報を渡し、担当を明示する:

| lens | 重点検査 (subagent 定義の検査項目番号) |
|---|---|
| `statistical` | 2, 3, 7 — ベースライン妥当性・有意性・回帰隠蔽 |
| `contamination` | 4, 6 — データ汚染・ライセンス・指標の誇張 |
| `reproducibility` | 1, 5 — 主張と成果物の突合・再現性 |

各 subagent は JSON verdict (APPROVE/REVISE/REJECT + findings) を返す。

## Phase 3: 判定と記録

1. 集約規則: 1つでも REJECT → **REJECT**。REJECT なしで REVISE あり → **REVISE**。
   全て APPROVE → **APPROVE**。
2. verdict 全文を `packages/training/experiments/artifacts/release-gate/`
   配下に `{lang}-{version}.json` として保存し commit する (追試可能な監査証跡)。
3. 判定に従う:
   - **APPROVE**: リリース手順 (MODEL_VERSIONING.md) に進む
   - **REVISE**: required_actions を全て解消してから Phase 2 を再実行。
     解消せずにリリースしない
   - **REJECT**: リリース中止。blocker findings を GitHub issue 化し、
     ユーザーに報告する

## Constraints

- ゲートをスキップしてよい例外は存在しない。ユーザーが明示的にスキップを
  指示した場合のみ、その旨を release-gate artifact に記録した上で従う
- reviewer の findings を「解消」とは、指摘された検証を実際に通すことを指す。
  artifact の書き換えや主張の弱体化による回避は解消ではない
- reviewer が検証に使う eval・凍結ベンチ・log.jsonl を修正しない
