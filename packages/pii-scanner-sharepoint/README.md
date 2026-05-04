# pleno-pii-scanner-sharepoint

Microsoft SharePoint `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Walks every document library in every site the app can see via
Microsoft Graph, paginates with the `/delta` endpoint so subsequent
runs only see new or changed files, and yields each file (and
optionally each list item) as a Document.

## Auth

Microsoft Entra app with the **`Sites.Selected`** application
permission plus `Files.Read.All`. Two client-credential modes:

- `client_secret` — classic confidential client. Sent to the Entra
  v2 token endpoint as `client_secret`.
- `federated_token` — pre-fetched OIDC JWT (workload identity from
  GHA, AKS, Cloud Run, …) sent as `client_assertion` with
  `client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`.
  No long-lived secret in the deployment.

Exactly one of the two must be supplied.

## Sites.Selected grant model

`Sites.Selected` is the *narrowest* SharePoint app permission. It
grants nothing by default — a tenant administrator must explicitly
grant the app access to **each individual site** via the Graph
`POST /sites/{id}/permissions` endpoint:

```json
{
  "roles": ["read"],
  "grantedToIdentities": [{
    "application": { "id": "<client-id>", "displayName": "pleno-scanner" }
  }]
}
```

Without this per-site grant, `GET /sites/{id}/drives` returns 403.
Operators should pin the `sites = (...)` allowlist to the exact set
that has been granted, so a misconfigured tenant fails loudly with
a 403 on a known site rather than silently returning zero results
from `?search=*`.

## Delta semantics

Per-drive `/root/delta` returns a `@odata.deltaLink` that the
connector persists as the cursor. On resume, that link is fetched
verbatim and only items changed since the prior run come back.
Folders are skipped. Files larger than `max_file_size_bytes` (default
100 MiB) are still yielded as refs (so the operator can audit them)
but `fetch()` returns no Document.

## Config

```toml
[sharepoint]
tenant_id = "00000000-0000-0000-0000-000000000000"
client_id = "11111111-1111-1111-1111-111111111111"
client_secret = "${MS_GRAPH_CLIENT_SECRET}"   # OR federated_token = "..."
sites = ["site-id-1", "contoso.sharepoint.com:/sites/team"]
include_lists = false
max_file_size_bytes = 104857600
```
