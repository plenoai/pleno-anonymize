# Solutions / Learning Docs

production incident や PR review で得た学びを蓄積するディレクトリ。

## index

| date | issue | title |
|---|---|---|
| 2026-05-03 | #53 | [Build-time spaCy model smoke test catches stale Dockerfile paths](./2026-05-03-build-time-model-smoke-test.md) |
| 2026-05-03 | #54 | [uv sync prunes wheels not in uv.lock — Dockerfile ordering matters](./2026-05-03-uv-sync-wheel-install-ordering.md) |
| 2026-05-03 | #55 | [NER 評価基準は metric triad (corpus, metric, aggregation) で語る](./2026-05-03-metric-triad-evaluation-criteria.md) |
| 2026-05-04 | ADR-0007 | [Hermetic SourceConnector tests via httpx.MockTransport — 100% coverage without network](./2026-05-04-hermetic-connector-tests-mocktransport.md) |
| 2026-05-04 | pii-scanner-oci #33 | [OCI registry token cache miss when challenge overrides scope](./2026-05-04-oci-token-cache-scope-key.md) |
| 2026-05-04 | ADR-0007 | [並列 teammates の workspace pyproject conflict — alphabetical sort + small commits](./2026-05-04-parallel-teammates-workspace-conflicts.md) |

## 書き方

各 doc は frontmatter (title, date, issue, related_prs, tags) と以下の章立て:

- 何が起きたか
- なぜ起きたか (root cause)
- 検出方法 / 教訓
- 適用ガイド (再発防止)
- 関連

ファイル名: `YYYY-MM-DD-<short-slug>.md`
