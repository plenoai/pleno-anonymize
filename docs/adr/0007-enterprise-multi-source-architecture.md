# ADR-0007: Enterprise Multi-Source Scanner Architecture

**Status:** Proposed
**Date:** 2026-05-04
**Drives:** Tasks #1–#47 (`pleno-pii-scanner` enterprise expansion to TruffleHog-Enterprise–level coverage)

---

## Context

`pleno-pii-scanner` v0.2.5 は現状、scan source として **filesystem (`dir`)・local git repo (`git`)・GitHub shallow clone (`github`)** の 3 系統しか持たない。`github.py` は `gh` CLI に依存し org enumeration は `--limit 1000` で silent truncate、shallow clone の token は subprocess URL で渡している。これは個人開発者向けの体裁であり、**エンタープライズ DLP 製品としては受発注に耐えない**。

要件:

- TruffleHog Enterprise 相当の **Git ホスト 4 種・オブジェクトストレージ 3 種・SaaS 協業 3 種・ナレッジ/PM 6 種・OCI レジストリ・データストア 7 種・CI ログ・Salesforce** を一括 scan できる
- multi-tenant (org 横断 / multi-AWS account / multi-Atlassian site) で credential が独立し、**1 scan ジョブで複数テナントを fan-out** できる
- production datastore を**絶対に落とさない** (replica 強制、statement timeout、reservoir sampling)
- 検出 secret の **liveness verification** で誤検知を消し、enterprise SOC のアラート疲労を作らない
- **incremental scan** で全件再走を avoid (BigQuery $5/TB、S3 GET 課金、Slack Tier 3 制限)
- RBAC・audit log・suppression policy・SLA-driven re-scan を governance layer として提供
- CLI 後方互換 (`dir` / `git` / `github`) は維持し、既存ユーザーの ergonomics を壊さない

8 並列 research teammates (`Plan` + `compound-engineering:ce-web-researcher` + 6×`compound-engineering:ce-framework-docs-researcher`) で source 群ごとに調査済み。本 ADR はその合意点をシステム設計に落とし込む。

## Decision

### 1. Source / Document Protocol

`pleno_pii_scanner.sources.base` に以下を新設する。

```python
class SourceConnector(Protocol):
    id: str          # 安定な scan instance ID, e.g. "aws:prod-us-east-1"
    kind: str        # "aws-s3", "slack", "confluence", ...
    async def discover(self, filter: SourceFilter, cursor: Cursor | None) -> AsyncIterator[DocumentRef]: ...
    async def fetch(self, ref: DocumentRef) -> AsyncIterator[Document | DocumentChunk]: ...
    def capabilities(self) -> Capabilities: ...
    async def close(self) -> None: ...
```

`Document` は `path` (論理 URI: `s3://bucket/key`, `slack://T01/C02/ts`)・`native_url`・`parent_chain`・`content_type`・`size`・`etag`・`content_hash`・`fetched_at`・`last_modified`・`metadata`・`text|binary|stream` のいずれか 1 つ — を frozen dataclass で持つ。

**discover と fetch を分離** する理由は、10⁹ key の S3 bucket / TB 級 SharePoint サイトに対して **enumeration 完了を待たずに scan を流せる** ようにするため。`DocumentChunk` で TB 級 object も byte_range + sliding overlap で stream 処理する (NER の入力上限 `--max-doc-bytes` 超過は head/middle/tail サンプリング、regex は exhaustive)。

### 2. Connector Registry + entry_points discovery

`pleno_pii_scanner.sources.registry` で `register(kind, factory)` を提供し、起動時に `entry_points("pleno_pii_scanner.connectors")` を **lazy import** する。サードパーティ connector (`pleno-pii-scanner-snowflake` 等) は wheel として独立配布でき、enterprise の調達/監査フローで個別承認できる。

### 3. Credential Broker (multi-tenant + OIDC)

優先順位 (高い順):

1. `--credentials-file PATH` (TOML、SOPS/age 復号サポート)
2. process env (`PLENO_<KIND>_<NAME>=...`)
3. OS keyring (macOS Keychain / Linux Secret Service / Windows Credential Manager)
4. cloud instance identity (AWS IMDSv2 / EKS IRSA / GCP metadata / Azure MSI)
5. **OIDC federation** (`AssumeRoleWithWebIdentity` / GCP Workload Identity Federation / Microsoft Entra) — long-lived key を CI に置かない

