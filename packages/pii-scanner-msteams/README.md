# pleno-pii-scanner-msteams

Microsoft Teams `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Teams channels are a high-volume source of unintentionally shared
secrets — paste-buffer leaks of API keys, customer PII in incident
chats, and copy/pasted database connection strings. This connector
walks every team + channel a service principal can see, runs the
Graph **delta query** for incremental scans, and emits each message
(and optionally each reply) as a Document.

## Auth

Microsoft Entra ID (formerly Azure AD) application credentials,
client-credentials flow against `login.microsoftonline.com`.

Two credential modes:

* **Client secret** — classic shared secret. Set `client_secret`.
* **Federated token** — workload-identity federation (recommended
  on AKS, GitHub Actions OIDC, etc.). Set `federated_token` to the
  signed JWT assertion; the connector exchanges it via
  `urn:ietf:params:oauth:client-assertion-type:jwt-bearer`.

Exactly one of the two must be set.

Required Microsoft Graph application permissions (admin consent):

* `Group.Read.All`
* `Channel.ReadBasic.All`
* `ChannelMessage.Read.All`

## Incremental delta

The connector calls
`/v1.0/teams/{team}/channels/{channel}/messages/delta` and persists
the per-channel `@odata.deltaLink` in its `Cursor`. Resume on the
next run continues from the same point — no re-walk of history.

## Config

```toml
[msteams]
tenant_id = "${AAD_TENANT_ID}"
client_id = "${AAD_CLIENT_ID}"
client_secret = "${AAD_CLIENT_SECRET}"  # OR federated_token
teams = ["team-id-1"]                     # optional allowlist
include_replies = true
```
