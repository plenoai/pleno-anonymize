"""Tests for Schedule + SLAPolicy + Schedule.with_run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pleno_pii_scanner.schedule.base import (
    SLAPolicy,
    Schedule,
    ScheduleOutcome,
)
from pleno_pii_scanner.schedule.cron import CronExpression


class TestScheduleConstruction:
    def test_rejects_empty_id(self) -> None:
        with pytest.raises(ValueError, match="id must be non-empty"):
            Schedule(
                id="",
                cron=CronExpression.parse("@hourly"),
                plan_ref="x",
            )

    def test_rejects_empty_plan_ref(self) -> None:
        with pytest.raises(ValueError, match="plan_ref must be non-empty"):
            Schedule(
                id="s1",
                cron=CronExpression.parse("@hourly"),
                plan_ref="",
            )

    def test_rejects_negative_jitter(self) -> None:
        with pytest.raises(ValueError, match="jitter_seconds must be >= 0"):
            Schedule(
                id="s1",
                cron=CronExpression.parse("@hourly"),
                plan_ref="x",
                jitter_seconds=-1,
            )

    def test_with_run_replaces_run_state(self) -> None:
        s = Schedule(
            id="s1",
            cron=CronExpression.parse("@hourly"),
            plan_ref="x",
        )
        ran_at = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        nxt = datetime(2026, 5, 4, 13, 0, tzinfo=UTC)
        s2 = s.with_run(
            ran_at=ran_at,
            next_run_at=nxt,
            outcome=ScheduleOutcome.SUCCESS,
            error=None,
        )
        assert s2.last_run_at == ran_at
        assert s2.next_run_at == nxt
        assert s2.last_outcome is ScheduleOutcome.SUCCESS
        assert s2.last_error is None
        # Original is frozen, untouched.
        assert s.last_run_at is None


class TestSLAPolicy:
    def test_default_uses_adr_thresholds(self) -> None:
        p = SLAPolicy.default()
        assert p.by_severity["critical"] == timedelta(hours=1)
        assert p.by_severity["high"] == timedelta(hours=24)
        assert p.by_severity["medium"] == timedelta(days=7)
        assert p.by_severity["low"] is None

    def test_deadline_returns_opened_plus_max_age(self) -> None:
        p = SLAPolicy.default()
        opened = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        assert p.deadline("critical", opened) == opened + timedelta(hours=1)

    def test_deadline_returns_none_for_uncovered_severity(self) -> None:
        p = SLAPolicy.default()
        opened = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        assert p.deadline("low", opened) is None
        # Unknown severity also returns None — never crashes the registry.
        assert p.deadline("ridiculous", opened) is None
