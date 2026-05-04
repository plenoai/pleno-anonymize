# pleno-pii-scanner-redis

Redis `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Scans every key in a Redis instance via `SCAN` (never `KEYS`),
enforces ACL read-only at connect time so an over-privileged
credential cannot accidentally mutate state, and serialises the
six built-in value types (string, list, set, hash, sorted-set,
stream) into Documents the regex / NER passes can match.

## Why ACL read-only

Redis credentials don't carry a sniff-and-block guard at the
client side; an admin user with `+@all` can `FLUSHDB` by mistake.
The connector calls `ACL WHOAMI` + `ACL GETUSER` at startup and
refuses to scan unless the user has only read-class commands
enabled (`+@read +@connection -@write -@admin -@dangerous`).

Override with `enforce_readonly=False` for development, but the
production path expects a dedicated read-only user.

## Config

```toml
[redis]
url = "rediss://scanner:${REDIS_PASSWORD}@redis.internal:6379/0"
match = "user:*"          # SCAN MATCH glob; default "*"
count_hint = 1000          # SCAN COUNT hint; default 100
max_value_bytes = 1048576  # cap per-key body; default 1 MiB
```

TLS via `rediss://`. ACL via the URL userinfo or
`username` / `password` keys.
