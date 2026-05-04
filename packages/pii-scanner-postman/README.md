# pleno-pii-scanner-postman

Postman `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Postman collections are a top-3 source of leaked secrets in the
wild — they routinely embed API keys, OAuth tokens, and database
DSNs in `request.url`, `request.header`, `request.body.raw`, and
script blocks (`event.script.exec`).

This connector pulls every collection in every workspace the API
key has access to, **resolves environment variables** so a key
hiding behind `{{api_key}}` is detected, and emits each request
node + script as a Document.

## Auth

Postman API key (single key, account-scoped). Generate from
Postman → Settings → API keys. Pass via `X-Api-Key` header per
the official spec.

## Secret rotation interlock

When the connector detects what looks like a hot credential
(matches one of the configured patterns in `interlock_patterns`,
default empty), it does NOT log the matching value. The finding
is still emitted via FindingsStore (envelope-encrypted), but the
connector's own logs scrub the matching content. This keeps the
scanner from becoming the leak channel during rotation incidents.

## Config

```toml
[postman]
api_key = "${POSTMAN_API_KEY}"
workspaces = ["my-team-workspace-id"]    # optional; default = every workspace key has access to
include_environments = true               # also scan environment values
include_examples = true                   # response examples often contain real data
```
