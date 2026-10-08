"""US equity trading days (NYSE full-day holidays), static table with an explicit coverage range.

Source: NYSE "Holidays & Trading Hours" (https://www.nyse.com/markets/hours-calendars),
checked 2026-10-08 for 2026, 2027 and 2028. Early closes (1:00 p.m.) are trading
days. Outside ``COVERAGE_START``..``COVERAGE_END`` a day is unknown (``None``):
callers must treat the trading calendar as unknown, never fall back to plain
weekdays. Extend the table (and the coverage end) before 2028-12-31.
"""
from __future__ import annotations

from datetime import date, timedelta

SOURCE_URL = 'https://www.nyse.com/markets/hours-calendars'
VERIFIED_AT = '2026-10-08'
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2028, 12, 31)
HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    # 2026
    '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03',
    '2026-09-07', '2026-11-26', '2026-12-25',
    # 2027
    '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05',
    '2027-09-06', '2027-11-25', '2027-12-24',
    # 2028 (New Year's Day falls on a Saturday and is not observed)
    '2028-01-17', '2028-02-21', '2028-04-14', '2028-05-29', '2028-06-19', '2028-07-04', '2028-09-04',
    '2028-11-23', '2028-12-25',
))


def covered(day: date) -> bool:
    return COVERAGE_START <= day <= COVERAGE_END


def is_trading_day(day: date) -> bool | None:
    """True / False inside the coverage range, None outside it."""
    if not covered(day):
        return None
    return day.weekday() < 5 and day not in HOLIDAYS


def previous_trading_day(day: date) -> date | None:
    """Latest trading day strictly before ``day``; None when the calendar runs out of coverage."""
    day -= timedelta(days=1)
    while True:
        state = is_trading_day(day)
        if state is None:
            return None
        if state:
            return day
        day -= timedelta(days=1)


def trading_days_ending(end: date, count: int) -> list[date] | None:
    """``count`` trading days ending at ``end`` (inclusive, oldest first); None if unknown."""
    if is_trading_day(end) is not True:
        return None
    days = [end]
    while len(days) < count:
        prev = previous_trading_day(days[-1])
        if prev is None:
            return None
        days.append(prev)
    return list(reversed(days))


def trading_days_between(latest: date, expected: date) -> int | None:
    """Trading days from ``latest`` up to ``expected`` (0 when equal); None if unknown."""
    lag, day = 0, expected
    while day > latest:
        day = previous_trading_day(day)
        if day is None:
            return None
        lag += 1
    return lag
