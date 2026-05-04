# pleno-pii-scanner-notion

Notion `SourceConnector` wheel for `pleno-pii-scanner`.

Scans every page, database, and block content the integration has been
explicitly shared with. Ships behind the registry entry-point group
`pleno_pii_scanner.connectors` (kind `notion`), routed by the unified CLI
as `pleno-pii-scanner scan notion ...`.

## Auth

**Internal Integration Token only** (Bearer). The token must come from a
workspace-internal integration that has been shared with the pages /
databases you want to scan. OAuth public integrations are out of scope
for v1 — the connector deliberately rejects every prefix other than the
`secret_*` / `ntn_*` Notion uses for internal integration tokens.

## Discovery modes

| Mode | Config | Behavior |
|---|---|---|
| Search | (default) | `POST /v1/search` with empty query — every page + database the integration was shared with |
| Explicit pages | `pages: ["<id>", ...]` | scan only those pages and their descendants |
| Database query | `databases: ["<id>", ...]` | enumerate rows of each database via `/v1/databases/{id}/query` |

The three modes are independent and can be combined; results dedupe on
`(object_type, id)`.

## Block tree → Markdown

Every page or row's block tree is materialized to Markdown so the
existing detector stack (regex + NER) sees the same surface text a
Notion reader would. Supported block types:

`paragraph`, `heading_1`, `heading_2`, `heading_3`, `bulleted_list_item`,
`numbered_list_item`, `to_do`, `toggle`, `code` (preserves language fence),
`quote`, `callout`, `divider`, `table`, `table_row`, `equation`, `embed`,
`bookmark`, `link_to_page`, `child_page`, `child_database`.

Unknown block types emit `<!-- unsupported: {type} -->` so a future
Notion API change cannot crash a scan.

## Database row → Markdown

Properties are serialized one per line as `prop_name: value`. Supported
property types: `title`, `rich_text`, `number`, `select`, `multi_select`,
`status`, `date`, `email`, `phone_number`, `url`, `people`, `files`,
`checkbox`, `relation`, `formula`, `rollup`. Low-signal metadata
(`created_time`, `last_edited_time`, `created_by`, `last_edited_by`) is
skipped.

## Rate limiting

Notion returns `429` with a `Retry-After` header. The connector raises
`RateLimited` on 429 so the scheduler's AIMD bucket can shrink the
per-tenant rate; concurrency defaults to 3 (Notion's published limit is
~3 RPS averaged).

## Archived

Archived pages / blocks are skipped by default; pass `include_archived=True`
to keep them.
