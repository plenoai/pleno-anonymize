# pleno-pii-scanner-github

Enterprise GitHub `SourceConnector` for `pleno-pii-scanner` (ADR-0007 §13).

## Why a separate wheel

The core wheel ships a builtin `github` connector that shells out to `git
clone --depth=1` + the `gh` CLI. It is zero-dependency and fine for a single
developer scanning their own repos, but it has hard limits in production:

- `gh repo list --limit 1000` silently truncates orgs with >1000 repos
- no GitHub App auth (only PATs and `gh auth login` flow)
- no GHES support (`gh` works, but cloning over HTTPS still needs a PAT)
- shallow-clone disk pressure when scanning hundreds of large repos
- no rate-limit feedback to the scheduler — `gh` swallows `X-RateLimit-*`

This wheel replaces all of the above using direct `httpx` calls to the
REST + GraphQL APIs. It registers a separate connector kind, `github-app`,
so both wheels can coexist (the builtin remains for users who want zero
deps).

## Connector kind

```
github-app
```

Required scopes: `contents:read`, `metadata:read` (and `members:read` when
enumerating an org or enterprise).

## Config (TOML)

```toml
[source]
kind = "github-app"
# Exactly one of repo / org / enterprise is required.
repo = "octocat/hello-world"
# org = "plenoai"
# enterprise = "acme-inc"
base_url = "https://api.github.com"   # GHES: "https://ghe.example.com/api/v3"
include_archived = false
```

Credentials flow through `CredentialBroker.get("github-app", name)` and
must carry an `app_id`, an `installation_id`, and a PEM-encoded
`private_key` in `Credential.payload`.

## Test

```sh
cd packages/pii-scanner-github
uv run pytest -q --cov=pleno_pii_scanner_github --cov-report=term-missing
```

All tests are offline — `httpx.MockTransport` is injected via the
connector's `transport` constructor argument; no real network is touched.
