from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from lib import trading_calendar as tc


@pytest.fixture
def master(tmp_path, monkeypatch):
    rows = []
    for d in pd.bdate_range('2026-03-30', '2026-04-10'):
        if d.date() != dt.date(2026, 4, 3):        # Good Friday
            rows.append({'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True})
    for d in pd.bdate_range('2019-04-15', '2019-04-26'):
        if d.date() != dt.date(2019, 4, 19):        # Good Friday on the 3rd Friday
            rows.append({'date': d.date(), 'open': '09:30', 'close': '16:00', 'active': True})
    p = tmp_path / 'cal.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    tc.clear_cache()
    yield
    tc.clear_cache()


def test_backtest_trading_days_skips_good_friday(master):
    from backtest._trading_calendar import trading_days
    days = list(trading_days(dt.date(2026, 4, 1), dt.date(2026, 4, 7)))
    assert days == [dt.date(2026, 4, 1), dt.date(2026, 4, 2), dt.date(2026, 4, 6), dt.date(2026, 4, 7)]


def test_nearest_monthly_expiry_moves_to_thursday_when_third_friday_is_closed(master):
    from backtest.options_pricing import nearest_monthly_expiry
    assert nearest_monthly_expiry(dt.date(2019, 3, 20), dte_target=25) == dt.date(2019, 4, 18)
