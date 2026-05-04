# pleno-pii-scanner-gitlab

GitLab `SourceConnector` for `pleno-pii-scanner` (Task #18 / ADR-0007 §13).

Targets both **SaaS gitlab.com** and **self-managed CE/EE**: the only knob
is `base_url` (default `https://gitlab.com`). For self-managed instances
behind a private CA, set `ca_bundle_path` to a PEM bundle.

## Why a separate wheel

The core wheel does not ship a builtin GitLab connector. python-gitlab is
sync-only and pulls `requests` + `urllib3`; we reach the GitLab REST API
directly with `httpx` to keep the dep surface small and stay async-native
(the scheduler dispatches discover/fetch concurrently per connector).

## Connector kind

```
gitlab
```

Required scopes (PAT or OAuth): `read_api`, `read_repository`. Project
access tokens additionally need `read_repository` on the target project.

## Three auth modes

```toml
# 1. Personal Access Token (PAT) — simplest.
[credential]
auth = "pat"
token = "glpat-xxx"

# 2. OAuth2 application token (Bearer).
[credential]
auth = "oauth"
access_token = "<oauth-token>"

# 3. Project access token — scoped to a single project.
[credential]
auth = "project"
token = "glpat-project-xxx"
```

## Source config (TOML)

```toml
[source]
kind = "gitlab"
# Exactly one of project / group is required.
project = "acme/widgets"
# group = "acme"
base_url = "https://gitlab.com"        # self-managed: "https://gitlab.example.com"
ca_bundle_path = "/etc/ssl/private-ca.pem"  # self-managed only
include_archived = false
visibility = "private"                 # one of "private", "internal", "public", or unset
```

## Repository content scan

`fetch()` shallow-clones each project (`git clone --depth=1`) into a
process-temp dir owned by the connector. The clone lifecycle ends at
`close()`; failed clones rmtree themselves. Tests inject `clone_fn` to
avoid touching the real network.

## Enumeration

* `project="ns/path"` — single project.
* `group="ns"` — recursive walk of all subgroups via
  `/groups/:id/projects?include_subgroups=true`.

Pagination follows GitLab's `Link: <...>; rel="next"` header. 429 responses
back off via `RateLimited` (consumed by the scheduler's AIMD bucket).

## Test

```sh
uv run pytest -q --cov=pleno_pii_scanner_gitlab --cov-report=term-missing
```

All API calls are mocked with `httpx.MockTransport`; clones and
enumeration are mocked via injected callables. No real GitLab traffic.
