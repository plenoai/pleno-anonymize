# pleno-pii-scanner-confluence

Atlassian Confluence `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Confluence pages routinely accumulate credentials in incident write-ups,
runbooks, vendor onboarding docs, and the "TEMP — please delete" pages
nobody ever deletes. Scanning a single space typically surfaces 10x
more findings than the corresponding code repo.

This connector targets both deployment models:

* **Cloud** — `/wiki/api/v2/pages` (cursor-paginated v2 REST), HTTP basic
  with `email:api_token`.
* **Data Center / Server** — `/rest/api/content` (start/limit v1 REST),
  bearer-token auth.

The page body comes back as **Confluence storage XHTML**. We walk the
XHTML tree with `xml.etree.ElementTree` to recover plain text — block
elements (`p`, `h1..h6`, `li`) emit newlines, and `ac:structured-macro`
blocks surface their `name=` and inner text so secrets parked inside a
`{code}` macro are still detected. No BeautifulSoup dependency.

Attachments are surfaced as metadata-only `DocumentRef`s (binary download
is opt-out via `include_attachments_meta=False`); the binary fetch belongs
to a dedicated content-extractor stage.

## Auth

* Cloud: Atlassian account email + API token from
  <https://id.atlassian.com/manage-profile/security/api-tokens>. Sent as
  HTTP basic `email:api_token`.
* DC / Server: personal access token. Sent as `Authorization: Bearer
  <token>`.

## Config

```toml
[confluence]
base_url = "https://acme.atlassian.net"
email = "ops@acme.example"
api_token = "${CONFLUENCE_TOKEN}"
deployment = "cloud"                  # or "dc"
spaces = ["ENG", "OPS"]               # optional allowlist (space keys)
include_attachments_meta = true       # emit DocumentRef per attachment
```

## Incremental

The cursor encodes the highest `last_modified` timestamp seen plus the
per-space pagination cursor. On resume the connector orders by
`lastmodified` and skips pages already covered by the prior run.
