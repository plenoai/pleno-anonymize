"""RBAC + Policy + TOML loader tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

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


def _make_enforcer(rules: list[PolicyRule]) -> RBACEnforcer:
    return RBACEnforcer(policy=Policy(rules=tuple(rules)))


def test_default_deny_when_no_rules() -> None:
    e = _make_enforcer([])
    d = e.evaluate(
        Subject(id="alice", kind="user"),
        Action.SCAN_SUBMIT,
        "github",
        "github:plenoai/pii",
    )
    assert d.effect == "deny"
    assert d.matched_rule is None
    assert d.allowed is False
    assert "default-deny" in d.reason


def test_explicit_user_allow() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="user:alice",
                source_kind_pattern="*",
                source_id_pattern="*",
                action=Action.SCAN_SUBMIT.value,
            )
        ]
    )
    d = e.evaluate(Subject(id="alice", kind="user"), Action.SCAN_SUBMIT, "github", "x")
    assert d.allowed is True
    assert d.matched_rule is not None


def test_team_membership_allow() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="team:security",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            )
        ]
    )
    s = Subject(id="bob", kind="user", teams=("security", "backend"))
    d = e.evaluate(s, Action.FINDING_REVEAL_VALUE, "aws", "aws:prod-1")
    assert d.allowed is True


def test_wildcard_subject_matches_anyone() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="*",
                source_kind_pattern="github",
                source_id_pattern="*",
                action=Action.FINDING_READ.value,
            )
        ]
    )
    d = e.evaluate(
        Subject(id="svc", kind="service_account"),
        Action.FINDING_READ,
        "github",
        "github:plenoai/x",
    )
    assert d.allowed is True


def test_kind_mismatch_does_not_match() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="service_account:scanner",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            )
        ]
    )
    # alice is user, not service_account -> rule must not match.
    d = e.evaluate(Subject(id="scanner", kind="user"), Action.SCAN_SUBMIT, "x", "y")
    assert d.allowed is False


def test_malformed_subject_pattern_no_colon_does_not_match() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="alice",  # missing prefix
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            )
        ]
    )
    d = e.evaluate(Subject(id="alice", kind="user"), Action.SCAN_SUBMIT, "x", "y")
    assert d.allowed is False


def test_deny_overrides_allow_short_circuit() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="user:alice",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            ),
            PolicyRule(
                subject_pattern="user:alice",
                source_kind_pattern="github",
                source_id_pattern="github:plenoai/secrets-*",
                action="*",
                effect="deny",
            ),
        ]
    )
    d = e.evaluate(
        Subject(id="alice", kind="user"),
        Action.SCAN_SUBMIT,
        "github",
        "github:plenoai/secrets-prod",
    )
    assert d.effect == "deny"
    assert d.matched_rule is not None
    assert d.matched_rule.effect == "deny"


def test_deny_first_then_allow_still_denies() -> None:
    # Order independence: deny appearing before allow still wins.
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="user:contractor-*",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
                effect="deny",
            ),
            PolicyRule(
                subject_pattern="*",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            ),
        ]
    )
    d = e.evaluate(
        Subject(id="contractor-1", kind="user"),
        Action.SCAN_SUBMIT,
        "github",
        "github:plenoai/x",
    )
    assert d.effect == "deny"


def test_action_specific_rule_skips_other_actions() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="user:alice",
                source_kind_pattern="*",
                source_id_pattern="*",
                action=Action.SCAN_SUBMIT.value,
            )
        ]
    )
    d = e.evaluate(Subject(id="alice", kind="user"), Action.SCHEDULE_CREATE, "x", "y")
    assert d.allowed is False


def test_source_kind_glob() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="*",
                source_kind_pattern="aws-*",
                source_id_pattern="*",
                action="*",
            )
        ]
    )
    d_match = e.evaluate(
        Subject(id="x", kind="user"), Action.SCAN_FETCH, "aws-s3", "aws-s3:bucket"
    )
    d_no = e.evaluate(
        Subject(id="x", kind="user"), Action.SCAN_FETCH, "github", "github:o/r"
    )
    assert d_match.allowed is True
    assert d_no.allowed is False


def test_source_id_glob_specific_path() -> None:
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="*",
                source_kind_pattern="github",
                source_id_pattern="github:plenoai/*",
                action="*",
            )
        ]
    )
    d_in = e.evaluate(
        Subject(id="x", kind="user"), Action.SCAN_SUBMIT, "github", "github:plenoai/a"
    )
    d_out = e.evaluate(
        Subject(id="x", kind="user"), Action.SCAN_SUBMIT, "github", "github:other/a"
    )
    assert d_in.allowed is True
    assert d_out.allowed is False


def test_team_glob() -> None:
    # team:platform-* matches platform-prod and platform-stg
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="team:platform-*",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            )
        ]
    )
    s = Subject(id="alice", kind="user", teams=("platform-prod",))
    assert e.evaluate(s, Action.SCAN_SUBMIT, "x", "y").allowed is True
    s2 = Subject(id="bob", kind="user", teams=("data",))
    assert e.evaluate(s2, Action.SCAN_SUBMIT, "x", "y").allowed is False


def test_decision_dataclass_default_reason_empty() -> None:
    d = Decision(effect="allow")
    assert d.reason == ""
    assert d.matched_rule is None


def test_load_policy_minimal(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            subject = "team:security"
            source_kind = "*"
            source_id = "*"
            action = "scan:submit"
            effect = "allow"

            [[rule]]
            subject = "user:contractor-*"
            source_kind = "*"
            source_id = "github:plenoai/secrets-*"
            action = "*"
            effect = "deny"
            """
        ).strip()
    )
    pol = load_policy_from_toml(p)
    assert len(pol.rules) == 2
    assert pol.rules[0].effect == "allow"
    assert pol.rules[1].effect == "deny"


