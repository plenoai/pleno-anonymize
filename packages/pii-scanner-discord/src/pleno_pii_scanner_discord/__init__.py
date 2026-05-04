"""Discord SourceConnector for pleno-pii-scanner (ADR-0007 §13)."""

from pleno_pii_scanner_discord.connector import (
    DiscordConfig,
    DiscordConnector,
    SPEC,
)

__all__ = ["DiscordConfig", "DiscordConnector", "SPEC"]
