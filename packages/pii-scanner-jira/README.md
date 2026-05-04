# pleno-pii-scanner-jira

Atlassian Jira `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Jira issue descriptions, summaries, and comment threads are a top
source of leaked PII and credentials in the wild — engineers paste
production stack traces with embedded API keys, dump customer support
tickets containing email/phone, and attach DSNs in "reproduction
steps" sections. This connector scans every issue (and optionally
every comment) the API token can read.

Supports both **Cloud** (HTTP basic auth `email:api_token`) and **Data
Center** (bearer token).

## Auth

- **Cloud**: API token at https://id.atlassian.com/manage-profile/security/api-tokens.
  Pair with the account email; sent as HTTP basic auth.
- **Data Center**: Personal Access Token (PAT). Sent as
  `Authorization: Bearer <token>`. The `email` field is ignored.

## ADF → text

Issue descriptions and comment bodies on Cloud arrive as Atlassian
Document Format (ADF) — a JSON tree of nodes. We walk the tree and
concatenate every `text` node, inserting newlines at `paragraph`,
`heading`, and `listItem` boundaries. We deliberately do **not**
depend on the heavy `atlaskit` ADF library; the walker is ~30 lines
and covers the long tail of node types by descending unconditionally.

## Incremental

Cursor encodes the latest seen `updated` timestamp (ISO-8601). On
resume the connector issues
`updated >= "<ts>" ORDER BY updated ASC` so the scheduler sees only
issues touched since the last successful run.

## Config

```toml
[jira]
base_url    = "https://acme.atlassian.net"
email       = "ops@acme.example"          # Cloud only; ignored when deployment="dc"
api_token   = "${JIRA_API_TOKEN}"
projects    = ["SEC", "INFRA"]            # optional JQL allowlist
include_comments = true                    # also scan every comment
deployment  = "cloud"                      # "cloud" | "dc"
```
