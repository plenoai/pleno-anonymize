"""Enterprise governance: RBAC + AuditLog + SuppressionEngine.

See ADR-0007 §10. The Scheduler (#7) imports `RBACEnforcer.evaluate` as
both a submit-time and a fetch-time callback so policy changes that
land mid-scan still gate per-document fetches. AuditLogger Protocol is
fed by every governance decision (allow + deny) so SIEMs receive a
complete trail. SuppressionEngine wraps the legacy `IgnoreSet` for
backward compatibility with existing `.plenoignore` files.
"""

from pleno_pii_scanner.governance.audit import (
    AuditEvent,
    AuditLogger,
    NdjsonHmacAuditLogger,
    OtlpAuditLogger,
    verify_chain,
)
from pleno_pii_scanner.governance.rbac import (
    Action,
    Decision,
    Policy,
    PolicyLoadError,
    PolicyRule,
    RBACEnforcer,
    Subject,
    load_policy_from_toml,
)
from pleno_pii_scanner.governance.suppression import (
    SuppressionEngine,
    SuppressionLoadError,
    SuppressionPolicy,
    SuppressionRule,
    ignore_set_to_policy,
    load_suppression_policy_from_toml,
)

__all__ = [
    "Action",
    "AuditEvent",
    "AuditLogger",
    "Decision",
    "NdjsonHmacAuditLogger",
    "OtlpAuditLogger",
    "Policy",
    "PolicyLoadError",
    "PolicyRule",
    "RBACEnforcer",
    "Subject",
    "SuppressionEngine",
    "SuppressionLoadError",
    "SuppressionPolicy",
    "SuppressionRule",
    "ignore_set_to_policy",
    "load_policy_from_toml",
    "load_suppression_policy_from_toml",
    "verify_chain",
]
