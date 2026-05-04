"""Credential broker and resolver framework.

The Scheduler (#7) and every SourceConnector ask CredentialBroker for a
Credential by `(kind, name)` or by CredentialProfile, and never read
env / TOML / keyring directly. This isolates credential acquisition
policy (priority chain, rotation, assume-role hops) from the data plane
so plugins can swap Vault / 1Password / SecretsManager in without
forking connectors.

See ADR-0007 §3.
"""

from pleno_pii_scanner.credentials.broker import (
    Credential,
    CredentialBroker,
    CredentialError,
    CredentialMisconfiguredError,
    CredentialNotFoundError,
    CredentialResolver,
    _is_secret_key,
)
from pleno_pii_scanner.credentials.profile import (
    AssumeRoleHop,
    CredentialProfile,
    apply_chain,
    register_hop_plugin,
    registered_hop_providers,
    unregister_hop_plugin,
)
from pleno_pii_scanner.credentials.resolvers import (
    EnvCredentialResolver,
    FileCredentialResolver,
    KeyringCredentialResolver,
)

__all__ = [
    "AssumeRoleHop",
    "Credential",
    "CredentialBroker",
    "CredentialError",
    "CredentialMisconfiguredError",
    "CredentialNotFoundError",
    "CredentialProfile",
    "CredentialResolver",
    "EnvCredentialResolver",
    "FileCredentialResolver",
    "KeyringCredentialResolver",
    "apply_chain",
    "register_hop_plugin",
    "registered_hop_providers",
    "unregister_hop_plugin",
]
# Re-exported for tests that exercise the masking helper. Underscore-
# prefixed names are not in __all__; they are implementation details
# but stable enough to test directly.
_ = _is_secret_key
