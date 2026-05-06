"""Minimal UTC-only cron parser for ScheduleRegistry (ADR-0007 §12).

Supports:
  * 5-field syntax: `minute hour day-of-month month day-of-week`
  * shortcuts: @yearly @annually @monthly @weekly @daily @midnight @hourly
  * field syntax: `*`, `n`, `n-m`, `*/k`, `n-m/k`, comma-separated lists
  * dom/dow OR semantics when both fields are constrained (POSIX behavior)

Deliberately UTC-only. The rest of the system stores `datetime.now(UTC)`,
and local-time scheduling reintroduces DST gaps and ambiguous-hour bugs
that have no business in an enterprise scanner. Operators who need
"every weekday at 9am Tokyo" express it as `0 0 * * 1-5` in UTC plus a
documented offset on the Schedule's tags.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


_MACROS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# 4 leap-cycles bounds any satisfiable cron, including `0 0 29 2 *`.
# Beyond that we declare the expression unsatisfiable rather than loop
# forever — protects long-lived registry processes from a typo'd `0 0 30 2 *`.
_MAX_LOOKAHEAD_DAYS = 4 * 366


def _parse_field(spec: str, lo: int, hi: int) -> frozenset[int]:
    """Parse one cron field into the explicit set of allowed values.

    Materialising the full set up front keeps `next_after` honest — every
    match check is a single set membership, with no per-call algebra to
    re-derive the allowed values.
    """
    out: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise ValueError(f"empty subexpression in {spec!r}")
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError as exc:
                raise ValueError(f"non-integer step in {part!r}") from exc
            if step < 1:
                raise ValueError(f"step must be >=1 in {part!r}")
        else:
            base, step = part, 1
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_s, end_s = base.split("-", 1)
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise ValueError(f"non-integer range in {part!r}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ValueError(f"non-integer value in {part!r}") from exc
        if start < lo or end > hi or start > end:
            raise ValueError(f"value(s) out of range [{lo}, {hi}] in {part!r}")
        out.update(range(start, end + 1, step))
    return frozenset(out)


@dataclass(frozen=True, slots=True)
class CronExpression:
    """Pre-parsed cron expression. Construct via `CronExpression.parse()`.

    `dom_constrained` / `dow_constrained` record whether the original
    field text was something other than `*`. POSIX cron requires the
    OR-semantics combinator only when *both* fields restrict the set;
    we cannot recover that from `frozenset` alone (`*` parses to a full
    set, indistinguishable from an explicit enumeration).
    """

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_constrained: bool
    dow_constrained: bool

    @classmethod
    def parse(cls, expr: str) -> CronExpression:
        raw = expr.strip()
        if not raw:
            raise ValueError("empty cron expression")
        macro = _MACROS.get(raw.lower())
        normalized = macro if macro is not None else raw
        parts = normalized.split()
        if len(parts) != 5:
            raise ValueError(
                f"cron expression must have 5 fields; got {len(parts)} in {expr!r}"
            )
        m, h, dom, mon, dow = parts
        return cls(
            expr=raw,
            minutes=_parse_field(m, 0, 59),
            hours=_parse_field(h, 0, 23),
            days_of_month=_parse_field(dom, 1, 31),
            months=_parse_field(mon, 1, 12),
            days_of_week=_parse_field(dow, 0, 6),
            dom_constrained=(dom != "*"),
            dow_constrained=(dow != "*"),
        )

    def matches(self, dt: datetime) -> bool:
        """True iff `dt` (UTC, minute-aligned) satisfies this expression."""
        if dt.tzinfo is None:
            raise ValueError("naive datetime not allowed; pass tz-aware UTC")
        u = dt.astimezone(UTC)
        return (
            u.month in self.months
            and u.hour in self.hours
            and u.minute in self.minutes
            and self._matches_day(u)
        )

    def next_after(self, after: datetime) -> datetime:
        """Smallest UTC `datetime > after` that satisfies this expression.

        Steps the candidate by the largest field that currently fails
        (month → day → hour → minute) so the worst case is bounded by
        days, not minutes — `0 0 29 2 *` finds the next Feb 29 in
        ~1500 day iterations rather than ~2 million minute iterations.
        """
        if after.tzinfo is None:
            raise ValueError("naive datetime not allowed; pass tz-aware UTC")
        candidate = after.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(
            minutes=1
        )
        deadline = candidate + timedelta(days=_MAX_LOOKAHEAD_DAYS)
        while candidate <= deadline:
            if candidate.month not in self.months:
                candidate = _first_of_next_month(candidate)
                continue
            if not self._matches_day(candidate):
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hours:
                candidate = candidate.replace(minute=0) + timedelta(hours=1)
                continue
            if candidate.minute in self.minutes:
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError(
            f"no matching time within {_MAX_LOOKAHEAD_DAYS} days "
            f"of {after.isoformat()} for {self.expr!r}"
        )

    def _matches_day(self, dt: datetime) -> bool:
        # Python weekday(): Monday=0..Sunday=6. Cron: Sunday=0..Saturday=6.
        cron_dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self.days_of_month
        dow_ok = cron_dow in self.days_of_week
        if self.dom_constrained and self.dow_constrained:
            return dom_ok or dow_ok
        return dom_ok and dow_ok


def _first_of_next_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1, day=1, hour=0, minute=0)
    return dt.replace(month=dt.month + 1, day=1, hour=0, minute=0)


__all__ = ["CronExpression"]
