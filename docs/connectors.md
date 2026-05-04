# Connector matrix

`pleno-pii-scanner` ships a core scanner plus per-source `SourceConnector`
plugins discovered through the `pleno_pii_scanner.connectors`
entry-points group. Every connector implements the same five-method
Protocol (`discover`, `fetch`, `capabilities`, `id`, `kind`, `close`)
and is registered via `register(SPEC)` on import.

The table below tracks every shipped connector and what it covers.

## Source-of-truth status

ADR-0007 §13 (cloud collab) and §15–16 (object storage / databases)
group sources by category. The matrix mirrors that grouping.

### §13 — Source code & collaboration

| Connector | Auth | Incremental | Cursor scheme | Notes |
| --- | --- | --- | --- | --- |
| `github`     | App token / GHES | yes | `pushed:>ts` | replaces legacy `gh` CLI shell-out |
| `gitlab`     | PAT (SaaS + self-managed) | yes | per-project `updated_after` | |
| `bitbucket`  | Cloud API token + Server bearer | yes | `updated_on` ISO | |
| `azure-devops` | PAT + federated identity | yes | per-project | |
| `slack`      | Bot/User/Org-wide xoxa + Discovery API | yes | `oldest=` ts | |
| `discord`    | Bot token + Message Content intent | yes | per-channel snowflake | `?before=` initial / `?after=` resume |
| `msteams`    | Graph + WIF | yes | per-channel `@odata.deltaLink` | client_secret OR federated_token, never both |
| `notion`     | OAuth + integration secret | yes | block-tree depth + cursor | block tree → Markdown |
| `jira`       | Atlassian Cloud + DC | yes | `updated >= "<iso>"` JQL | ADF → text walker |
| `confluence` | Atlassian Cloud + DC | yes | per-space cursor + `last_modified` | storage XHTML → text |
| `postman`    | API key | no | — | resolves `{{var}}` + secret-rotation interlock |
| `sharepoint` | Graph + Sites.Selected + WIF | yes | per-drive `deltaLink` | files + lists |
| `gdrive`     | Service account + DWD | yes | per-drive `nextPageToken` | exports Google Docs natively |
| `salesforce` | JWT bearer flow (connected app) | yes | per-sObject `nextRecordsUrl` | Cases / Accounts / Opportunities / Users |
| `ci-logs`    | per-CI tokens | yes | per-pipeline run id | GitHub Actions / CircleCI / Buildkite / Jenkins |

### §15 — Object storage

| Connector | Auth | Incremental | Notes |
| --- | --- | --- | --- |
| `aws`        | multi-account AssumeRole | yes (via versioning) | reads object versions for "deleted" findings |
| `gcs`        | service account + WIF + Cloud Asset Inventory | yes | inventory bootstrap then list-only delta |
| `azure-blob` | managed identity + Lighthouse delegation | yes | cross-tenant via Lighthouse |
| `oci`        | bearer realm token | no | OCI Distribution Spec v1.1 with image-index multi-arch + layer dedup |

### §16 — Databases & search

| Connector | Auth | Sampling | Cursor |
| --- | --- | --- | --- |
| `postgres` | IAM auth (RDS / CloudSQL / native) | reservoir (`n=299`, 95%@1%) | replica-only via `pg_is_in_recovery()` |
| `mysql`    | IAM / native | `RAND()` for ≤100k, CRC32 hash bucket above + `MAX_EXECUTION_TIME` hint | replica enforced via `read_only=ON` + `SHOW REPLICA STATUS` |
| `mongodb`  | SCRAM / x509 / OIDC | `$sample` aggregation | secondary read-preference enforced |
| `redis`    | ACL with `+@read -@write -@admin -@dangerous -@scripting -@all` | full key SCAN | refuses if ACL is over-privileged |
| `elasticsearch` | API key / basic / bearer | `function_score` random_score | PIT + `search_after` (deep-pagination safe); supports OpenSearch flavor |
| `bigquery` | service account + WIF | `TABLESAMPLE SYSTEM (n PERCENT)` | dry-run cost cap before execution |
| `snowflake` | key-pair JWT | `SELECT … SAMPLE (n ROWS)` | dedicated XS warehouse, `STATEMENT_TIMEOUT_IN_SECONDS` |

## Cross-cutting invariants

Every connector in this repo ships with the following guarantees so the
scheduler, FindingsStore, and operator workflows treat them uniformly:

* **Cursor is `str`** — `pleno_pii_scanner.sources.base.Cursor` is a
  type alias for `str`, never a class. Connectors JSON-encode their
  resume state.
* **Hermetic tests via `httpx.MockTransport`** — no live API call ever
  enters the test path. Coverage gate is ≥99% per package.
