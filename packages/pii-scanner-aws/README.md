# pleno-pii-scanner-aws

AWS S3 enterprise connector wheel for `pleno-pii-scanner`.

Implements the `SourceConnector` Protocol from
`pleno_pii_scanner.sources.base` for AWS S3, with:

- Multi-account fan-out via STS `AssumeRole` chains
  (`OrganizationAccountAccessRole` for AWS Organizations).
- Incremental discovery via `(continuation_token, last_modified_floor)`
  cursor.
- TB-scale object streaming via ranged `GetObject` (`DocumentChunk`).
- Reservoir sampling (n=300, ADR-0007 §16) for buckets with >10⁶ keys.
- S3 Inventory manifest preference for petabyte-scale buckets.
- AIMD rate-limit feedback on `503 SlowDown` / `429`.
- Glacier-class objects skipped by default.

Install separately so security teams can audit the AWS SDK dependency
without dragging in the rest of the connector matrix (ADR-0007 §13).

```toml
[project.entry-points."pleno_pii_scanner.connectors"]
aws-s3 = "pleno_pii_scanner_aws:SPEC"
```

Discovered automatically by the core CLI:

```
pleno-pii-scanner scan aws-s3 --source-config aws.toml
```
