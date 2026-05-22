"""SP-2 Phase A: trading-day iterator for resolver-driven backtests.

Yields business days (Mon-Fri) in [start, end]. US market holidays are
NOT excluded — strategies that need true NYSE calendars should call
the resolver inside their existing bar-iteration loop with the calendar
they already use. For Phase A acceptance + the smoke tests, business-day
granularity is sufficient.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Iterator


def trading_days(start: date, end: date) -> Iterator[date]:
    d = start
    while d <= end:
        if d.weekday() < 5:  # 0=Mon, 4=Fri
            yield d
        d += timedelta(days=1)
