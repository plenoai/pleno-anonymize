# pleno-pii-scanner-saas-retriever

`saas-retriever`-backed `SourceConnector` for `pleno-pii-scanner`.
API-only adapter that wraps any
[saas-retriever](https://pypi.org/project/saas-retriever/) connector
(today: org-wide GitHub; slack / jira / confluence / notion / gitlab /
bitbucket land in subsequent saas-retriever releases) behind the
`SourceConnector` protocol.

This wheel replaces the previous `pleno-pii-scanner-saas-scraper`
package, which drove a real Chrome session via Playwright. saas-retriever
is API-only — no browser, no Chromium binary, no SAML / SSO race.

## Why

`pleno-anonymize` ships dedicated, enterprise-grade SourceConnectors for
Slack, GitHub (App auth + GHES), Jira, Confluence, Bitbucket, GitLab,
Notion. Use those when you need GitHub App auth, JQL filters, Slack
Discovery API, or any provider-specific knob.

This wheel exists for the simpler case: a single API token, a one-shot
SaaS scan, no extra wheels to install per provider. As saas-retriever
absorbs more auth modes, the per-SaaS wheels become deletable.

## Install

```sh
uv pip install pleno-pii-scanner-saas-retriever
```

No system dependencies — saas-retriever talks HTTPS via httpx.

## Usage

A single connector kind (`saas-retriever`) covers every provider; the
underlying connector is selected via `config.connector_kind`.

```yaml
sources:
  - kind: saas-retriever
    config:
      connector_kind: github
      owner: plenoai
      # repo: <single-repo>     # optional — omit for org-wide enumeration
      resources: [code, issues, prs]
      token: ${GITHUB_TOKEN}    # or rely on the env var / `gh auth token`
      include_archived: false
      id: github-plenoai        # optional override
```

Or programmatically:

```python
from pleno_pii_scanner_saas_retriever import build_connector

connector = build_connector({
    "connector_kind": "github",
    "owner": "plenoai",
    "resources": ["code", "issues", "prs"],
})
async for ref in connector.discover(filter, cursor=None):
    async for doc in connector.fetch(ref):
        ...  # feed into the regex / NER pipeline as usual
await connector.close()
```

## Supported connector kinds

Whatever `saas-retriever` registers at the time of the call — currently
`github`. New connectors land upstream and become immediately available
here without changes to this wheel.

## Limits

- No incremental cursor — saas-retriever connectors don't expose a
  delta token. Pair with `--since` for time-bound filtering.
- Binary payloads pass through verbatim; downstream ContentExtractor
  handles MIME sniffing.

## License

AGPL-3.0-or-later.
