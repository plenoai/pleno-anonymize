# pleno-pii-scanner-salesforce

Salesforce `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Salesforce orgs accumulate PII in places customers rarely audit:
free-text `Case.Description`, `Case.Comments`, `Account.Description`,
`Opportunity.Description`, and `User.Email` / phone fields. Standard
support workflows pour customer-supplied data straight into these
columns — exactly the surface this connector enumerates.

## Auth — JWT Bearer flow (Connected App)

The connector uses the OAuth2 [JWT Bearer
flow](https://help.salesforce.com/s/articleView?id=sf.remoteaccess_oauth_jwt_flow.htm)
because it is the only supported headless flow that does not require
storing a refresh token (rotated by Salesforce admins on Connected App
churn). Setup, once per org:

1. Create a Connected App: **Setup → App Manager → New Connected App**.
2. Enable OAuth Settings, add the certificate that pairs with your
   private key, and grant scopes `api refresh_token`.
3. Pre-authorize the app for the integration user
   (Manage → Permitted Users → Admin approved users are pre-authorized).
4. Note the Connected App's **Consumer Key** (`client_id`) and the
   integration **Username**.

Per scan, the connector mints an RS256 JWT (`iss=client_id`,
`sub=username`, `aud=login.salesforce.com`, `exp=now+3min`), POSTs it
to `/services/oauth2/token`, and caches the resulting access token
until 30 s before its declared expiry.

## sObjects scanned

`Case`, `Account`, `Opportunity`, `User` by default. Override via
the `sobjects` config tuple — every entry must be queryable by the
integration user (the connector calls `/sobjects/{name}/describe` to
enumerate fields, then runs `SELECT Id, <field>, ... FROM <name>`).

## Config

```toml
[salesforce]
instance_url = "https://acme.my.salesforce.com"
client_id = "${SALESFORCE_CONNECTED_APP_KEY}"
username = "scanner@acme.com"
private_key_pem = "${SALESFORCE_JWT_PRIVATE_KEY_PEM}"
sobjects = ["Case", "Account", "Opportunity", "User"]
api_version = "v60.0"
page_size = 200
```
