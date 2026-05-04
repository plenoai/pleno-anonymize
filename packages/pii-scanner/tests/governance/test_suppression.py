"""SuppressionEngine + hierarchy + TTL + IgnoreSet adapter tests."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pleno_pii_scanner.governance.suppression import (
    SuppressionEngine,
    SuppressionLoadError,
    SuppressionPolicy,
    SuppressionRule,
    ignore_set_to_policy,
    load_suppression_policy_from_toml,
)
from pleno_pii_scanner.ignore import IgnoreSet
from pleno_pii_scanner.models import Finding


def _f(
    entity: str = "PHONE_NUMBER",
    file: str = "src/app.py",
    matched: str = "090-1234-5678",
) -> Finding:
    return Finding(
        entity=entity,
        file=file,
        line=10,
        col=4,
        score=0.9,
        snippet="contact = ...",
        matched=matched,
        pattern_name="phone",
    )


NOW = datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)


def test_rule_requires_at_least_one_criterion() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SuppressionRule(scope="org")


def test_rule_matches_by_entity() -> None:
    r = SuppressionRule(scope="org", entity="PHONE_NUMBER")
    assert r.matches(_f(entity="PHONE_NUMBER")) is True
    assert r.matches(_f(entity="EMAIL")) is False


def test_rule_matches_by_path_glob() -> None:
    r = SuppressionRule(scope="repo", path_glob="docs/samples/**")
    assert r.matches(_f(file="docs/samples/x.py")) is True
    assert r.matches(_f(file="src/app.py")) is False


def test_rule_matches_by_fingerprint() -> None:
    target = _f()
    fp = target.fingerprint()
    r = SuppressionRule(scope="repo", fingerprint=fp)
    assert r.matches(target) is True
    other = _f(matched="080-0000-0000")
    assert r.matches(other) is False


def test_rule_combined_criteria_must_all_match() -> None:
    r = SuppressionRule(
        scope="repo", entity="PHONE_NUMBER", path_glob="src/**"
    )
    assert r.matches(_f(entity="PHONE_NUMBER", file="src/app.py")) is True
    assert r.matches(_f(entity="EMAIL", file="src/app.py")) is False
    assert r.matches(_f(entity="PHONE_NUMBER", file="tests/x.py")) is False


def test_rule_ttl_expires() -> None:
    expired = SuppressionRule(
        scope="org",
        entity="PHONE_NUMBER",
        expires_at=NOW - timedelta(days=1),
    )
    active = SuppressionRule(
        scope="org",
        entity="PHONE_NUMBER",
        expires_at=NOW + timedelta(days=1),
    )
    assert expired.is_active(NOW) is False
    assert active.is_active(NOW) is True


def test_engine_returns_no_suppression_when_no_rules() -> None:
    e = SuppressionEngine([])
    suppressed, rule = e.is_suppressed(_f(), now=NOW)
    assert suppressed is False
    assert rule is None


def test_engine_simple_org_suppress() -> None:
    org = SuppressionPolicy(
        scope="org",
        name="org/global",
        rules=(SuppressionRule(scope="org", entity="PHONE_NUMBER"),),
    )
    e = SuppressionEngine([org])
    suppressed, rule = e.is_suppressed(_f(), now=NOW)
    assert suppressed is True
    assert rule is not None
    assert rule.entity == "PHONE_NUMBER"


def test_engine_repo_allow_overrides_org_suppress() -> None:
    org = SuppressionPolicy(
        scope="org",
        name="org/global",
        rules=(SuppressionRule(scope="org", entity="PHONE_NUMBER"),),
    )
    repo = SuppressionPolicy(
        scope="repo",
        name="repo/local",
        rules=(
            SuppressionRule(
                scope="repo",
                entity="PHONE_NUMBER",
                path_glob="src/**",
                effect="allow",
            ),
        ),
    )
    e = SuppressionEngine([org, repo])
    suppressed, rule = e.is_suppressed(_f(file="src/app.py"), now=NOW)
    assert suppressed is False
    assert rule is not None
    assert rule.effect == "allow"


def test_engine_layer_precedence_repo_over_team_over_org() -> None:
    # All three layers match, but repo wins.
    org = SuppressionPolicy(
        scope="org",
        name="o",
        rules=(SuppressionRule(scope="org", entity="PHONE_NUMBER", reason="org"),),
    )
    team = SuppressionPolicy(
        scope="team",
        name="t",
        rules=(SuppressionRule(scope="team", entity="PHONE_NUMBER", reason="team"),),
    )
    repo = SuppressionPolicy(
        scope="repo",
        name="r",
        rules=(SuppressionRule(scope="repo", entity="PHONE_NUMBER", reason="repo"),),
    )
    e = SuppressionEngine([org, team, repo])
    suppressed, rule = e.is_suppressed(_f(), now=NOW)
    assert suppressed is True
    assert rule is not None
    assert rule.reason == "repo"
    # Layer order in the engine is repo, team, org (highest precedence first).
    assert [layer.scope for layer in e.layers] == ["repo", "team", "org"]


def test_engine_ttl_expired_rule_ignored() -> None:
    org = SuppressionPolicy(
        scope="org",
        name="o",
        rules=(
            SuppressionRule(
                scope="org",
                entity="PHONE_NUMBER",
                expires_at=NOW - timedelta(seconds=1),
            ),
        ),
    )
    e = SuppressionEngine([org])
    suppressed, rule = e.is_suppressed(_f(), now=NOW)
    assert suppressed is False
    assert rule is None


def test_engine_default_now_uses_utc_clock() -> None:
    # Don't pin `now` - exercises the default-clock branch.
    org = SuppressionPolicy(
        scope="org",
        name="o",
        rules=(SuppressionRule(scope="org", entity="PHONE_NUMBER"),),
    )
    e = SuppressionEngine([org])
    suppressed, _ = e.is_suppressed(_f())
    assert suppressed is True


def test_engine_fingerprint_does_not_apply_to_other_findings() -> None:
    a = _f(matched="090-1111-2222")
    b = _f(matched="090-3333-4444")
    rule = SuppressionRule(scope="repo", fingerprint=a.fingerprint())
    e = SuppressionEngine(
        [SuppressionPolicy(scope="repo", name="r", rules=(rule,))]
    )
    sa, _ = e.is_suppressed(a, now=NOW)
    sb, _ = e.is_suppressed(b, now=NOW)
    assert sa is True
    assert sb is False


def test_load_suppression_minimal(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "org"
            entity = "PHONE_NUMBER"
            reason = "blanket org allowlist"
            """
        ).strip()
    )
    pol = load_suppression_policy_from_toml(p, expected_scope="org", name="org/g")
    assert pol.scope == "org"
    assert pol.name == "org/g"
    assert len(pol.rules) == 1
    assert pol.rules[0].entity == "PHONE_NUMBER"


