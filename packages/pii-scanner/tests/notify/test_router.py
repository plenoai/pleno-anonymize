"""Tests for the Router and RoutingRule.

Stub notifiers record received batches so we can assert fan-out and
sub-batch composition without touching httpx.
"""

from __future__ import annotations

import pytest

from pleno_pii_scanner.notify.base import (
    NotificationBatch,
    NotificationResult,
)
from pleno_pii_scanner.notify.router import Router, RoutingRule
from ._helpers import make_batch, make_finding


class RecordingNotifier:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.batches: list[NotificationBatch] = []
        self.closed = False
        self._fail = fail

    async def send(self, batch: NotificationBatch) -> NotificationResult:
        self.batches.append(batch)
        if self._fail:
            raise RuntimeError(f"{self.name} simulated failure")
        return NotificationResult(
            transport=self.name,
            delivered=True,
            delivered_count=len(batch.findings),
        )

    async def close(self) -> None:
        self.closed = True


# ---------------- RoutingRule.matches ----------------


def test_rule_severity_filter():
    rule = RoutingRule(name="r", severity_in=frozenset({"critical"}))
    crit = make_finding(verification="passed")
    low = make_finding(score=0.1)
    assert rule.matches(crit, None) is True
    assert rule.matches(low, None) is False


def test_rule_verification_filter():
    rule = RoutingRule(name="r", verification_in=frozenset({"passed"}))
    assert rule.matches(make_finding(verification="passed"), None) is True
    assert rule.matches(make_finding(verification="unverified"), None) is False


def test_rule_entity_pattern_filter():
    rule = RoutingRule(name="r", entity_pattern="AWS_*")
    assert rule.matches(make_finding(entity="AWS_KEY"), None) is True
    assert rule.matches(make_finding(entity="EMAIL"), None) is False


def test_rule_source_kind_filter_requires_metadata():
    rule = RoutingRule(name="r", source_kind_pattern="github")
    assert rule.matches(make_finding(), None) is False
    assert rule.matches(make_finding(), "github") is True
    assert rule.matches(make_finding(), "gitlab") is False


def test_rule_no_filters_matches_everything():
    rule = RoutingRule(name="r")
    assert rule.matches(make_finding(), None) is True


# ---------------- Router construction ----------------


def test_router_rejects_unknown_transport_in_rule():
    notifiers = {"slack": RecordingNotifier("slack")}
    with pytest.raises(ValueError):
        Router(
            rules=[RoutingRule(name="r", transports=("missing",))],
            notifiers=notifiers,
        )


def test_router_rejects_unknown_default_transport():
    with pytest.raises(ValueError):
        Router(rules=[], notifiers={}, default_transport="ghost")


def test_router_exposes_rules_and_notifiers():
    n = RecordingNotifier("slack")
    rules = (RoutingRule(name="r", transports=("slack",)),)
    router = Router(rules=rules, notifiers={"slack": n})
    assert router.rules == rules
    assert router.notifiers == {"slack": n}


# ---------------- Router.route ----------------


async def test_route_empty_batch_returns_no_results():
    router = Router(rules=[], notifiers={})
    batch = NotificationBatch(scan_id="s", findings=(), severity_summary={})
    assert await router.route(batch) == []


async def test_route_severity_filter_directs_only_matching_findings():
    slack = RecordingNotifier("slack")
    smtp = RecordingNotifier("smtp")
    router = Router(
        rules=[
            RoutingRule(
                name="critical-only",
                severity_in=frozenset({"critical"}),
                transports=("slack",),
            ),
            RoutingRule(name="all", transports=("smtp",)),
        ],
        notifiers={"slack": slack, "smtp": smtp},
    )
    crit = make_finding(verification="passed", entity="EMAIL")
    low = make_finding(verification="unverified", score=0.2, entity="PHONE")
    batch = make_batch(crit, low)
    results = await router.route(batch)

    assert {r.transport for r in results} == {"slack", "smtp"}
    assert slack.batches[0].findings == (crit,)
    assert smtp.batches[0].findings == (crit, low)