def test_load_policy_default_effect_is_allow(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            subject = "*"
            source_kind = "*"
            source_id = "*"
            action = "scan:fetch"
            """
        ).strip()
    )
    pol = load_policy_from_toml(p)
    assert pol.rules[0].effect == "allow"


def test_load_policy_unknown_top_level(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [unexpected]
            x = 1

            [[rule]]
            subject = "*"
            source_kind = "*"
            source_id = "*"
            action = "*"
            """
        ).strip()
    )
    with pytest.raises(PolicyLoadError, match="unknown top-level"):
        load_policy_from_toml(p)


def test_load_policy_unknown_rule_key(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            subject = "*"
            source_kind = "*"
            source_id = "*"
            action = "*"
            extra_typo = "oops"
            """
        ).strip()
    )
    with pytest.raises(PolicyLoadError, match="unknown keys"):
        load_policy_from_toml(p)


def test_load_policy_missing_required(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            subject = "*"
            source_kind = "*"
            action = "*"
            """
        ).strip()
    )
    with pytest.raises(PolicyLoadError, match="missing required"):
        load_policy_from_toml(p)


def test_load_policy_wrong_type(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            subject = 1
            source_kind = "*"
            source_id = "*"
            action = "*"
            """
        ).strip()
    )
    with pytest.raises(PolicyLoadError, match="must be a string"):
        load_policy_from_toml(p)


def test_load_policy_invalid_action(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            subject = "*"
            source_kind = "*"
            source_id = "*"
            action = "scan:bogus"
            """
        ).strip()
    )
    with pytest.raises(PolicyLoadError, match="not a known Action"):
        load_policy_from_toml(p)


def test_load_policy_invalid_effect(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            subject = "*"
            source_kind = "*"
            source_id = "*"
            action = "*"
            effect = "alllow"
            """
        ).strip()
    )
    with pytest.raises(PolicyLoadError, match="must be 'allow' or 'deny'"):
        load_policy_from_toml(p)


def test_load_policy_rule_not_array(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text("rule = 1\n")
    with pytest.raises(PolicyLoadError, match="array of tables"):
        load_policy_from_toml(p)


def test_load_policy_rule_entry_not_table(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    # An array of strings - rules array exists but elements are not tables.
    p.write_text('rule = ["bad"]\n')
    with pytest.raises(PolicyLoadError, match="must be a table"):
        load_policy_from_toml(p)


def test_load_policy_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(PolicyLoadError, match="not found"):
        load_policy_from_toml(tmp_path / "missing.toml")


def test_load_policy_invalid_toml(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text("not = = valid")
    with pytest.raises(PolicyLoadError, match="invalid TOML"):
        load_policy_from_toml(p)


def test_load_policy_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "policy.toml"
    p.write_text("")
    pol = load_policy_from_toml(p)
    assert pol.rules == ()


def test_multiple_allow_matches_first_match_wins() -> None:
    # Hits the "allow_match is not None, keep scanning for deny" branch.
    e = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="*",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            ),
            PolicyRule(
                subject_pattern="user:alice",
                source_kind_pattern="github",
                source_id_pattern="*",
                action=Action.SCAN_SUBMIT.value,
            ),
        ]
    )
    d = e.evaluate(
        Subject(id="alice", kind="user"),
        Action.SCAN_SUBMIT,
        "github",
        "github:o/r",
    )
    assert d.allowed is True
    # First matching allow rule is the wildcard one above.
    assert d.matched_rule is not None
    assert d.matched_rule.subject_pattern == "*"


def test_two_phase_use_case_submit_then_fetch_revoked() -> None:
    # Simulate the production scenario: scheduler enforces at submit
    # (allow) and again at fetch (now denied due to policy edit).
    initial = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="user:alice",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
            )
        ]
    )
    revoked = _make_enforcer(
        [
            PolicyRule(
                subject_pattern="user:alice",
                source_kind_pattern="*",
                source_id_pattern="*",
                action="*",
                effect="deny",
            ),
        ]
    )
    s = Subject(id="alice", kind="user")
    assert initial.evaluate(s, Action.SCAN_SUBMIT, "github", "x").allowed is True
    assert revoked.evaluate(s, Action.SCAN_FETCH, "github", "x").allowed is False
