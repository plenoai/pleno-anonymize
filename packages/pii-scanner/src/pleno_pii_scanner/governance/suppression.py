"""Hierarchical Finding suppression engine (ADR-0007 §10).

Three layers — org → team → repo — are evaluated in that order. The
**lowest** layer (repo) is the most specific and overrides the higher
layers (org-wide blanket deny + repo-local allow → finding is kept).
This matches how operators reason about exceptions: a security-team
broad-stroke must be locally overridable by the team that owns the
codebase, otherwise legitimate findings get permanently buried.

Each layer is a `SuppressionPolicy` (a TOML file or in-memory list).
Rules carry an optional `expires_at`; expired rules are silently
ignored so a TTL-based exception cannot rot indefinitely.

The legacy `IgnoreSet` (.plenoignore baseline) is wrapped as a special
repo-layer policy via `IgnoreSetPolicy`, so existing
.plenoignore + baseline.json continue to work unchanged.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import pathspec

from pleno_pii_scanner.ignore import IgnoreSet
from pleno_pii_scanner.models import Finding

SuppressionScope = Literal["org", "team", "repo"]
SuppressionEffect = Literal["suppress", "allow"]


@dataclass(frozen=True, slots=True)
class SuppressionRule:
    """One suppression entry.

    A rule matches a Finding when EVERY non-None criterion matches:
    `entity` (exact), `path_glob` (gitignore syntax), `fingerprint`
    (exact). All-None criteria match nothing — that is a misconfigured
    rule and we refuse it at construction.

    `effect="allow"` is the override knob: a repo-layer allow rule
    cancels matching org / team suppress rules. `expires_at` past `now`
    drops the rule from evaluation.
    """

    scope: SuppressionScope
    entity: str | None = None
    path_glob: str | None = None
    fingerprint: str | None = None
    reason: str = ""
    expires_at: datetime | None = None
    effect: SuppressionEffect = "suppress"

    def __post_init__(self) -> None:
        if self.entity is None and self.path_glob is None and self.fingerprint is None:
            raise ValueError(
                "SuppressionRule needs at least one of entity / path_glob / fingerprint"
            )

    def is_active(self, now: datetime) -> bool:
        return self.expires_at is None or self.expires_at > now

    def matches(self, finding: Finding) -> bool:
        if self.fingerprint is not None and finding.fingerprint() != self.fingerprint:
            return False
        if self.entity is not None and finding.entity != self.entity:
            return False
        if self.path_glob is not None:
            spec = pathspec.PathSpec.from_lines("gitignore", [self.path_glob])
            if not spec.match_file(finding.file):
                return False
        return True


@dataclass(frozen=True, slots=True)
class SuppressionPolicy:
    """A named layer of rules. `name` is logged so audits can attribute
    a suppression decision to a source file ("org/global.toml")."""

    scope: SuppressionScope
    name: str
    rules: tuple[SuppressionRule, ...] = field(default_factory=tuple)


# Layer precedence: later list index = stronger override authority.
# `repo` last so a repo-local allow can cancel an org suppress.
_SCOPE_PRECEDENCE: dict[SuppressionScope, int] = {"org": 0, "team": 1, "repo": 2}


class SuppressionEngine:
    """Evaluates a Finding against a stack of layered policies.

    Algorithm: from highest-precedence layer (repo) downward, find the
    first matching rule. If `effect="allow"`, the finding is kept (early
    return). If `effect="suppress"`, the finding is suppressed (early
    return). Layers below the first hit are not consulted, which is what
    makes a repo override authoritative.
    """

    def __init__(self, layers: list[SuppressionPolicy]) -> None:
        # Sort once at construction; a stable sort preserves caller
        # order within a scope so org-vs-org tiebreaks stay predictable.
        self._layers = sorted(
            layers, key=lambda p: _SCOPE_PRECEDENCE[p.scope], reverse=True
        )

    @property
    def layers(self) -> tuple[SuppressionPolicy, ...]:
        return tuple(self._layers)

    def is_suppressed(
        self,
        finding: Finding,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, SuppressionRule | None]:
        """Return (suppressed, rule_that_decided)."""
        clock = now or datetime.now(tz=_default_tz())
        for layer in self._layers:
            for rule in layer.rules:
                if not rule.is_active(clock):
                    continue
                if not rule.matches(finding):
                    continue
                return rule.effect == "suppress", rule
        return False, None


def _default_tz():  # pragma: no cover - pure plumbing
    from datetime import timezone

    return timezone.utc


def ignore_set_to_policy(name: str, ignore_set: IgnoreSet) -> SuppressionPolicy:
    """Adapt a legacy `IgnoreSet` (.plenoignore + baseline) into a
    repo-layer `SuppressionPolicy`.

    The legacy `IgnoreSet` predates the layered engine and is still
    used by the BYOR baseline path. We re-expose it here so existing
    repos with `.plenoignore` continue to work without rewriting the
    file. Each entity / fingerprint becomes its own SuppressionRule;
    the gitignore-compiled `PathSpec` is wrapped in a single
    delegating rule (see `_PathSpecRule`).
    """
    rules: list[SuppressionRule] = []
    for entity in sorted(ignore_set.entities):
        rules.append(
            SuppressionRule(scope="repo", entity=entity, reason=".plenoignore entity")
        )
    for fp in sorted(ignore_set.fingerprints):
        rules.append(
            SuppressionRule(
                scope="repo", fingerprint=fp, reason=".plenoignore fingerprint"
            )
        )
    if ignore_set.path_spec is not None:
        rules.append(_PathSpecRule(ignore_set.path_spec))
    return SuppressionPolicy(scope="repo", name=name, rules=tuple(rules))


class _PathSpecRule(SuppressionRule):
    """Delegating rule for an `IgnoreSet.path_spec`.

    The parent `SuppressionRule.__post_init__` rejects all-None
    criteria; we set `path_glob` to a sentinel and override `matches`
    to consult the prebuilt PathSpec stored in a class-level registry
    keyed by `id(self)`. Cleanup is handled in `__del__` so the
    registry does not leak across long-running processes.
    """

    __slots__ = ()
    _spec_registry: dict[int, pathspec.PathSpec] = {}

    def __init__(self, path_spec: pathspec.PathSpec) -> None:
        super().__init__(
            scope="repo",
            path_glob="<from-ignoreset>",
            reason=".plenoignore path",
        )
        _PathSpecRule._spec_registry[id(self)] = path_spec

    def __del__(self) -> None:  # pragma: no cover - GC timing
        _PathSpecRule._spec_registry.pop(id(self), None)

    def matches(self, finding: Finding) -> bool:
        spec = _PathSpecRule._spec_registry[id(self)]
        return spec.match_file(finding.file)


class SuppressionLoadError(ValueError):
    """Raised when a suppression TOML file fails strict schema validation."""


_RULE_KEYS_REQUIRED: frozenset[str] = frozenset({"scope"})
_RULE_KEYS_OPTIONAL: frozenset[str] = frozenset(
    {"entity", "path_glob", "fingerprint", "reason", "expires_at", "effect"}
)
_RULE_KEYS_ALLOWED: frozenset[str] = _RULE_KEYS_REQUIRED | _RULE_KEYS_OPTIONAL


def load_suppression_policy_from_toml(
    path: Path,
    *,
    expected_scope: SuppressionScope,
    name: str | None = None,
) -> SuppressionPolicy:
    """Parse a suppression-policy TOML file with strict schema.

    Each `[[rule]]` table must declare `scope` (matching `expected_scope`
    so a misfiled org rule cannot accidentally land in repo layer).
    Unknown keys raise.
    """
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError as e:
        raise SuppressionLoadError(f"suppression file not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise SuppressionLoadError(f"invalid TOML in {path}: {e}") from e

    extra = set(raw.keys()) - {"rule"}
    if extra:
        raise SuppressionLoadError(f"unknown top-level keys: {sorted(extra)}")
    rules_raw = raw.get("rule", [])
    if not isinstance(rules_raw, list):
        raise SuppressionLoadError("`rule` must be an array of tables")
    rules: list[SuppressionRule] = []
    for idx, rd in enumerate(rules_raw):
        if not isinstance(rd, dict):
            raise SuppressionLoadError(f"rule[{idx}] must be a table")
        keys = set(rd.keys())
        unknown = keys - _RULE_KEYS_ALLOWED
        if unknown:
            raise SuppressionLoadError(f"rule[{idx}] unknown keys: {sorted(unknown)}")
        scope = rd.get("scope")
        if scope != expected_scope:
            raise SuppressionLoadError(
                f"rule[{idx}].scope {scope!r} does not match expected {expected_scope!r}"
            )
        expires_raw = rd.get("expires_at")
        expires_at: datetime | None = None
        if expires_raw is not None:
            if isinstance(expires_raw, datetime):
                expires_at = expires_raw
            else:
                raise SuppressionLoadError(
                    f"rule[{idx}].expires_at must be a TOML datetime"
                )
        effect = rd.get("effect", "suppress")
        if effect not in ("suppress", "allow"):
            raise SuppressionLoadError(
                f"rule[{idx}].effect must be 'suppress' or 'allow', got {effect!r}"
            )
        try:
            rule = SuppressionRule(
                scope=scope,
                entity=rd.get("entity"),
                path_glob=rd.get("path_glob"),
                fingerprint=rd.get("fingerprint"),
                reason=rd.get("reason", ""),
                expires_at=expires_at,
                effect=effect,
            )
        except ValueError as e:
            raise SuppressionLoadError(f"rule[{idx}]: {e}") from e
        rules.append(rule)
    return SuppressionPolicy(
        scope=expected_scope, name=name or str(path), rules=tuple(rules)
    )
