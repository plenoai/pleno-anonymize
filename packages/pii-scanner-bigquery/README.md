# pleno-pii-scanner-bigquery

Google BigQuery `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

BigQuery warehouses concentrate analytics data — production exports,
event logs, joined identity tables — and a single misconfigured share
can leak millions of customer records. This connector samples each
table, lets the scanner inspect row content, and refuses to issue
queries that would burn unbounded warehouse cost.

## Why TABLESAMPLE

Full-table scans against a 10 TB events table cost more than the
entire scanner's quarterly budget and provide no extra signal once
PII is densely present. `TABLESAMPLE SYSTEM (n PERCENT)` reads a
deterministic block-level subset — the same sample on repeat scans
when the table has not been re-clustered — at a fraction of the cost.
We omit the clause when `sample_percent=100` so partition-pruning
and clustered reads still apply.

## Why dry-run cost cap

BigQuery bills by `bytesProcessed`. A single query against an
unpartitioned wide table can cost thousands of dollars. The
connector runs every query through `jobs.insert?dryRun=true` first,
reads `totalBytesProcessed` from the response, and refuses to
execute when the projection exceeds `max_bytes_billed`
(default 100 GiB). The same cap is also passed to BigQuery as
`maximumBytesBilled` so the warehouse refuses the job server-side
even if the dry-run estimate slipped under the limit.

## Auth

Two modes, exactly one is required:

- `service_account_json` — full SA key JSON (string). The connector
  signs a JWT and exchanges it at the Google OAuth token endpoint.
- `federated_token` — a Workload Identity Federation access token
  that has already been exchanged externally (GitHub Actions OIDC,
  EKS IRSA, etc.). The connector uses it directly as the bearer
  without ever holding SA key material.

The minimum IAM scope is `https://www.googleapis.com/auth/bigquery.readonly`.

## Config

```toml
[bigquery]
project = "my-gcp-project"
datasets = ["analytics", "marketing"]   # optional allowlist
service_account_json = "${BQ_SA_JSON}"
sample_percent = 1.0                     # TABLESAMPLE percent
max_bytes_billed = 107374182400          # 100 GiB cap
page_size = 1000
location = "US"
```
