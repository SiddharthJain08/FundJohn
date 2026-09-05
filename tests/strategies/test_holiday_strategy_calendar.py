from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from lib import trading_calendar as tc


@pytest.fixture
def master(tmp_path, monkeypatch):
    closed = {dt.date(2026, 9, 7), dt.date(2026, 11, 26), dt.date(2026, 4, 3)}   # Labor Day, Thanksgiving, Good Friday
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True}
            for d in pd.bdate_range('2026-03-01', '2026-12-31') if d.date() not in closed]
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda a, b: (_ for _ in ()).throw(AssertionError('alpaca probe must not run')))
    yield
    tc.clear_cache()


def test_holidays_are_exchange_closures_not_federal(master):
    from strategies.implementations import S_holiday_seasonality_energy_etf_tv1 as s
    hs = {h.date() for h in s._holidays_near(pd.Timestamp('2026-10-15'), window_days=45)}
    assert dt.date(2026, 10, 12) not in hs      # Columbus Day: NYSE open
    assert dt.date(2026, 11, 26) in hs          # Thanksgiving
    hs2 = {h.date() for h in s._holidays_near(pd.Timestamp('2026-04-10'), window_days=20)}
    assert dt.date(2026, 4, 3) in hs2           # Good Friday: NYSE closed, not federal


def test_entry_exit_use_sessions(master):
    from strategies.implementations import S_holiday_seasonality_energy_etf_tv1 as s
    entry, exit_ = s._entry_exit_for_holiday(pd.Timestamp('2026-09-07'))
    assert exit_ == pd.Timestamp('2026-09-04')
    assert entry == pd.Timestamp('2026-08-26')   # prior[-8]: 09-04,09-03,09-02,09-01,08-31,08-28,08-27,08-26