def test_load_suppression_default_name_uses_path(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text("")
    pol = load_suppression_policy_from_toml(p, expected_scope="org")
    assert pol.name == str(p)


def test_load_suppression_with_expires(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "team"
            entity = "PHONE_NUMBER"
            expires_at = 2026-12-31T00:00:00Z
            """
        ).strip()
    )
    pol = load_suppression_policy_from_toml(p, expected_scope="team")
    assert pol.rules[0].expires_at is not None
    assert pol.rules[0].expires_at.year == 2026


def test_load_suppression_with_allow_effect(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "repo"
            path_glob = "tests/fixtures/**"
            effect = "allow"
            """
        ).strip()
    )
    pol = load_suppression_policy_from_toml(p, expected_scope="repo")
    assert pol.rules[0].effect == "allow"


def test_load_suppression_unknown_top_level(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text("[meta]\nx = 1\n")
    with pytest.raises(SuppressionLoadError, match="unknown top-level"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_load_suppression_unknown_rule_key(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "org"
            entity = "PHONE_NUMBER"
            typo_field = "x"
            """
        ).strip()
    )
    with pytest.raises(SuppressionLoadError, match="unknown keys"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_load_suppression_scope_mismatch(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "org"
            entity = "PHONE_NUMBER"
            """
        ).strip()
    )
    with pytest.raises(SuppressionLoadError, match="does not match expected"):
        load_suppression_policy_from_toml(p, expected_scope="repo")


def test_load_suppression_rule_array_wrong_type(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text("rule = 1\n")
    with pytest.raises(SuppressionLoadError, match="array of tables"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_load_suppression_rule_entry_not_table(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text('rule = ["bad"]\n')
    with pytest.raises(SuppressionLoadError, match="must be a table"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_load_suppression_expires_wrong_type(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "org"
            entity = "X"
            expires_at = "2026-01-01"
            """
        ).strip()
    )
    with pytest.raises(SuppressionLoadError, match="TOML datetime"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_load_suppression_invalid_effect(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "org"
            entity = "X"
            effect = "bogus"
            """
        ).strip()
    )
    with pytest.raises(SuppressionLoadError, match="must be 'suppress' or 'allow'"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_load_suppression_no_criteria_propagates(tmp_path: Path) -> None:
    # Underlying SuppressionRule.__post_init__ rejects all-None criteria;
    # load_* must wrap and re-raise as SuppressionLoadError.
    p = tmp_path / "sup.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            scope = "org"
            """
        ).strip()
    )
    with pytest.raises(SuppressionLoadError, match="at least one"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_load_suppression_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(SuppressionLoadError, match="not found"):
        load_suppression_policy_from_toml(
            tmp_path / "missing.toml", expected_scope="org"
        )


def test_load_suppression_invalid_toml(tmp_path: Path) -> None:
    p = tmp_path / "sup.toml"
    p.write_text("not = = valid")
    with pytest.raises(SuppressionLoadError, match="invalid TOML"):
        load_suppression_policy_from_toml(p, expected_scope="org")


def test_ignore_set_adapter_entity(tmp_path: Path) -> None:
    ignore_file = tmp_path / ".plenoignore"
    ignore_file.write_text("PHONE_NUMBER\n")
    iset = IgnoreSet.load(ignore_file)
    pol = ignore_set_to_policy("repo/.plenoignore", iset)
    assert pol.scope == "repo"
    assert any(r.entity == "PHONE_NUMBER" for r in pol.rules)
    e = SuppressionEngine([pol])
    suppressed, _ = e.is_suppressed(_f(), now=NOW)
    assert suppressed is True


def test_ignore_set_adapter_fingerprint(tmp_path: Path) -> None:
    target = _f()
    fp = target.fingerprint()
    ignore_file = tmp_path / ".plenoignore"
    ignore_file.write_text(f"finding:{fp}\n")
    iset = IgnoreSet.load(ignore_file)
    pol = ignore_set_to_policy("repo/.plenoignore", iset)
    e = SuppressionEngine([pol])
    suppressed, rule = e.is_suppressed(target, now=NOW)
    assert suppressed is True
    assert rule is not None
    assert rule.fingerprint == fp


def test_ignore_set_adapter_path_glob(tmp_path: Path) -> None:
    ignore_file = tmp_path / ".plenoignore"
    ignore_file.write_text("docs/samples/**\n")
    iset = IgnoreSet.load(ignore_file)
    pol = ignore_set_to_policy("repo/.plenoignore", iset)
    e = SuppressionEngine([pol])
    inside, _ = e.is_suppressed(_f(file="docs/samples/x.py"), now=NOW)
    outside, _ = e.is_suppressed(_f(file="src/app.py"), now=NOW)
    assert inside is True
    assert outside is False


def test_ignore_set_adapter_empty(tmp_path: Path) -> None:
    iset = IgnoreSet.load(tmp_path / "missing")
    pol = ignore_set_to_policy("repo/.plenoignore", iset)
    assert pol.rules == ()
