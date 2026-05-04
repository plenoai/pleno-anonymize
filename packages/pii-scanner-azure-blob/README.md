# pleno-pii-scanner-azure-blob

Azure Blob Storage enterprise connector wheel for `pleno-pii-scanner`.

Implements the `SourceConnector` Protocol from
`pleno_pii_scanner.sources.base` for Azure Blob Storage, with:

- Three auth modes — **Workload Identity** (federated OIDC token
  exchange via Microsoft Entra, no long-lived key in CI), **Managed
  Identity** (IMDS at `169.254.169.254`), and legacy **Account Key**
  (Shared Key signing per the Azure Storage REST spec).
- **Azure Lighthouse multi-account fan-out** — one scan job can list
  blobs across many `(subscription_id, storage_account)` pairs with
  independent credentials.
- Container discovery via explicit per-account list **or** per-account
  `?comp=list` enumeration.
- Paginated `?restype=container&comp=list` blob enumeration with XML
  `<NextMarker>` pagination, parsed with `xml.etree.ElementTree`.
- Streaming `GET` for content fetch, pinned to API version
  `2023-11-03` via `x-ms-version`.
- Soft-deleted / version-aware listing — soft-deleted blobs are
  skipped unless `include_versions=True`.
- Bounded-concurrency fan-out via `asyncio.Semaphore` (default 8).
- Customer-managed encryption keys (CMEK) passed through opaquely; key
  material is **never** decrypted or stored.
- 403 access denied surfaces a single warning `DocumentRef` per
  container rather than crashing the scan.

httpx-only — no `azure-storage-blob` SDK on the dependency surface so
tests stay hermetic with `httpx.MockTransport` and security teams audit
a small dependency graph (ADR-0007 §15).

```toml
[project.entry-points."pleno_pii_scanner.connectors"]
azure_blob = "pleno_pii_scanner_azure_blob:SPEC"
```

Discovered automatically by the core CLI:

```
pleno-pii-scanner scan azure_blob --source-config azure-blob.toml
```
