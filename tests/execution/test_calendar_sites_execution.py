# tests/execution/test_calendar_sites_execution.py
from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from lib import trading_calendar as tc

ET = ZoneInfo('America/New_York')


@pytest.fixture
def master(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-08-17', '2026-09-30') if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: (_ for _ in ()).throw(AssertionError('alpaca probe must not run')))
    yield
    tc.clear_cache()


def test_engine_next_trading_day_skips_labor_day_without_cli(master, monkeypatch):
    from execution import engine
    monkeypatch.setattr(engine, '_ALPACA_BIN', '/nonexistent/alpaca', raising=False)
    assert engine._next_trading_day(dt.date(2026, 9, 4)) == dt.date(2026, 9, 8)
    assert engine._is_trading_session(dt.date(2026, 9, 7)) is False
    assert engine._is_trading_session(dt.date(2026, 9, 8)) is True


def test_handoff_previous_trading_day_skips_holiday(master):
    from execution.trade_handoff_builder import _previous_trading_day
    assert _previous_trading_day('2026-09-08') == '2026-09-04'


def test_option_hedge_next_trading_day_skips_holiday(master):
    from execution.option_hedge import _next_trading_day
    assert _next_trading_day(dt.date(2026, 9, 4)) == dt.date(2026, 9, 8)


def test_executor_static_session_closed_on_holiday(master):
    from execution.alpaca_executor import _static_session
    t = dt.time
    now = dt.datetime(2026, 9, 7, 11, 0, tzinfo=ET)     # Labor Day, mid-morning
    assert _static_session(now, t(4, 0), t(9, 30), t(16, 0), t(20, 0)) == 'closed'
    now = dt.datetime(2026, 9, 8, 11, 0, tzinfo=ET)
    assert _static_session(now, t(4, 0), t(9, 30), t(16, 0), t(20, 0)) == 'rth'
