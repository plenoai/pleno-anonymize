"""RBAC policy engine for ADR-0007 §10.

Provides Subject + Action enum + Policy + RBACEnforcer. Policy is loaded
from `policy.toml` and evaluated at scan-submit time AND at connector
fetch time (two-phase check covers credential rotation / policy edits
that happen after submit but before fetch). Decisions are fail-safe:
any matching deny rule wins, even when allow rules also match.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal


class Action(StrEnum):
    """RBAC-protected operations. Wildcard "*" in policy matches any."""

    SCAN_SUBMIT = "scan:submit"
    SCAN_FETCH = "scan:fetch"
    FINDING_READ = "finding:read"
    FINDING_REVEAL_VALUE = "finding:reveal_value"
    FINDING_SUPPRESS = "finding:suppress"
    SCHEDULE_CREATE = "schedule:create"


# Set of valid action strings used by TOML schema validation. Wildcard is
# allowed inside policy rules but is NOT an Action enum value, so we keep
# the two sets separate.
_VALID_ACTION_VALUES: frozenset[str] = frozenset(a.value for a in Action)


SubjectKind = Literal["user", "team", "service_account"]
Effect = Literal["allow", "deny"]


@dataclass(frozen=True, slots=True)
class Subject:
    """Identity attempting an Action.

    `teams` is the transitive team membership at evaluation time. The
    enforcer matches a `team:<name>` rule subject against this set so a
    user gains team-granted permissions without per-user rules.
    """

    id: str
    kind: SubjectKind
    teams: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """Single allow/deny rule. Patterns use fnmatch (Unix glob)."""

    subject_pattern: str
    source_kind_pattern: str
    source_id_pattern: str
    action: str
    effect: Effect = "allow"

    def matches(
        self,
        subject: Subject,
        action: Action,
        source_kind: str,
        source_id: str,
    ) -> bool:
        if not _match_action(self.action, action):
            return False
        if not fnmatch.fnmatchcase(source_kind, self.source_kind_pattern):
            return False
        if not fnmatch.fnmatchcase(source_id, self.source_id_pattern):
            return False
        return _match_subject(self.subject_pattern, subject)


def _match_action(pattern: str, action: Action) -> bool:
    # "*" wildcards every action; otherwise compare verbatim. The pattern
    # is already validated to be either "*" or an Action.value at load.
    return pattern == "*" or pattern == action.value


def _match_subject(pattern: str, subject: Subject) -> bool:
    """Return True when `pattern` covers `subject`.

    Pattern shape is `<prefix>:<glob>` where prefix is one of
    `user|team|service_account` and glob is fnmatch. Bare "*" wildcards
    every subject regardless of kind. A `team:` pattern matches when the
    glob hits any of `subject.teams`, because team rules grant on the
    "is member of" axis.
    """
    if pattern == "*":
        return True
    if ":" not in pattern:
        return False
    prefix, glob = pattern.split(":", 1)
    if prefix == "team":
        return any(fnmatch.fnmatchcase(t, glob) for t in subject.teams)
    if prefix == subject.kind:
        return fnmatch.fnmatchcase(subject.id, glob)
    return False


@dataclass(frozen=True, slots=True)
class Decision:
    """Outcome of a single RBAC evaluation.

    `matched_rule` is the rule that produced the verdict; for the
    "no rule matched" default-deny case, `matched_rule` is None.
    """

    effect: Effect
    matched_rule: PolicyRule | None = None
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"


@dataclass(frozen=True, slots=True)
class Policy:
    """Ordered rule set evaluated by RBACEnforcer."""

    rules: tuple[PolicyRule, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RBACEnforcer:
    """Evaluates a Policy against (subject, action, source) tuples.

    Default-deny: with zero matching rules the verdict is deny. When at
    least one allow matches, the subject is granted UNLESS a deny rule
    also matches, in which case deny wins (fail-safe). This is the
    standard "explicit deny overrides allow" semantics expected by every
    enterprise auditor.
    """

    policy: Policy

    def evaluate(
        self,
        subject: Subject,
        action: Action,
        source_kind: str,
        source_id: str,
    ) -> Decision:
        allow_match: PolicyRule | None = None
        for rule in self.policy.rules:
            if not rule.matches(subject, action, source_kind, source_id):
                continue
            if rule.effect == "deny":
                # Short-circuit on first deny: every rule below is moot
                # because deny dominates regardless of allow count.
                return Decision(
                    effect="deny",
                    matched_rule=rule,
                    reason="explicit deny overrides any allow",
                )
            if allow_match is None:
                allow_match = rule
        if allow_match is not None:
            return Decision(effect="allow", matched_rule=allow_match)
        return Decision(effect="deny", reason="no matching rule (default-deny)")


class PolicyLoadError(ValueError):
    """Raised when policy.toml fails strict schema validation."""


_RULE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"subject", "source_kind", "source_id", "action"}
)
_RULE_OPTIONAL_KEYS: frozenset[str] = frozenset({"effect"})
_RULE_ALLOWED_KEYS: frozenset[str] = _RULE_REQUIRED_KEYS | _RULE_OPTIONAL_KEYS


def load_policy_from_toml(path: Path) -> Policy:
    """Parse `policy.toml` into a Policy with strict schema validation.

    Unknown top-level keys, unknown rule keys, missing required keys,
    wrong types, or unknown action strings raise PolicyLoadError. We
    refuse to silently drop unrecognized rule fields because a typo in
    `effect = "alllow"` would otherwise default to allow and quietly
    grant unintended access.
    """
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as e:
        raise PolicyLoadError(f"policy file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise PolicyLoadError(f"invalid TOML in {path}: {e}") from e

    extra = set(raw.keys()) - {"rule"}
    if extra:
        raise PolicyLoadError(f"unknown top-level keys: {sorted(extra)}")
    rules_raw = raw.get("rule", [])
    if not isinstance(rules_raw, list):
        raise PolicyLoadError("`rule` must be an array of tables")
    rules: list[PolicyRule] = []
    for idx, rd in enumerate(rules_raw):
        if not isinstance(rd, dict):
            raise PolicyLoadError(f"rule[{idx}] must be a table")
        keys = set(rd.keys())
        unknown = keys - _RULE_ALLOWED_KEYS
        if unknown:
            raise PolicyLoadError(f"rule[{idx}] unknown keys: {sorted(unknown)}")
        missing = _RULE_REQUIRED_KEYS - keys
        if missing:
            raise PolicyLoadError(
                f"rule[{idx}] missing required keys: {sorted(missing)}"
            )
        for k in ("subject", "source_kind", "source_id", "action"):
            if not isinstance(rd[k], str):
                raise PolicyLoadError(f"rule[{idx}].{k} must be a string")
        action = rd["action"]
        if action != "*" and action not in _VALID_ACTION_VALUES:
            raise PolicyLoadError(
                f"rule[{idx}].action {action!r} is not a known Action; "
                f"valid: {sorted(_VALID_ACTION_VALUES)} or '*'"
            )
        effect = rd.get("effect", "allow")
        if effect not in ("allow", "deny"):
            raise PolicyLoadError(
                f"rule[{idx}].effect must be 'allow' or 'deny', got {effect!r}"
            )
        rules.append(
            PolicyRule(
                subject_pattern=rd["subject"],
                source_kind_pattern=rd["source_kind"],
                source_id_pattern=rd["source_id"],
                action=action,
                effect=effect,
            )
        )
    return Policy(rules=tuple(rules))
