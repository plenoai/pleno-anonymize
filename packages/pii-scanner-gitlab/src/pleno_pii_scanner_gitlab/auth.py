"""GitLab credential classification.

GitLab supports three token types this connector handles, each with a
different request header. Misrouting (e.g. sending an OAuth token under
`PRIVATE-TOKEN`) silently 401s on some endpoints and 200s on others —
so we classify at the boundary and use a typed enum to drive the header
choice in `api.py`.

Auth modes (ADR-0007 §13):

  * PAT (Personal Access Token, prefix `glpat-`) → `PRIVATE-TOKEN: <t>`
  * Project access token (also `glpat-` prefix, scoped per project) →
    `PRIVATE-TOKEN: <t>`. Same header as PAT; the difference is purely
    in scope, surfaced for telemetry / error messages.
  * OAuth2 application token (no fixed prefix) → `Authorization: Bearer <t>`

We accept the OAuth path even when the token happens to start with
`glpat-` because GitLab's CI job tokens (`glcbt-`) and dedicated
deploy tokens (`gldt-`) also flow through the OAuth header in some
configurations. Operators select the mode explicitly via `auth=`.
"""

from __future__ import annotations

from enum import Enum


class GitlabAuthMode(Enum):
    """Auth mode chosen explicitly by the operator.

    `PAT` and `PROJECT` ride the same `PRIVATE-TOKEN` header; we keep
    them distinct so error messages and metrics can tell which one
    misfired (project tokens have a much narrower scope).
    """

    PAT = "pat"
    OAUTH = "oauth"
    PROJECT = "project"


class InvalidGitlabAuthError(ValueError):
    """Raised when the credential payload does not name a known auth mode.

    Subclassing ValueError keeps the registry factory's existing
    error-handling code path (it already special-cases ValueError as
    "credential misconfigured"), while still letting tests assert the
    specific class.
    """


# Set of legal `auth=` strings, materialised so the parser surfaces
# a typo (e.g. `pats` or `bearer`) instead of silently routing as PAT.
_LEGAL_MODES = {mode.value for mode in GitlabAuthMode}


def parse_auth_mode(raw: str) -> GitlabAuthMode:
    """Turn the credential payload's `auth=` string into a typed enum.

    The payload value is operator-supplied — we never read it from the
    token itself because PAT and project-tokens share the `glpat-`
    prefix and OAuth tokens have no fixed prefix.
    """
    if raw not in _LEGAL_MODES:
        raise InvalidGitlabAuthError(
            f"unsupported gitlab auth mode {raw!r}; "
            f"expected one of {sorted(_LEGAL_MODES)}"
        )
    return GitlabAuthMode(raw)


def header_for(mode: GitlabAuthMode, token: str) -> tuple[str, str]:
    """Return the (header_name, header_value) pair for the given auth mode.

    Centralised so the API client never has to branch on auth mode at
    request time. Token value is interpolated into the header value
    here and never stored anywhere outside the httpx request envelope.
    """
    if mode is GitlabAuthMode.OAUTH:
        return ("Authorization", f"Bearer {token}")
    # PAT and PROJECT both ride PRIVATE-TOKEN — the scope difference is
    # enforced server-side; from the wire's perspective they are identical.
    return ("PRIVATE-TOKEN", token)


__all__ = [
    "GitlabAuthMode",
    "InvalidGitlabAuthError",
    "header_for",
    "parse_auth_mode",
]
