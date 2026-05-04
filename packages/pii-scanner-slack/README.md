# pleno-pii-scanner-slack

Slack `SourceConnector` wheel for [`pleno-pii-scanner`](../pii-scanner).

Auto-routes between three Slack token shapes from the prefix:

| Prefix    | Path             | Scope                                        |
|-----------|------------------|----------------------------------------------|
| `xoxb-`   | `conversations.*`| Bot, single workspace                        |
| `xoxp-`   | `conversations.*`| User, single workspace, full visibility      |
| `xoxa-`   | `discovery.*`    | Enterprise Grid org-wide, all workspaces     |

The Discovery API path is the Tier 3 rate-limit avoidance route called out
in ADR-0007 §13 — it exposes every channel across every workspace in the
Grid with one auth and uses a separate (more generous) rate-limit budget.

## Install

```toml
# pyproject.toml
dependencies = [
    "pleno-pii-scanner",
    "pleno-pii-scanner-slack",
]
```

## CLI

This wheel does **not** add a CLI binary. Routing happens via the core
registry:

```sh
pleno-pii-scanner scan slack --source-config slack.toml
```

## Document path format

`slack://T<team>/C<channel>/<ts>` for messages, with `/files/F<file_id>`
appended for attachments. This is the canonical FindingsStore key
documented in ADR-0007 §1.

## Cursor format

JSON `{channel_id: oldest_ts}` for `xoxb`/`xoxp`, or
`{<team_id>:<channel_id>: ts}` for `xoxa` (channel ids may be reused
across workspaces in an Enterprise Grid).
