"""Bundled CredentialResolver implementations.

Three layers ship with the core wheel: file (TOML at
``~/.config/pleno/credentials.toml``), env (``PLENO_<KIND>_<NAME>_*``),
and OS keyring (``keyring`` library, optional). Cloud instance
identity (IMDSv2 / IRSA / MSI) and OIDC federation live in the
per-cloud plugin wheels and register additional resolvers via
``CredentialBroker.register_resolver``.

See ADR-0007 §3.
"""

from pleno_pii_scanner.credentials.resolvers.env import EnvCredentialResolver
from pleno_pii_scanner.credentials.resolvers.file import (
    FileCredentialResolver,
    default_credentials_path,
)
from pleno_pii_scanner.credentials.resolvers.keyring import (
    SERVICE_NAME,
    KeyringCredentialResolver,
)

__all__ = [
    "EnvCredentialResolver",
    "FileCredentialResolver",
    "KeyringCredentialResolver",
    "SERVICE_NAME",
    "default_credentials_path",
]
