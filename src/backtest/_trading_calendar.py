"""SP-2 Phase A: trading-day iterator for resolver-driven backtests.

2026-09-04: yields NYSE SESSIONS from the trading_calendar master
(lib.trading_calendar), no longer Mon–Fri. Holidays are excluded; when the
master is absent the library falls back (alpaca CLI, then weekday math with a
WARNING) so this iterator never raises.
"""
from __future__ import annotations
from datetime import date
from typing import Iterator

try:
    from lib.trading_calendar import sessions
except ModuleNotFoundError:  # ROOT-only sys.path callers
    from src.lib.trading_calendar import sessions


def trading_days(start: date, end: date) -> Iterator[date]:
    yield from sessions(start, end)
