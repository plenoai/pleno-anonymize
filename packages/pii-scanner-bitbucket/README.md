# pleno-pii-scanner-bitbucket

Bitbucket Cloud + Bitbucket Server (Data Center) `SourceConnector` wheel
for `pleno-pii-scanner`. Task #19 / ADR-0007 §13.

Both flavors share one connector kind (`bitbucket`) and one wheel; the
flavor is selected by config:

```toml
[source.acme-cloud]
kind   = "bitbucket"
flavor = "cloud"
workspace = "acme"

[source.acme-server]
kind     = "bitbucket"
flavor   = "server"
base_url = "https://bitbucket.acme.internal"
project  = "PROD"
ca_bundle_path = "/etc/pki/acme-root.pem"
```

Auth (resolved by the core CredentialBroker, passed in `_credential`):

| flavor | accepted payload keys |
|--------|-----------------------|
| cloud  | `username` + `app_password`, **or** `access_token` (workspace token, Bearer) |
| server | `access_token` (HTTP access token, Bearer), **or** `username` + `password` (basic PAT) |

Repository content scan piggybacks on `git clone --depth=1` (same shell
seam the builtin `github` connector uses) so the pipeline does not
diverge between git hosts. The clone command is injectable via
`clone_fn` for hermetic tests.
