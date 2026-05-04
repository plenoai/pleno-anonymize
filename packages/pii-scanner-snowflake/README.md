# pleno-pii-scanner-snowflake

Snowflake `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Snowflake warehouses commonly hold the most-PII-dense tables in the
business — customer rosters, support tickets, billing addresses,
clickstream payloads. Scanning them safely matters more than
scanning them quickly.

This connector enumerates databases / schemas / tables via the
[Snowflake SQL REST API v2](https://docs.snowflake.com/en/developer-guide/sql-api/intro)
and yields one `Document` per sampled row.

## Why these defaults

* **Dedicated XS warehouse (`PII_SCANNER_XS`).** A dedicated
  warehouse means PII scans never contend for slots with the BI /
  ELT workload. XS is the smallest billable size — it auto-suspends
  in 60 s and costs ~$0.0011/credit-second.
* **`ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS`.** A
  hard ceiling per statement. A runaway `SELECT * FROM huge_table
  SAMPLE` can otherwise burn an entire monthly credit budget on
  one bad query.
* **Reservoir sampling via `SAMPLE (n ROWS)`.** Snowflake's
  `SAMPLE` is server-side and bypasses full scan; required for
  multi-billion-row fact tables.
* **Key-pair JWT auth.** Snowflake's password auth path is being
  deprecated for service users. JWT signed by an RSA private key
  rotates without a control-plane round-trip and never appears
  on the wire.

## Auth

Generate an RSA key pair, register the public key on the Snowflake
user (`ALTER USER svc_pii_scanner SET RSA_PUBLIC_KEY = '...'`),
and pass the private-key PEM via the connector config. The
connector signs a short-lived JWT (`RS256`) and presents it as
`Authorization: Bearer <jwt>` plus
`X-Snowflake-Authorization-Token-Type: KEYPAIR_JWT`.

## Config

```toml
[snowflake]
account = "abc12345.us-east-1"
user = "SVC_PII_SCANNER"
private_key_pem = "${SNOWFLAKE_PRIVATE_KEY}"
warehouse = "PII_SCANNER_XS"           # dedicated, XS, auto-suspend
role = "PII_SCANNER_RO"                # read-only role
databases = ["PROD"]                   # optional allowlist
schemas = ["PUBLIC", "ANALYTICS"]      # optional allowlist
sample_rows = 1000                     # per-table cap
statement_timeout_seconds = 60         # session ceiling
```
