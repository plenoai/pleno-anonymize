# pleno-pii-scanner-mongodb

MongoDB `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Scans every collection across the configured databases via the
server-side `$sample` aggregation (never `find({})` cursor scans),
enforces a secondary read at startup so the scanner cannot accidentally
load the primary, and renders BSON documents (`ObjectId`, `Decimal128`,
`ISODate`, `Binary`) as text the regex / NER passes can match.

## Why secondary enforcement

A connection pointed at the primary will compete for working set with
the application; on a 100k-collection cluster, an unfiltered `$sample`
on the primary has been known to push p99 read latency by an order of
magnitude. The connector calls `client.admin.command("hello")` at
startup and refuses to scan unless `secondary: true`.

Override with `require_secondary=False` for development, but the
production path expects `mongodb://primary,secondary,...?readPreference=secondary`.

## Why $sample (not find())

`$sample` uses a server-side reservoir; `find().limit(N)` returns the
N earliest-inserted documents (heavily biased toward warm pages and
non-representative for PII detection). The default sample size is 299
— `n = log(0.05) / log(0.99) ≈ 299` for 95% confidence at p=1% PII
prevalence (ADR-0007 §16).

## Why change streams (incremental)

When `incremental=True`, the connector switches from `$sample` to
`db.collection.watch(resume_after=cursor)` so subsequent scans only
see documents that changed since the last cursor — avoiding the
re-sample cost on a stable, high-cardinality cluster.

## Config

```toml
[mongodb]
uri = "mongodb+srv://scanner:${MONGO_PASSWORD}@cluster0.mongodb.net/?readPreference=secondary"
databases = ["app", "audit"]   # default: all non-system DBs
collections = ["app.users"]    # default: all
sample_rows = 299              # default: 299 (95% conf @ 1% prevalence)
require_secondary = true       # default: true
incremental = false            # default: false; true → watch() instead
```

TLS via `mongodb+srv://` or the URI's `tls=true` query parameter.
Credentials may be embedded in the URI or supplied separately as
`username` / `password`.
