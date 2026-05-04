# pleno-pii-scanner-postgres

PostgreSQL SourceConnector for [pleno-pii-scanner](https://github.com/plenoai/pleno-anonymize), implementing ADR-0007 §16 (production-safe datastore scanning).

## Features

- **Replica enforcement** — refuses to connect to a primary by default (`pg_is_in_recovery()` check); CI integration tests fail when pointed at a writable endpoint.
- **Statement timeout** — every query runs under a `SET LOCAL statement_timeout = '30s'` guard so a runaway scan cannot stall production OLTP workloads.
- **Reservoir sampling** — `TABLESAMPLE BERNOULLI` + `LIMIT n` (default 300) per table for 95% confidence at p=1% PII detection (Algorithm L bound, see ADR §16). Avoids full-table scans of 10⁹-row tables.
- **Dedicated pool** — capped at 2 connections by default so the connector can never starve the production app of pool slots.
- **Column type filtering** — only enumerates `varchar`, `text`, `citext`, `jsonb`, `xml`, and `bytea` columns. Numeric and date columns produce no findings and are skipped to keep the discover surface bounded.
- **IAM auth** — supports AWS RDS IAM tokens via the `iam` config block (uses `boto3.client('rds').generate_db_auth_token`); falls back to standard libpq auth when absent.

## Install

```bash
pip install pleno-pii-scanner pleno-pii-scanner-postgres
```

## Use

```toml
# postgres-prod-replica.toml
dsn = "postgresql://scanner@prod-replica.internal:5432/app"
schemas = ["public", "billing"]
sample_rows = 300        # reservoir n; 95% conf at p=1%
statement_timeout = "30s"
require_replica = true
```

```bash
pleno-pii-scanner scan run postgres --config postgres-prod-replica.toml
```