`CredentialProfile` は base identity + ordered `assume_role` hop chain を持ち、AWS Organizations の `OrganizationAccountAccessRole` 経由で **数百 AWS account を 1 scan で横断** する。`CredentialResolver` は interface 化し、Vault / AWS Secrets Manager / 1Password Connect は plugin として差し込める。

### 4. Scheduler + Rate Limiter

asyncio + `Semaphore` で per-connector concurrency、`(connector_kind, tenant_id)` キーの token bucket で global rate limit、**429 で AIMD shrink**。NER は `ProcessPoolExecutor(forkserver)` で別プロセス、model weight を共有。tenacity ベースの uniform retry decorator で `RateLimitExceeded` / `httpx.HTTPStatusError` / `Retry-After` を統一処理。

### 5. CheckpointStore + Incremental Scan

SQLite (`~/.local/state/pleno/scan-<id>/`) を default、Postgres / S3 を plugin。`(source_id, cursor, last_doc_ref, last_byte_range)` を **batch 単位で永続化** し、findings は Parquet/JSONL shard で append (kill -9 で 1 batch 分しかロスしない)。

各 connector の incremental cursor 形:

| connector | cursor |
|---|---|
| GitHub | `pushed:>{ts}` + issue/PR `since={ts}` |
| GitLab | `last_activity_after={ts}` + `updated_after={ts}` |
| Bitbucket | BBQL `updated_on > {ts}` |
| Azure DevOps | `GitQueryCommitsCriteria(from_date)` + WIQL `ChangedDate >= @last_run` |
| S3 | inventory diff or `last_modified > {ts}` per key |
| GCS | object `updated > {ts}` |
| Azure Blob | `/drives/{id}/root/delta` token |
| Slack | `oldest={ts}` (Slack ts 文字列) |
| Teams | `/channels/{cid}/messages/delta` deltaLink |
| Discord | `after=snowflake(last_id)` |
| Jira | JQL `updated >= -1d ORDER BY updated ASC` |
| Confluence | CQL `lastModified >= ...` |
| Notion | `databases/{id}/query` filter `last_edited_time >= cursor` |
| SharePoint | `/drives/{id}/root/delta` (gold standard) |
| Google Drive | `changes.list(pageToken)` (start page token = cursor) |
| Postman | resource `updatedAt` diff |
| OCI registry | manifest digest 不変なら全 layer skip |
| Postgres | `WHERE updated_at > $1` or pgoutput logical replication slot |
| MySQL | binlog stream or `updated_at` index |
| Mongo | change stream `resume_after` token |
| Elasticsearch | `_seq_no` + `_primary_term` |
| BigQuery | `_PARTITIONTIME >= ...` |
| Snowflake | `STREAMS` (`CREATE STREAM scanner_s ON TABLE t`) |

### 6. ContentExtractor Registry

