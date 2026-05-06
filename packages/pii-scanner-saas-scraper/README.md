# pleno-pii-scanner-saas-scraper

`saas-scraper`-backed `SourceConnector` for `pleno-pii-scanner`. Lets the
PII / secret scanner reach SaaS surfaces that have no clean API path —
SSO-locked Slack workspaces, SAML-restricted GitHub orgs, Notion sidebars
that only render in the web UI — by driving a real Chrome session via
[saas-scraper](https://pypi.org/project/saas-scraper/).

## Why

`pleno-anonymize` already ships dedicated REST connectors for Slack,
GitHub, Jira, Confluence, Bitbucket, GitLab, Notion. Use those when API
access is the cleanest path. This wheel exists for the cases where it
isn't:

- Workspaces where SCIM provisioning blocks bot-token issuance.
- SAML-locked GitHub orgs that don't allow token-scoped access from
  scanner roles.
- Surfaces that only exist in the web UI (Notion sidebar entries, Slack
  canvas, Jira inline previews).

Auth is delegated to the user's existing Chrome profile — no token
plumbing in the scanner config.

## Install

```sh
uv pip install pleno-pii-scanner-saas-scraper
playwright install chromium  # one-time browser binary
```

## Usage

A single connector kind (`saas-scraper`) covers every provider; the
underlying scraper is selected via `config.scraper_kind`.

```yaml
sources:
  - kind: saas-scraper
    config:
      scraper_kind: github
      owner: plenoai
      repo: saas-scraper
      resources: [code, issues, prs]
      headless: true
      profile_dir: ~/.cache/saas-scraper-profile
      id: github-plenoai-saas-scraper  # optional override
```

Or programmatically:

```python
from pleno_pii_scanner_saas_scraper import build_connector

connector = build_connector({
    "scraper_kind": "slack",
    "workspace": "acme",
    "headless": True,
})
async for ref in connector.discover(filter, cursor=None):
    async for doc in connector.fetch(ref):
        ...  # feed into the regex / NER pipeline as usual
await connector.close()
```

## Supported scraper kinds

Whatever `saas-scraper` registers at the time of the call — currently
slack, github, gitlab, bitbucket, jira, confluence, notion. New
connectors land in saas-scraper and become immediately available here
without changes to this wheel.

## Limits

- Concurrency is 1 — every saas-scraper call serialises on the single
  Chrome instance owned by the connector. `Capabilities.max_concurrent_fetches=1`.
- No incremental cursor — the web UI doesn't expose a delta token,
  so every scan is a full re-walk. Pair with `--since` for time-bound
  filtering.
- Binary payloads pass through verbatim; downstream
  ContentExtractor handles MIME sniffing.

## License

AGPL-3.0-or-later.