async def test_route_multi_rule_fanout_to_two_transports():
    slack = RecordingNotifier("slack")
    splunk = RecordingNotifier("splunk")
    router = Router(
        rules=[
            RoutingRule(
                name="crit-slack",
                severity_in=frozenset({"critical"}),
                transports=("slack",),
            ),
            RoutingRule(
                name="aws-splunk",
                entity_pattern="AWS_*",
                transports=("splunk",),
            ),
        ],
        notifiers={"slack": slack, "splunk": splunk},
    )
    aws_crit = make_finding(entity="AWS_KEY", verification="passed")
    batch = make_batch(aws_crit)
    results = await router.route(batch)
    assert len(results) == 2
    assert slack.batches[0].findings == (aws_crit,)
    assert splunk.batches[0].findings == (aws_crit,)


async def test_route_dedupes_same_transport_listed_in_multiple_matching_rules():
    slack = RecordingNotifier("slack")
    router = Router(
        rules=[
            RoutingRule(name="r1", transports=("slack",)),
            RoutingRule(name="r2", entity_pattern="EMAIL", transports=("slack",)),
        ],
        notifiers={"slack": slack},
    )
    f = make_finding(entity="EMAIL")
    await router.route(make_batch(f))
    assert slack.batches[0].findings == (f,)


async def test_route_unmatched_falls_through_to_default_transport():
    catchall = RecordingNotifier("catchall")
    slack = RecordingNotifier("slack")
    router = Router(
        rules=[
            RoutingRule(
                name="crit",
                severity_in=frozenset({"critical"}),
                transports=("slack",),
            )
        ],
        notifiers={"slack": slack, "catchall": catchall},
        default_transport="catchall",
    )
    low = make_finding(score=0.1)
    await router.route(make_batch(low))
    assert catchall.batches[0].findings == (low,)
    assert slack.batches == []


async def test_route_unmatched_drops_with_stderr_warning_when_no_default(capsys):
    slack = RecordingNotifier("slack")
    router = Router(
        rules=[
            RoutingRule(
                name="crit",
                severity_in=frozenset({"critical"}),
                transports=("slack",),
            )
        ],
        notifiers={"slack": slack},
    )
    low = make_finding(score=0.1)
    results = await router.route(make_batch(low))
    captured = capsys.readouterr()
    assert "dropping 1 finding" in captured.err
    assert results == []


async def test_route_transport_exception_becomes_failed_result():
    bad = RecordingNotifier("bad", fail=True)
    router = Router(
        rules=[RoutingRule(name="r", transports=("bad",))],
        notifiers={"bad": bad},
    )
    results = await router.route(make_batch(make_finding()))
    assert len(results) == 1
    assert results[0].delivered is False
    assert "unhandled transport error" in (results[0].error or "")


async def test_route_sub_batch_severity_summary_recomputed():
    slack = RecordingNotifier("slack")
    router = Router(
        rules=[
            RoutingRule(
                name="crit",
                severity_in=frozenset({"critical"}),
                transports=("slack",),
            )
        ],
        notifiers={"slack": slack},
    )
    crit = make_finding(verification="passed")
    low = make_finding(score=0.1)
    await router.route(make_batch(crit, low))
    assert slack.batches[0].severity_summary == {"critical": 1}


async def test_route_propagates_metadata_to_sub_batch():
    slack = RecordingNotifier("slack")
    router = Router(
        rules=[RoutingRule(name="r", transports=("slack",))],
        notifiers={"slack": slack},
    )
    f = make_finding()
    batch = make_batch(f, source_kind="dir")
    await router.route(batch)
    assert slack.batches[0].metadata == {"source_kind": "dir"}


async def test_router_close_closes_all_notifiers():
    a = RecordingNotifier("a")
    b = RecordingNotifier("b")
    router = Router(rules=[], notifiers={"a": a, "b": b})
    await router.close()
    assert a.closed and b.closed