MIME 別 dispatch + sniff (`python-magic`)。text/* passthrough、archive (zip/tar/gz/zstd、depth + size bomb guard、>100x expansion で reject)、PDF (`pdfminer.six` / `pypdfium2`)、Office (`python-docx` / `openpyxl` / `pypandoc`)、columnar (`pyarrow` parquet/orc, `fastavro`)、HTML (`markdownify`)、ADF / Confluence storage / Notion blocks は per-source pre-normalizer。**enterprise では PDF/Office の本文抽出は必須**。`--scan-binary=raw` で entropy/regex-only fallback。

### 7. SecretsVerifier (liveness)

検出 token を per-provider API でテスト (GitHub `/user`, AWS `sts:GetCallerIdentity`, Slack `auth.test`, Stripe `/v1/account`, ...)。**verified=true は severity=critical に bump**。verification cache + reverify schedule。**TruffleHog Enterprise の最重要価値はここ** — 誤検知を SOC に流さない。

### 8. Custom detector framework (BYOD)

YAML で regex / context keyword / verifier function を宣言可能。enterprise は社内固有 secret 形式 (社内 API token、独自 ID 体系) を持つので必須。`pleno-recognizers` パッケージとは別レイヤー。

### 9. Notifier

SARIF (既存) + Slack incoming webhook + SMTP + Jira issue create + Splunk HEC + generic webhook + OTLP export。severity / verification status で routing rule。

### 10. RBAC + AuditLog + SuppressionEngine

`policy.toml` (or OPA/Rego) で `(subject, source_kind, source_id_glob, action)` 評価。submit 時 + fetch 時の二段 check。AuditLog は append-only NDJSON + HMAC chain or OTLP→SIEM。SuppressionEngine は **org → team → repo の階層 policy**、baseline は最下層。

### 11. FindingsStore

生 findings は Parquet shard (S3/GCS)、queryable index は Postgres (finding_id, fingerprint, source_id, status, owner, sla_due_at)。snippet は KMS-managed DEK で **envelope encryption** (tenant 単位)。fingerprint + entity + path のみ平文で dedup/search 可能。raw value は `sha256[:16]` + masked excerpt のみ保持し、**memfd 上にしか raw value を載せない**。

### 12. ScheduleRegistry (SLA-driven re-scan)

cron + jitter per source、severity-weighted SLA (critical=1h、high=24h、medium=7d)、Scheduler と統合 (separate cron 不要)。

### 13. パッケージ構成: 別 wheel 主、extras 副

| wheel | 内容 |
|---|---|
| `pleno-pii-scanner` (core) | Protocol, registry, scheduler, regex/NER engine, governance, CLI、builtin `dir`/`git`/`github` |
| `pleno-pii-scanner-github` | GitHub App + GHES + org enum (PyGithub + githubkit) |
| `pleno-pii-scanner-gitlab` | python-gitlab |
| `pleno-pii-scanner-bitbucket` | atlassian-python-api Bitbucket |
| `pleno-pii-scanner-azuredevops` | azure-devops |
| `pleno-pii-scanner-aws` | aioboto3 (S3) |
| `pleno-pii-scanner-gcp` | google-cloud-storage + Drive |
| `pleno-pii-scanner-azure` | azure-storage-blob |
| `pleno-pii-scanner-slack` | slack-sdk |
| `pleno-pii-scanner-m365` | msgraph-sdk (Teams + SharePoint) |
| `pleno-pii-scanner-discord` | discord.py |
| `pleno-pii-scanner-atlassian` | Jira + Confluence (httpx 直叩き) |
| `pleno-pii-scanner-notion` | notion-client |
| `pleno-pii-scanner-postman` | httpx 直叩き |
| `pleno-pii-scanner-oci` | daemon-less OCI registry |
| `pleno-pii-scanner-postgres` / `-mysql` / `-mongo` / `-elastic` / `-redis` / `-bigquery` / `-snowflake` | datastore connectors |
| `pleno-pii-scanner-ci` | Jenkins / CircleCI / Buildkite / GH Actions logs |
| `pleno-pii-scanner-salesforce` | simple-salesforce |
| `pleno-pii-scanner[all]` | 全 connector を depend する meta-extra |

**extras ではなく別 wheel** とする理由: enterprise は transitive lockfile を pin するため、extras 構成だと未使用 SDK の version range まで毎回 resolve され、boto3 / google-api-python-client / msgraph の version conflict が発生する。別 wheel ならセキュリティチームが connector を独立に承認できる。

### 14. CLI 体系

```
pleno-pii-scanner scan <connector-kind> [--source-config FILE] [--credential-profile NAME] [common opts]
pleno-pii-scanner scan --plan plan.toml         # multi-source orchestration
pleno-pii-scanner connectors list|describe <kind>
pleno-pii-scanner credentials test <profile>
pleno-pii-scanner findings query|export
pleno-pii-scanner schedule add|list|run
```

既存 `dir` / `git` / `github` は thin alias として残す (`scan dir <path>` / `scan git <path>` / `scan github <slug-or-org>`)。snapshot test on `--report-format=json` で既存契約を byte-identical 保証。

### 15. Daemon-less OCI registry

**`docker-py` 禁止**。`/var/run/docker.sock` への mount は CI / k8s pod / gVisor sandbox で許可されない。HTTP 直叩き (httpx + tenacity) で OCI Distribution Spec v1.1 + Image Index 多 arch 展開。`tarfile.open(fileobj=stream, mode='r|gz')` + zstandard で member 単位 streaming (RSS<1GB)。**config.Env / Cmd / Entrypoint / image history scan は最高優先度** — registry finding の 40-60% はここ。layer digest 単位 finding cache で base layer dedup。

### 16. Production-safe datastore scan

全 datastore で:

- replica / read-only role 強制 (`pg_is_in_recovery()` チェック、CI test で primary 接続を fail)
- statement_timeout 30s
- reservoir sampling (n=300 = 95% 信頼で p=1% PII 検出: `n = log(0.05) / log(0.99) ≈ 299`)
- 専用 connection pool (max 2)
- raw value は finding に含めない (`Finding.value: NoReturn` で誤用検出)

BigQuery は **dry_run → cost cap 確認 → 本実行** の二段必須、`maximum_bytes_billed=100MB` hard cap、`priority=BATCH`。Snowflake は dedicated XS warehouse + `STATEMENT_TIMEOUT_IN_SECONDS=30` + `QUERY_TAG='pii-scanner'` で chargeback 可視化。

### 17. Deploy

- distroless Docker image (daemon 不要)
- Helm chart (`deploy/helm/pleno-pii-scanner/`) で workload identity binding、credentials secret refs、connector enable list、findings store config
- `HorizontalPodAutoscaler` + `PodDisruptionBudget` + `NetworkPolicy`
- 各 connector wheel は **tag push trusted publishing** (CLAUDE.md 方針に従う)

## Consequences

- **依存爆発を回避**: enterprise 採用時、AWS だけ使う顧客が Atlassian / Slack / Snowflake SDK の脆弱性監査を強いられない。`pleno-pii-scanner-aws` を単独承認できる
- **CLI 後方互換 OK**: 既存ユーザーは `dir` / `git` / `github` の挙動・出力形式が byte-identical で動く。snapshot test で保証
- **Operating cost 制御**: BigQuery $50/月 budget alert、Snowflake AUTO_SUSPEND=60、Slack Tier 3 制限回避は Discovery API 経路で
- **Negative — 実装規模**: 全 connector で 30+ wheel + 数百ファイル。タスクボード #1–#47 で並列実装する
- **Negative — 監査面積**: connector ごとに credential 保管 / scope minimization / network egress を個別レビューする運用負荷が出る。governance layer (RBAC + AuditLog) で対応
- **Negative — multi-arch wheel build**: `pleno-pii-scanner-postgres` (`psycopg[binary]`) など binary wheel は manylinux + macos + windows でビルドが必要。GitHub Actions matrix で対応
- **Positive — TruffleHog Enterprise との差別化軸**: 日本語 PII 特化 (My Number / 健康保険証 / 運転免許 / 戸籍 / 住民票) + Presidio 統合 + DB-cluster mode は parity 製品にない強み。Postman の secret rotation interlock も TruffleHog にない

## Validation

- [ ] 既存 `dir` / `git` / `github` の `--report-format=json` snapshot test が green
- [ ] 各 connector に vcr/cassette 統合テスト (公開 fixture: `octocat/Hello-World`, `library/alpine`, public S3) と secret 露出ゼロ
- [ ] production DB の primary endpoint に接続したら CI が fail する統合テスト (`pg_is_in_recovery()=false` で skip)
- [ ] `pleno-pii-scanner connectors describe <kind>` の出力と `docs/connectors/<kind>.md` が一致
- [ ] BigQuery connector で `maximum_bytes_billed` を超える query を投げた時に exception で停止
- [ ] Slack scan で `xoxb` token に `xoxa` の API を呼ぶと型エラーになる (NewType 分離)
- [ ] OCI registry scan で `docker.sock` が mount されていない環境で完走
- [ ] `pleno-pii-scanner scan --plan plan.toml` で複数 connector を 1 ジョブ実行し、checkpoint 中断・再開で結果が変わらない

## Follow-on

- 各 connector 実装は Tasks #17–#42 に分割済み (本 ADR の Decision §13 と一対一対応)
- core infrastructure は Tasks #3–#14
- Tasks #43–#44 で Helm + GitHub Actions trusted publishing
- Tasks #45–#46 で per-connector test + docs
- Task #47 で実装中の知見を `docs/solutions/2026-05-04-*` に compound

## References

- Research outputs (8 並列 teammates, 2026-05-04):
  - Source protocol architecture (`Plan` agent)
  - TruffleHog Enterprise parity matrix (`ce-web-researcher`)
  - Git host connectors (GitHub/GitLab/Bitbucket/Azure DevOps)
  - Object storage connectors (S3/GCS/Azure Blob)
  - Collaboration connectors (Slack/Teams/Discord)
  - Wiki & PM connectors (Jira/Confluence/Notion/SharePoint/Drive/Postman)
  - Container registry connectors (Docker Hub/GHCR/ECR/GCR/ACR/Harbor/Quay)
  - Datastore connectors (Postgres/MySQL/Mongo/Elastic/Redis/BigQuery/Snowflake)
- TruffleHog Enterprise: <https://trufflesecurity.com/trufflehog-enterprise>, <https://docs.trufflesecurity.com/scan-data-for-secrets>
- Related ADRs: [0001-aws-lambda-container-image](./0001-aws-lambda-container-image.md), [0002-api-url-structure](./0002-api-url-structure.md)
- 既存 source code: `packages/pii-scanner/src/pleno_pii_scanner/{walker,git_history,github,cli}.py`
