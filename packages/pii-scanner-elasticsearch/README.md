# pleno-pii-scanner-elasticsearch

Elasticsearch / OpenSearch `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Scans `_source` documents across one or many indices using a
**Point-In-Time** (Elasticsearch) or **scroll-equivalent** snapshot so a
single scan sees a consistent view even while writes are happening.

Pagination uses `search_after` (the modern, deep-pagination-safe
alternative to `from`/`size`). Optional `random_score` query lets
operators sample a percentage of large indices instead of full-scanning.

## Auth

One of:

- API key — `Authorization: ApiKey <base64(id:api_key)>`
- Basic — `Authorization: Basic <base64(user:pass)>`
- Bearer — `Authorization: Bearer <jwt>`

## Config

```toml
[elasticsearch]
hosts = ["https://es.example.com:9200"]
api_key = "${ES_API_KEY}"          # or basic_user/basic_password, or bearer_token
indices = ["logs-*", "audit-*"]    # wildcards supported
flavor = "elasticsearch"            # or "opensearch"
sample_fraction = 0.05              # 5% random-score sample; 1.0 = full scan
text_fields = ["message", "raw"]    # which fields to concat into Document.text
page_size = 1000
```

## Flavor differences

- Elasticsearch uses `POST /_pit?keep_alive=...` to open a PIT and passes
  `pit.id` in subsequent search bodies.
- OpenSearch (no PIT in 1.x; PIT in 2.x via `_search/point_in_time`) —
  when `flavor="opensearch"` we open via `POST /_search/point_in_time`
  and supply `pit_id`. For older OpenSearch where PIT is unavailable, the
  connector falls back to scroll API automatically.
