# pleno-pii-scanner-gdrive

Google Drive `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Google Drive is a top destination for accidental PII leaks: shared
spreadsheets with customer lists, Google Docs containing API keys
copy-pasted "for a teammate", and binary attachments uploaded into
shared drives without DLP review. This connector enumerates every
file across My Drive + every shared drive a service account can
see (via Domain-Wide Delegation), exports Google-native files as
plain text or PDF, and streams binary files for downstream scanning.

## Auth

Service-account JSON key with **Domain-Wide Delegation (DWD)**
enabled and the `https://www.googleapis.com/auth/drive.readonly`
scope authorised in Google Workspace Admin → Security → API controls.
Set `impersonate` to the subject email whose Drive should be walked
(usually a dedicated audit user). The connector mints a short-lived
OAuth2 access token via the
`https://oauth2.googleapis.com/token` JWT-bearer flow.

`impersonate` is required when `include_shared_drives=True` because
shared-drive enumeration requires a Workspace user identity, not the
raw service-account principal.

## Google Docs export

Native Google Docs / Sheets / Slides files have no downloadable bytes
— they live as opaque internal documents. The connector calls
`/files/{id}/export?mimeType=...` per the configured
`export_google_docs_as`:

- `text/plain` (default): yields a `Document.text` containing the
  exported plaintext. Cheapest to scan; preserves prose but loses
  formatting.
- `application/pdf`: yields a `Document.binary` with the PDF bytes.
  Use this when downstream extractors need layout (forms, tables).

## Config

```toml
[gdrive]
service_account_json = "${GDRIVE_SA_KEY_JSON}"  # entire SA key JSON
impersonate = "audit@example.com"               # DWD subject
drives = []                                     # optional allowlist
include_shared_drives = true
max_file_size_bytes = 104857600                 # 100 MiB
export_google_docs_as = "text/plain"            # or "application/pdf"
```

## Cursor

Per-drive `nextPageToken` map is JSON-encoded as the opaque cursor
string. Resume picks up at the in-progress page of each drive.
