---
title: Build-time spaCy model smoke test catches stale Dockerfile paths
date: 2026-05-03
issue: #53
related_prs: [#23, #40, #43]
tags: [docker, ci, observability, fail-fast, spacy]
---

# Build-time spaCy model smoke test catches stale Dockerfile paths

## 何が起きたか

- 4 週間前の PR #23 が Dockerfile を変更した結果、`server/src/app.py` 内の `spacy.load(<filesystem_path>)` が参照するパスが stale になっていた。
- model load は runtime warmup の **daemon thread** で実行されており、例外が発生しても process は落ちず log のみが残った。
- `/health` (liveness) は静的に 200 を返すため healthy と見なされ、deploy も green で通過。
- 実トラフィックを処理する `/api/analyze` のみ 500 を返し続け、PR #40 (4 週間後) で初めて regression が発覚した。

## なぜ起きたか (root cause)

1. **重い初期化を background thread に逃がす設計**: warmup が daemon thread で動くため、failure が「黙って消える」。例外は logger に出るが、liveness probe は影響を受けない。
2. **build と runtime の責務が分離されすぎている**: image build は「依存が入った」ことしか保証せず、「load できる」ことを保証しない。
3. **Dockerfile 変更のレビュー時に runtime path との整合性が確認できない**: code レビュー単体で気付ける情報量を超える。

## 検出方法 / 教訓

PR #43 で `RUN python -c "import spacy; spacy.load('<path>')"` を Dockerfile に追加。

- image build 中に **7 秒** で同じ failure mode を再現できる。
- build が fail するため、broken image は registry に push されない (= production に届かない)。
- daemon thread での silent crash と異なり、CI / build log で即座に可視化される。

教訓: **production runtime で初期化する重い処理は、build 時に 1 回 dry run せよ**。

## 適用ガイド (再発防止)

build-time smoke test を追加すべき対象:

- **model wheel の load** (spaCy, transformers, sklearn pickle 等)
- **asset / weight の download 後の整合性チェック** (sha256, size)
- **config validation** (pydantic settings, env var の必須項目)
- **DB migration の dry-run** (alembic check 等、副作用なしで検証可能なもの)

実装パターン:

```dockerfile
# Stage: smoke test (image build を fail-fast させる)
RUN python -c "import spacy; nlp = spacy.load('/opt/models/pleno_anonymize_ja'); assert nlp.pipe_names"
```

注意点:

- runtime path と build-time path は **同じ string** で書く (重複は許容、整合性のほうが重要)。
- smoke test は数秒で終わる軽量 check に絞る。training や全件推論は別ステージへ。
- daemon thread で初期化するのを止めるわけではない。**build と runtime の二重チェック** が肝。

## 関連

- PR #23 — Dockerfile 変更で path stale 化
- PR #40 — production regression の発覚
- PR #43 — build-time smoke test 導入
- 関連 learning: `2026-05-03-uv-sync-wheel-install-ordering.md` (同じインシデントの別 root cause)
