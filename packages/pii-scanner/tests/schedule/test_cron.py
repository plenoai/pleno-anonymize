"""Tests for the minimal UTC cron parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pleno_pii_scanner.schedule.cron import CronExpression


class TestParseValidation:
    def test_empty_expression(self) -> None:
        with pytest.raises(ValueError, match="empty cron expression"):
            CronExpression.parse("   ")

    def test_too_few_fields(self) -> None:
        with pytest.raises(ValueError, match="must have 5 fields"):
            CronExpression.parse("0 0 *")

    def test_too_many_fields(self) -> None:
        with pytest.raises(ValueError, match="must have 5 fields"):
            CronExpression.parse("0 0 * * * *")

    def test_empty_subexpression(self) -> None:
        with pytest.raises(ValueError, match="empty subexpression"):
            CronExpression.parse("0,, * * * *")

    def test_non_integer_step(self) -> None:
        with pytest.raises(ValueError, match="non-integer step"):
            CronExpression.parse("*/abc * * * *")

    def test_zero_step(self) -> None:
        with pytest.raises(ValueError, match="step must be"):
            CronExpression.parse("*/0 * * * *")

    def test_non_integer_range(self) -> None:
        with pytest.raises(ValueError, match="non-integer range"):
            CronExpression.parse("a-b * * * *")

    def test_non_integer_value(self) -> None:
        with pytest.raises(ValueError, match="non-integer value"):
            CronExpression.parse("xyz * * * *")

    def test_value_below_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            CronExpression.parse("0 0 0 * *")  # day-of-month starts at 1

    def test_value_above_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            CronExpression.parse("60 * * * *")

    def test_inverted_range(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            CronExpression.parse("10-5 * * * *")


class TestMacros:
    @pytest.mark.parametrize(
        "macro,expected",
        [
            ("@yearly", (0, 0, 1, 1)),  # min=0 hour=0 dom=1 month=1
            ("@annually", (0, 0, 1, 1)),
            ("@monthly", (0, 0, 1, None)),
            ("@weekly", (0, 0, None, None)),  # dow=0 separately
            ("@daily", (0, 0, None, None)),
            ("@midnight", (0, 0, None, None)),
            ("@hourly", (0, None, None, None)),
        ],
    )
    def test_macro_expands(
        self, macro: str, expected: tuple[int, int, int | None, int | None]
    ) -> None:
        c = CronExpression.parse(macro)
        if expected[0] is not None:
            assert c.minutes == frozenset({expected[0]})
        if expected[1] is not None:
            assert c.hours == frozenset({expected[1]})
        if expected[2] is not None:
            assert c.days_of_month == frozenset({expected[2]})
        if expected[3] is not None:
            assert c.months == frozenset({expected[3]})

    def test_macro_case_insensitive(self) -> None:
        a = CronExpression.parse("@DAILY")
        b = CronExpression.parse("@daily")
        assert a.minutes == b.minutes


class TestNextAfter:
    def test_naive_datetime_rejected(self) -> None:
        c = CronExpression.parse("* * * * *")
        with pytest.raises(ValueError, match="naive datetime"):
            c.next_after(datetime(2026, 1, 1, 0, 0))

    def test_naive_in_matches_rejected(self) -> None:
        c = CronExpression.parse("* * * * *")
        with pytest.raises(ValueError, match="naive datetime"):
            c.matches(datetime(2026, 1, 1))

    def test_every_minute_advances_one_minute(self) -> None:
        c = CronExpression.parse("* * * * *")
        now = datetime(2026, 5, 4, 12, 30, 45, 123, tzinfo=UTC)
        nxt = c.next_after(now)
        assert nxt == datetime(2026, 5, 4, 12, 31, tzinfo=UTC)

    def test_hourly_top_of_hour(self) -> None:
        c = CronExpression.parse("@hourly")
        now = datetime(2026, 5, 4, 12, 30, tzinfo=UTC)
        assert c.next_after(now) == datetime(2026, 5, 4, 13, 0, tzinfo=UTC)

    def test_daily_skips_to_midnight(self) -> None:
        c = CronExpression.parse("@daily")
        now = datetime(2026, 5, 4, 23, 59, tzinfo=UTC)
        assert c.next_after(now) == datetime(2026, 5, 5, 0, 0, tzinfo=UTC)

    def test_skips_invalid_hour_within_day(self) -> None:
        # Same day match but later hour exercises the hour-skip branch.
        c = CronExpression.parse("0 12 * * *")
        now = datetime(2026, 5, 4, 6, 0, tzinfo=UTC)
        assert c.next_after(now) == datetime(2026, 5, 4, 12, 0, tzinfo=UTC)

    def test_step_minutes(self) -> None:
        c = CronExpression.parse("*/15 * * * *")
        now = datetime(2026, 5, 4, 12, 7, tzinfo=UTC)
        assert c.next_after(now) == datetime(2026, 5, 4, 12, 15, tzinfo=UTC)

    def test_dow_only_constrained(self) -> None:
        # Sundays at midnight; AND with dom=* keeps dow filter exclusive.
        c = CronExpression.parse("0 0 * * 0")
        # Mon May 4 2026 → next Sunday May 10.
        now = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
        assert c.next_after(now) == datetime(2026, 5, 10, 0, 0, tzinfo=UTC)

    def test_dom_only_constrained(self) -> None:
        c = CronExpression.parse("0 0 15 * *")
        now = datetime(2026, 5, 4, 0, 0, tzinfo=UTC)
        assert c.next_after(now) == datetime(2026, 5, 15, 0, 0, tzinfo=UTC)

    def test_dom_and_dow_or_semantics(self) -> None:
        # Both constrained → POSIX OR. Either the 1st OR a Sunday.
        c = CronExpression.parse("0 0 1 * 0")
        # Tue May 5 2026 → next match is Sun May 10 (sunday) before
        # Mon Jun 1 (1st of month). Verify OR not AND.
        now = datetime(2026, 5, 5, 0, 0, tzinfo=UTC)
        assert c.next_after(now) == datetime(2026, 5, 10, 0, 0, tzinfo=UTC)

    def test_february_29_in_leap_year(self) -> None:
        c = CronExpression.parse("0 0 29 2 *")
        # Look from May 2026; next Feb 29 is 2028.
        now = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
        assert c.next_after(now) == datetime(2028, 2, 29, 0, 0, tzinfo=UTC)

    def test_unsatisfiable_expression(self) -> None:
        # Feb 30 never exists.
        c = CronExpression.parse("0 0 30 2 *")
        with pytest.raises(ValueError, match="no matching time"):
            c.next_after(datetime(2026, 1, 1, 0, 0, tzinfo=UTC))

    def test_month_advances_across_year(self) -> None:
        c = CronExpression.parse("0 0 1 1 *")  # Jan 1 only
        now = datetime(2026, 6, 15, 0, 0, tzinfo=UTC)
        assert c.next_after(now) == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)

    def test_non_utc_input_normalised(self) -> None:
        from datetime import timezone

        c = CronExpression.parse("@hourly")
        jst = timezone(timedelta(hours=9))
        # 21:30 JST = 12:30 UTC; next hourly = 13:00 UTC.
        now = datetime(2026, 5, 4, 21, 30, tzinfo=jst)
        assert c.next_after(now) == datetime(2026, 5, 4, 13, 0, tzinfo=UTC)


class TestMatches:
    def test_match_exact(self) -> None:
        c = CronExpression.parse("30 12 * * *")
        assert c.matches(datetime(2026, 5, 4, 12, 30, tzinfo=UTC))

    def test_match_negative(self) -> None:
        c = CronExpression.parse("30 12 * * *")
        assert not c.matches(datetime(2026, 5, 4, 12, 31, tzinfo=UTC))
