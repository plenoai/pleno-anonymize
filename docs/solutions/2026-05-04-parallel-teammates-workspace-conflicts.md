---
title: 並列 teammates の workspace pyproject conflict — alphabetical sort + small commits
date: 2026-05-04
issue: ADR-0007 multi-source rollout
related_prs: [pii-scanner-github, pii-scanner-aws, pii-scanner-slack, pii-scanner-postgres, pii-scanner-oci]
tags: [workflow, monorepo, uv-workspaces, git-worktree, parallelism]
---

# 並列 teammates の workspace pyproject conflict — alphabetical sort + small commits

## 何が起きたか

multi-source 展開で 5+ teammates を同時に走らせ、各々が `packages/pii-scanner-<X>/` を新規作成する構成にしたところ、root `pyproject.toml` の `[tool.uv.workspace] members` 配列が常に rebase conflict を起こした:

```toml
[tool.uv.workspace]
members = [
    "packages/recognizers",
    "packages/pii-scanner",
    "packages/pii-scanner-github",   # teammate A 追加
    "packages/pii-scanner-aws",      # teammate B 追加
    ...
]
```

teammate A が push、teammate B が rebase → 両者が同じ位置に新行を追加していて conflict。

## なぜ起きたか (root cause)

3 つの問題が重なる:

1. **配列の順序が決まっていない**: 各 teammate が「末尾に追加」するため必ず同じ行に挿入 → 3-way merge が成立しない
2. **uv.lock も同時更新**: `uv sync` で lock も触るため二重 conflict
3. **commit が大きい**: 各 teammate が「実装完了 → commit → push」の単一 commit のため、conflict resolution が原子的に解けない

## 検出方法 / 教訓

monorepo workspace の members 配列は **alphabetical に厳密ソート** を rule として確立すれば、追加位置が一意に決まり 3-way merge が成立する:

```toml
[tool.uv.workspace]
members = [
    "packages/pii-scanner",
    "packages/pii-scanner-aws",
    "packages/pii-scanner-azure-devops",
    "packages/pii-scanner-bitbucket",
    "packages/pii-scanner-gcs",
    "packages/pii-scanner-github",
    "packages/pii-scanner-gitlab",
    "packages/pii-scanner-notion",
    "packages/pii-scanner-oci",
    "packages/pii-scanner-postgres",
    "packages/pii-scanner-slack",
    "packages/recognizers",
    "packages/training",
    "server",
]
```

これでも `uv.lock` の merge は手動だが、members の merge は git の三方マージで自動解決されることが多い。

## 適用ガイド

### teammate brief に明示する

各 teammate の prompt に以下を必ず含める:

> 3. Add `packages/pii-scanner-<X>` to root `pyproject.toml` workspace members (**alphabetical**)
> 7. `git pull --rebase origin feature/multi-source` then push — resolve pyproject.toml/uv.lock conflicts by merging all teammates' workspace members

### orchestrator 側で worktree を分ける

各 teammate を `Agent({isolation: "worktree"})` で起動。teammate 間の filesystem 相互作用がゼロになる。push のみが共有 contention point。

### conflict resolution は orchestrator が行う

teammate が conflict に当たって停止する場合に備え、orchestrator (主 session) は teammate の push 完了通知を受けたら fetch + rebase。teammate 内部での rebase 失敗は再 launch しない (テストの再実行コストが高い)。

### CI 側で workspace の整合性を保証

別途 lint job で「members が alphabetical sorted か」「全 member 配下に pyproject.toml があるか」を assert する:

```yaml
- name: Verify workspace members alphabetical
  run: |
    python -c "
    import tomllib
    members = tomllib.load(open('pyproject.toml','rb'))['tool']['uv']['workspace']['members']
    assert members == sorted(members), f'unsorted: {members}'
    "
```

## 関連

- uv workspaces docs: https://docs.astral.sh/uv/concepts/projects/workspaces/
- gh-wt: CoW worktree tool used to spawn teammate isolated workspaces
- 関連 learning: hermetic connector tests via httpx.MockTransport
