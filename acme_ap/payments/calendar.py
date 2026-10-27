"""US bank business days: weekends and Federal Reserve holidays (with Saturday->Friday / Sunday->Monday observance).
Banks do not move money on other days, so ACH lead time and settlement are counted in these, not calendar days."""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_monday(year: int, month: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=last.weekday())


@lru_cache(maxsize=None)
def holidays(year: int) -> frozenset[date]:
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    observed = {d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d for d in fixed}
    floating = {_nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3), _last_monday(year, 5), _nth_weekday(year, 9, 0, 1),
                _nth_weekday(year, 10, 0, 2), _nth_weekday(year, 11, 3, 4)}
    # New Year's Day observed on Dec 31 belongs to the previous year's calendar
    extra = {date(year, 12, 31)} if date(year + 1, 1, 1).weekday() == 5 else set()
    return frozenset(observed | floating | extra)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def next_business_day(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def prev_business_day(d: date) -> date:
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        n -= is_business_day(d)
    return d


def sub_business_days(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        n -= is_business_day(d)
    return d
