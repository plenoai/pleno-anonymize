---
title: uv sync prunes wheels not in uv.lock — Dockerfile ordering matters
date: 2026-05-03
issue: #54
related_prs: [#34, #44]
tags: [docker, uv, packaging, dependency-management, fail-fast]
---

# uv sync prunes wheels not in uv.lock — Dockerfile ordering matters

## 何が起きたか

production の `/ready` が 503 を返す状態に。`spacy.load('ja_ner_ja')` が runtime で `E050` (model not found) を返していた。image build は **成功** しており、CI も green。

## なぜ起きたか (root cause)

`uv sync --frozen --no-dev` は uv.lock を venv に対する **single source of truth** として扱い、**lock に存在しない既インストール package を prune する**。

PR #34 時点の Dockerfile はこの順:

```dockerfile
RUN uv sync --frozen --no-dev --no-install-project   # 1. base deps
RUN uv pip install /tmp/ja_ner_ja-*.whl              # 2. project-local wheel
RUN uv sync --frozen --no-dev                        # 3. project install
                                                     #    ← ここで 2. の wheel が prune される
```

ja_ner_ja wheel は uv.lock に登録されていない (project-local wheel) ため、3 行目の `uv sync` が「lock にないので不要」と判断して削除した。結果:

- `/opt/.venv/lib/python*/site-packages/ja_ner_ja/` が消える
- import 自体は別パスから通る場合でも、spaCy の data path resolver が壊れて `E050`
- image build は副作用なく成功 → registry に push → production で初めて顕在化

## 検出方法 / 教訓

- uv の **prune semantics** は「速い再現性」のために設計されている。lock に無いものは敵。
- `uv pip install` は escape hatch だが、後段で `uv sync` を走らせるとリセットされる。
- PR #44 修正: wheel install を **2 回目の `uv sync` の後** に移動。これで prune の対象外になる。

## 適用ガイド (再発防止)

優先度順:

### (a) wheel を uv.lock に取り込む (推奨, 構造的解決)

constraint file 経由で uv.lock に project-local wheel を登録する:

```toml
# pyproject.toml
[tool.uv.sources]
ja_ner_ja = { path = "./wheels/ja_ner_ja-0.13.0-py3-none-any.whl" }
```

これにより `uv sync` は wheel を「正規 dependency」として扱い、prune しない。

### (b) Dockerfile に lint コメント (PR #44 で実施済)

```dockerfile
RUN uv sync --frozen --no-dev          # FINAL sync — anything after this won't be pruned
RUN uv pip install /tmp/ja_ner_ja-*.whl  # MUST come after the final uv sync
```

### (c) CI structural check (将来)

shell-level lint で「`uv pip install` の後に `uv sync` が無い」ことを assert する:

```bash
awk '/uv pip install/{seen=1} /uv sync/{if(seen){print "ERROR: uv sync after uv pip install"; exit 1}}' Dockerfile
```

## 関連

- PR #34 — 問題が混入した Dockerfile
- PR #44 — install ordering 修正
- uv docs: `--frozen` semantics, prune behavior
- 関連 learning: `2026-05-03-build-time-model-smoke-test.md` (同じインシデントの surfacing 機構)
