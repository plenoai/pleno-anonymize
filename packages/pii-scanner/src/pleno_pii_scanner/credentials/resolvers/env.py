"""Environment-variable credential resolver.

Honors the ``PLENO_<KIND>_<NAME>_<FIELD>`` convention used by every
deploy environment that injects secrets via env (k8s Secret refs,
GitHub Actions secrets, Heroku config vars). Names are normalized to
upper-snake-case; hyphens in `kind` and `name` become underscores so
``aws-iam`` / ``my-org`` map to ``PLENO_AWS_IAM_...`` /
``PLENO_..._MY_ORG_...`` without surprise.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pleno_pii_scanner.credentials.broker import (
    Credential,
    CredentialMisconfiguredError,
)

# Per-kind required field set. Keys are the canonical credential kind
# strings; values are the fields that must be present in env for the
# resolver to consider the credential complete. Anything missing makes
# the resolver return None (so a partial match falls through to the
# next resolver instead of being treated as a real credential).
_KIND_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "github-pat": ("token",),
    "github-app": ("app_id", "installation_id", "private_key"),
    "gitlab-pat": ("token",),
    "bitbucket-app-password": ("username", "app_password"),
    "aws-iam": ("access_key_id", "secret_access_key"),
    "aws-oidc": ("role_arn",),
    "gcp-sa-key": ("client_email", "private_key"),
    "azure-sp": ("tenant_id", "client_id", "client_secret"),
    "slack-bot": ("token",),
    "slack-user": ("token",),
}

# Optional fields per kind. Picked up from env when present but not
# required. Keeps the env interface tolerant of additional metadata
# (region, session_token for STS-vended IAM creds, etc.).
_KIND_OPTIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "aws-iam": ("session_token", "region"),
    "aws-oidc": ("region", "external_id", "session_name", "duration_seconds"),
    "github-app": ("base_url",),
    "gitlab-pat": ("base_url",),
    "slack-bot": ("workspace",),
    "slack-user": ("workspace",),
}


def _normalize(component: str) -> str:
    return component.upper().replace("-", "_")


def _env_prefix(kind: str, name: str) -> str:
    if name == "default":
        # Default name compresses to PLENO_<KIND>_<FIELD>, matching the
        # legacy single-tenant convention so existing deployments keep
        # working without renaming env vars.
        return f"PLENO_{_normalize(kind)}"
    return f"PLENO_{_normalize(kind)}_{_normalize(name)}"


class EnvCredentialResolver:
    """Process-environment-backed CredentialResolver."""

    name = "env"

    def __init__(self, *, priority: int = 80) -> None:
        self.priority = priority

    async def resolve(self, kind: str, name: str) -> Credential | None:
        required = _KIND_REQUIRED_FIELDS.get(kind)
        if required is None:
            return None
        prefix = _env_prefix(kind, name)
        env: Mapping[str, str] = os.environ
        payload: dict[str, object] = {}
        present: list[str] = []
        for field_name in required:
            value = env.get(f"{prefix}_{field_name.upper()}")
            if value is None and name == "default" and field_name == "token":
                # Backwards compatibility: the original single-tenant
                # convention exposed PLENO_GITHUB_TOKEN with no _NAME_
                # segment. Fall back to that exact form before declaring
                # the credential incomplete.
                value = env.get(f"PLENO_{_normalize(kind)}_TOKEN")
            if value is None:
                return None
            if value == "":
                # Empty string is almost always operator error (k8s Secret
                # mounted with the wrong key). Surface loudly rather than
                # accept an empty token that would later fail at the API.
                raise CredentialMisconfiguredError(
                    f"env {prefix}_{field_name.upper()} is empty"
                )
            payload[field_name] = value
            present.append(f"{prefix}_{field_name.upper()}")
        for field_name in _KIND_OPTIONAL_FIELDS.get(kind, ()):
            value = env.get(f"{prefix}_{field_name.upper()}")
            if value:
                payload[field_name] = value
        return Credential(
            kind=kind,
            payload=payload,
            source=f"env:{','.join(present)}",
        )
