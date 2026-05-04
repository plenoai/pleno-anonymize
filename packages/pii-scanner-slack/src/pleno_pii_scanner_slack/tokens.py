"""Slack token classification.

Slack tokens carry their authentication shape in the prefix. The connector
must dispatch to the right API surface (conversations.* vs discovery.*) at
construction time — calling discovery.conversations.list with a `xoxb` bot
token returns `not_allowed_token_type` and counts against the bot's already
tight Tier 4 budget. We refuse to construct a misrouted client at all.

The mapping is fixed by Slack:

  xoxb-...  bot token            single workspace, conversations.* + files.*
  xoxp-...  user token           single workspace, conversations.* + files.* + users.*
  xoxa-...  org-wide / app-level Enterprise Grid org-wide,  discovery.*
  xoxe-...  refresh token        not used for API calls (rotation only)
  xoxs-...  legacy session token deprecated, refused

ADR-0007 §13 references the Discovery API path explicitly as the Tier 3
rate-limit avoidance strategy on Enterprise Grid; making the dispatch a
typed enum at the boundary keeps `xoxa` mistakes from silently falling
back to per-channel pagination.
"""

from __future__ import annotations

from enum import Enum


class SlackTokenType(Enum):
    """Slack token kind, derived from the token prefix.

    BOT and USER both drive the conversations.* path (single workspace);
    ORG drives discovery.*. The string values are the canonical short
    names used in error messages and metrics, not the raw prefix.
    """

    BOT = "bot"
    USER = "user"
    ORG = "org"


class InvalidSlackTokenError(ValueError):
    """Raised when a token does not match a supported Slack prefix.

    Prefer this over a bare ValueError so the registry factory can
    distinguish credential-misconfiguration from generic config errors
    when surfacing the failure to the operator.
    """


def classify_token(token: str) -> SlackTokenType:
    """Return the SlackTokenType corresponding to `token`'s prefix.

    Empty strings, unknown prefixes, and the deprecated xoxs/xoxe forms
    raise InvalidSlackTokenError with a message that names what was
    accepted instead of dumping the (sensitive) token. The token value
    is never echoed.
    """
    if not token:
        raise InvalidSlackTokenError(
            "slack token is empty; expected one of xoxb- / xoxp- / xoxa-"
        )
    # Match by prefix only — the body of a Slack token is opaque base32-
    # ish and we never inspect it. We compare with `startswith` instead of
    # splitting on `-` because some tokens contain interior dashes.
    if token.startswith("xoxb-"):
        return SlackTokenType.BOT
    if token.startswith("xoxp-"):
        return SlackTokenType.USER
    if token.startswith("xoxa-") or token.startswith("xoxa2-"):
        # xoxa2-* is the newer Enterprise Grid org-wide format; both map
        # to the Discovery API. Slack ships them interchangeably across
        # docs and the SDK accepts both unprefixed in headers.
        return SlackTokenType.ORG
    raise InvalidSlackTokenError(
        "unsupported slack token prefix; expected xoxb-, xoxp-, or xoxa- "
        "(legacy xoxs-/xoxe- and arbitrary other prefixes are refused)"
    )


__all__ = ["InvalidSlackTokenError", "SlackTokenType", "classify_token"]
