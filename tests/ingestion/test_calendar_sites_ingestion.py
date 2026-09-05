# tests/ingestion/test_calendar_sites_ingestion.py
from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from lib import trading_calendar as tc


@pytest.fixture
def master(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-08-03', '2026-09-30') if d.date() != dt.date(2026, 9, 7)]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: (_ for _ in ()).throw(AssertionError('alpaca probe must not run')))
    yield
    tc.clear_cache()


def test_cboe_session_date_rolls_holiday_to_prior_session(master):
    from ingestion.ingest_cboe_chains import session_date_for
    assert session_date_for('2026-09-07 17:05:00') == dt.date(2026, 9, 4)   # Labor Day stamp
    assert session_date_for('2026-09-08 08:00:00') == dt.date(2026, 9, 4)   # pre-open Tuesday
    assert session_date_for('2026-09-08 17:05:00') == dt.date(2026, 9, 8)


def test_finra_prev_bday_skips_holiday(master):
    from ingestion.ingest_finra_short_interest import _prev_bday
    assert _prev_bday(dt.date(2026, 9, 8)) == dt.date(2026, 9, 4)


def test_nasdaq_business_days_are_sessions(master):
    from ingestion.ingest_nasdaq_earnings_calendar import business_days
    days = business_days(dt.date(2026, 9, 4), days_back=2, days_ahead=2)
    assert days == [dt.date(2026, 9, 2), dt.date(2026, 9, 3), dt.date(2026, 9, 4),
                    dt.date(2026, 9, 8), dt.date(2026, 9, 9)]
    assert business_days(dt.date(2026, 9, 7), days_back=1, days_ahead=1) == [dt.date(2026, 9, 4), dt.date(2026, 9, 8)]


def test_pipeline_check_is_trading_day_uses_master(master, monkeypatch):
    from system_checks.checks import pipeline as pc
    monkeypatch.setattr(pc, 'ALPACA_CLI', '/nonexistent/alpaca')
    assert pc._is_trading_day(dt.date(2026, 9, 7)) is False
    assert pc._is_trading_day(dt.date(2026, 9, 8)) is True
