# pleno-pii-scanner-discord

Discord `SourceConnector` for [pleno-pii-scanner](../pii-scanner/).

Scans every message in every text channel of every guild a Bot
token has access to, with **snowflake-cursor pagination** so the
scan resumes from where it left off without re-reading history.

## Bot prerequisites

Discord requires the bot to have the **Message Content** privileged
intent enabled. Without it, every message body comes back empty.
Enable in the Developer Portal → Bot → Privileged Intents.

The bot also needs `View Channel` and `Read Message History` on
each channel it should scan. Operators typically install the bot
with the `bot` scope and the `1024` (View Channel) + `65536`
(Read Message History) permission integers.

## Config

```toml
[discord]
token = "${DISCORD_BOT_TOKEN}"
guilds = ["123456789012345678"]   # optional; default = every guild bot is in
channel_types = [0, 5]             # 0=GUILD_TEXT, 5=GUILD_ANNOUNCEMENT
max_messages_per_channel = 5000   # cap per channel; 0 = no cap
include_threads = true
```

## Pagination

Discord uses **snowflake IDs** (64-bit timestamp-derived). Per
channel, the connector pages backwards via
`?before=<snowflake>` until it hits `max_messages_per_channel` or
the channel exhausts. The last-seen snowflake is persisted as the
incremental cursor; subsequent scans resume forward via
`?after=<snowflake>`.
