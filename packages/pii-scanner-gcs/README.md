# pleno-pii-scanner-gcs

Google Cloud Storage enterprise connector wheel for `pleno-pii-scanner`.

Implements the `SourceConnector` Protocol from
`pleno_pii_scanner.sources.base` for GCS, with:

- Three auth modes — service-account JSON key, **Workload Identity
  Federation** (OIDC token exchange, no long-lived key in CI), and
  Application Default Credentials (env / metadata server).
- Bucket discovery via explicit list **or Cloud Asset Inventory**
  (`storage.googleapis.com/Bucket` query) for enumerate-without-hardcoding.
- Paginated `objects.list` with `prefix` and `glob` filters.
- Streaming `objects.get?alt=media` for content fetch.
- Object versioning — scans live versions when bucket has versioning
  enabled; soft-deleted are skipped unless `include_deleted=True`.
- Bounded-concurrency fan-out via `asyncio.Semaphore` (default 8).
- Customer-managed encryption keys (CMEK) passed through opaquely; key
  material is **never** stored or logged.
- 403 access denied surfaces a single warning `DocumentRef` per bucket
  rather than crashing the scan.

httpx-only — no `google-cloud-storage` SDK on the dependency surface so
tests stay hermetic with `httpx.MockTransport` and security teams audit
a small dependency graph (ADR-0007 §13).

```toml
[project.entry-points."pleno_pii_scanner.connectors"]
gcs = "pleno_pii_scanner_gcs:SPEC"
```

Discovered automatically by the core CLI:

```
pleno-pii-scanner scan gcs --source-config gcs.toml
```
