# pleno-pii-scanner-confluence

Atlassian Confluence SourceConnector for `pleno-pii-scanner`. Single
connector kind (`confluence`), two wire flavors selected by config:

- `flavor = "cloud"` — `https://{site}.atlassian.net/wiki/rest/api/...`
  plus the v2 host at `https://api.atlassian.com/ex/confluence/{cloudId}/...`
- `flavor = "datacenter"` — `<base_url>/rest/api/...`

## Configuration

```toml
[confluence]
flavor    = "cloud"                                 # or "datacenter"
base_url  = "https://acme.atlassian.net/wiki"       # cloud
# base_url = "https://confluence.acme.internal"     # datacenter
spaces    = ["ENG", "SEC"]                          # optional allowlist
```

Credentials follow the same three modes as the Jira connector:

- Basic — `{ "email": "...", "api_token": "..." }` (Cloud)
- Bearer — `{ "token": "..." }` (Data Center PAT, or Cloud OAuth)

## What it does

Per scan run:

1. Enumerate spaces (`/space`, paginated).
2. Per space, enumerate pages (`/space/{key}/content/page`).
3. For each page, request the `storage` body, the page's comments
   (`/content/{id}/child/comment`), and any attachment refs.
4. Convert storage-format XHTML → plain text (preserving rich-text
   bodies inside `<ac:structured-macro>`), append serialized
   attachment refs, and emit one `Document` per page.

The cursor is JSON-encoded as the highest `version.when` timestamp
seen; the next run only re-emits pages with a newer modification time.

See ADR-0007 §13.
