# pleno-pii-scanner-ci-logs

`SourceConnector` for CI build logs across four vendors (Task #41, ADR-0007 §13):

| flavor              | endpoint shape                                                     | auth header                  | paginator         |
| ------------------- | ------------------------------------------------------------------ | ---------------------------- | ----------------- |
| `github_actions`    | `https://api.github.com/repos/{owner}/{repo}/actions/runs`         | `Authorization: Bearer <PAT>`| `?per_page&page`  |
| `circleci`          | `https://circleci.com/api/v2/project/gh/{owner}/{repo}/job`        | `Circle-Token: <token>`      | `?page-token`     |
| `buildkite`         | `https://api.buildkite.com/v2/organizations/{org}/pipelines/{pipe}`| `Authorization: Bearer <tok>`| `Link` header     |
| `jenkins`           | `{base_url}/api/json?tree=jobs[name,builds[number,url]]`           | HTTP Basic                   | none (single GET) |

Build logs leak secrets (`echo $AWS_SECRET_ACCESS_KEY`, env-dump steps,
stack traces with DSNs). Common knobs:

```toml
[ci_logs]
flavor = "github_actions"   # | "circleci" | "buildkite" | "jenkins"
since = "2026-05-01T00:00:00Z"
max_builds = 50
failed_only = true          # default false; only failed/errored builds
```

GitHub Actions `/runs/{id}/logs` returns a zip. We unpack each `.txt`
member as its own `Document`, with a per-member size cap (default
50 MiB) to defend against zip bombs. One bad log line cannot abort
the scan: malformed payloads, decode errors, and per-member I/O
failures are logged via per-flavor diagnostics and skipped.
