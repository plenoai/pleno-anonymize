# pleno-pii-scanner-mysql

MySQL `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Mirrors the production-safe shape of the PostgreSQL connector
(`pii-scanner-postgres`):

- **Replica enforcement** at connect time via
  `SHOW VARIABLES LIKE 'read_only'` AND `SHOW SLAVE STATUS` (or
  `SHOW REPLICA STATUS` on MySQL ≥8.0.22). Refuses to scan a
  primary by accident — same #1-bug protection PostgreSQL gets via
  `pg_is_in_recovery`.
- **`max_execution_time` SESSION hint** of 30 s on every query
  (MySQL 5.7+ uses the optimizer hint syntax
  `SELECT /*+ MAX_EXECUTION_TIME(30000) */ ...`).
- **Reservoir sampling** (n=300 default, math reproduces the
  PostgreSQL package). For tables ≤ 100 k rows: `ORDER BY RAND()
  LIMIT n`. Above: a primary-key hash sampling trick to stay
  within the time budget on multi-billion-row tables.
- **Pool capped at 2** so the scanner never starves the
  application of connections on a shared instance.
- **Binlog incremental** when `incremental=True`: opens a binlog
  stream from the last persisted (file, position) cursor and emits
  `INSERT`/`UPDATE` row events as Documents. Requires the user to
  have `REPLICATION SLAVE` + `REPLICATION CLIENT` grants.

## Config

```toml
[mysql]
dsn = "mysql://scanner:${MYSQL_PASSWORD}@replica.internal:3306/app"
schemas = ["app", "billing"]
sample_rows = 300
require_replica = true
incremental = false
```