* **Owned-vs-injected client lifecycle** — every `__init__` accepts an
  optional `httpx.AsyncClient`; `close()` only `aclose()`s the client
  the connector owns.
* **Stable `id` derivation** — when the operator does not supply
  `config.id`, it is derived from a SHA-256 of the source-locating
  config (workspace ids, hosts, indices) without ever incorporating raw
  secrets.
* **Filter contract** — `SourceFilter.include` / `exclude` are matched
  against the connector's path scheme using `fnmatch`. Connectors push
  the predicate server-side when the API supports it (JQL `updated >=`,
  S3 `Prefix=`, Slack `oldest=`).
* **`Capabilities` is the scheduler contract** — `incremental` lets the
  scheduler skip a full re-walk when a checkpoint exists;
  `content_hash_delta` short-circuits unchanged ETag/digest before
  re-fetching the body; `max_concurrent_fetches` bounds the per-connector
  asyncio Semaphore.

## Auth registry

Connectors never persist credentials. Resolved credentials come from
`CredentialBroker` (#5). The mapping below documents what shape each
connector expects from the broker so operators can pre-provision the
right secret store entries.

| Source kind | Broker shape |
| --- | --- |
| GitHub App        | `{"app_id", "installation_id", "private_key"}` |
| GitLab            | `{"token"}` (PAT) |
| Bitbucket Cloud   | `{"username", "app_password"}` |
| Bitbucket Server  | `{"bearer_token"}` |
| Azure DevOps      | `{"pat"}` or federated `{"workload_identity_token"}` |
| Slack             | `{"bot_token"}` or `{"user_token"}` or `{"discovery_token"}` |
| Discord           | `{"bot_token"}` |
| Microsoft Teams   | `{"tenant_id", "client_id", "client_secret \| federated_token"}` |
| Notion            | `{"integration_token"}` |
| Jira              | `{"email", "api_token"}` (cloud) / `{"bearer_token"}` (DC) |
| Confluence        | same as Jira |
| Postman           | `{"api_key"}` |
| SharePoint        | same as MS Teams plus `Sites.Selected` consent |
| Google Drive      | `{"service_account_json", "impersonate"}` (DWD) |
| Salesforce        | `{"client_id", "username", "private_key_pem"}` (JWT bearer) |
| AWS S3            | `{"role_arn"}` per account; sts:AssumeRole chain |
| GCS               | `{"service_account_json"}` or WIF `{"federated_token"}` |
| Azure Blob        | `{"tenant_id", "client_id", "managed_identity \| federated_token"}` |
| OCI registry      | none required for public; `{"username", "password"}` for private; bearer realm exchange happens at request time |
| PostgreSQL        | `{"dsn"}` or `{"iam_token"}` |
| MySQL             | `{"dsn"}` or `{"iam_token"}` |
| MongoDB           | `{"uri"}` |
| Redis             | `{"username", "password"}` (ACL user) |
| Elasticsearch     | `{"api_key" \| "basic_user/basic_password" \| "bearer_token"}` |
| BigQuery          | `{"service_account_json \| federated_token"}` |
| Snowflake         | `{"account", "user", "private_key_pem"}` (key-pair JWT) |
| CI logs           | per-CI: `{"github_token"}`, `{"circleci_token"}`, `{"buildkite_token"}`, `{"jenkins_username", "jenkins_api_token"}` |

## Cost / quota notes

The connectors that hit metered APIs default to conservative budgets so
a runaway scan cannot incur surprise bills:

* **BigQuery** — `max_bytes_billed=100GB` enforced via dry-run before
  every query.
* **Snowflake** — dedicated `PII_SCANNER_XS` warehouse + 60-second
  `STATEMENT_TIMEOUT_IN_SECONDS`.
* **Postgres / MySQL** — replica-only execution + reservoir / RAND
  sampling capped at 299 rows per table for 95% confidence at 1%
  prevalence.
* **Elasticsearch** — `sample_fraction` knob applies `function_score`
  random_score so a 5% sample never reads more than 5% of shards.

## Adding a new connector

1. Copy `packages/pii-scanner-postman/` as a starting template.
2. Implement the five Protocol methods. Keep `Cursor` as JSON-encoded
   string. Inject `httpx.AsyncClient | None` for hermetic testing.
3. Register the entry point: `[project.entry-points."pleno_pii_scanner.connectors"] kind = "package_module:SPEC"`.
4. Add the package to `pyproject.toml` `[tool.uv.workspace]` members
   alphabetically.
5. Write `tests/test_connector.py` with `httpx.MockTransport`; gate is
   ≥99% line coverage.
6. Tag-push `pii-scanner-<kind>/v0.1.0` triggers PyPI trusted
   publishing via `.github/workflows/release-pypi.yml`.
