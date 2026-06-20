# tests/test_price_panel.py
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import price_panel as pp  # noqa: E402


class TestIsEquityTicker:
    def test_classifies(self):
        assert pp.is_equity_ticker('AAPL')
        assert pp.is_equity_ticker('SPY')
        assert not pp.is_equity_ticker('^VIX')
        assert not pp.is_equity_ticker('BTC-USD')
        assert not pp.is_equity_ticker('CL=F')
        assert not pp.is_equity_ticker('EURUSD=X')


class TestApplyEquityCalendar:
    def test_drops_weekend_only_rows(self):
        idx = pd.to_datetime(['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-08'])  # Fri Sat Sun Mon
        df = pd.DataFrame({'AAPL': [100.0, np.nan, np.nan, 101.0],
                           'BTC-USD': [4e4, 4.05e4, 4.02e4, 4.1e4]}, index=idx)
        out = pp.apply_equity_calendar(df)
        assert list(out.index) == [idx[0], idx[3]]
        assert list(out.columns) == ['AAPL', 'BTC-USD']

    def test_keeps_row_with_any_equity_obs(self):
        idx = pd.to_datetime(['2024-01-05', '2024-01-08'])
        df = pd.DataFrame({'AAPL': [np.nan, 50.0], 'MSFT': [100.0, np.nan]}, index=idx)
        assert pp.apply_equity_calendar(df).shape[0] == 2

    def test_no_equity_columns_noop(self):
        idx = pd.to_datetime(['2024-01-06', '2024-01-07'])
        df = pd.DataFrame({'BTC-USD': [1.0, 2.0]}, index=idx)
        assert pp.apply_equity_calendar(df).shape[0] == 2


class TestGate:
    def test_enabled(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
        assert pp.equity_calendar_enabled() is False
        monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
        assert pp.equity_calendar_enabled() is True
        monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '0')
        assert pp.equity_calendar_enabled() is False

    def test_calendar_for_off(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
        for ic in ('equity', 'etp', 'option', 'crypto'):
            assert pp.calendar_for(ic) == 'union'

    def test_calendar_for_on(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
        assert pp.calendar_for('equity') == 'equity'
        assert pp.calendar_for('etp') == 'equity'
        assert pp.calendar_for('option') == 'equity'
        assert pp.calendar_for('crypto') == 'union'
