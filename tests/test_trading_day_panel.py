# tests/test_trading_day_panel.py
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


class TestIsEquityTicker:
    def test_classifies_tickers(self):
        assert ub._is_equity_ticker('AAPL')
        assert ub._is_equity_ticker('SPY')
        assert not ub._is_equity_ticker('^VIX')
        assert not ub._is_equity_ticker('BTC-USD')
        assert not ub._is_equity_ticker('CL=F')
        assert not ub._is_equity_ticker('EURUSD=X')


class TestApplyEquityCalendar:
    def test_drops_weekend_only_rows(self):
        # Fri, Sat, Sun, Mon. Equity trades Fri+Mon; crypto trades all four.
        idx = pd.to_datetime(['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-08'])
        df = pd.DataFrame({'AAPL': [100.0, np.nan, np.nan, 101.0],
                           'BTC-USD': [40000.0, 40500.0, 40250.0, 41000.0]}, index=idx)
        out = ub._apply_equity_calendar(df)
        assert list(out.index) == [idx[0], idx[3]]
        assert list(out.columns) == ['AAPL', 'BTC-USD']  # columns untouched

    def test_keeps_row_with_any_equity_obs(self):
        idx = pd.to_datetime(['2024-01-05', '2024-01-08'])
        df = pd.DataFrame({'AAPL': [np.nan, 50.0], 'MSFT': [100.0, np.nan],
                           'BTC-USD': [1.0, 2.0]}, index=idx)
        out = ub._apply_equity_calendar(df)
        assert out.shape[0] == 2  # both rows have >=1 equity obs

    def test_no_equity_columns_is_noop(self):
        idx = pd.to_datetime(['2024-01-06', '2024-01-07'])
        df = pd.DataFrame({'BTC-USD': [1.0, 2.0]}, index=idx)
        out = ub._apply_equity_calendar(df)
        assert out.shape[0] == 2  # nothing to anchor on -> return unchanged


class TestGateAndDispatch:
    def test_gate_default_off(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', raising=False)
        assert ub._equity_calendar_enabled() is False

    def test_gate_on_off(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '1')
        assert ub._equity_calendar_enabled() is True
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '0')
        assert ub._equity_calendar_enabled() is False

    def test_calendar_for_gate_off(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', raising=False)
        for ic in ('equity', 'etp', 'option', 'crypto'):
            assert ub._calendar_for(ic) == 'union'

    def test_calendar_for_gate_on(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '1')
        assert ub._calendar_for('equity') == 'equity'
        assert ub._calendar_for('etp') == 'equity'
        assert ub._calendar_for('option') == 'equity'
        assert ub._calendar_for('crypto') == 'union'  # crypto NEVER aligned


class TestLoadPricesPanelsDispatch:
    def test_applies_filter_only_for_equity_calendar(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ub, '_apply_equity_calendar',
                            lambda cw: (calls.append('called'), cw)[1])
        # Stub the heavy read + quarantine so no parquet/DB is needed.
        raw = pd.DataFrame({'ticker': ['AAPL'], 'date': ['2024-01-05'],
                            'open': [1.0], 'high': [1.0], 'low': [1.0], 'close': [1.0]})
        monkeypatch.setattr(ub.pd, 'read_parquet', lambda *a, **k: raw.copy())
        import pipeline.quarantine_filter as qf
        monkeypatch.setattr(qf, 'filter_quarantined', lambda p, name: p)

        ub.load_prices_panels(calendar='union')
        assert calls == []
        ub.load_prices_panels(calendar='equity')
        assert calls == ['called']


class TestRunBacktestWiring:
    def test_requests_calendar_by_instrument_class(self, monkeypatch):
        captured = {}

        def fake_load(calendar='union'):
            captured['calendar'] = calendar
            raise RuntimeError('stop-after-load')  # short-circuit the heavy path

        monkeypatch.setattr(ub, 'load_prices_panels', fake_load)
        monkeypatch.setattr(ub, 'find_strategy_file', lambda sid: 'x.py')
        monkeypatch.setattr(ub, 'load_strategy_class',
                            lambda fp: type('S', (), {'__name__': 'S', 'active_in_regimes': []}))
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '1')

        with pytest.raises(RuntimeError, match='stop-after-load'):
            ub.run_backtest('S_x', instrument_class='equity', commit=False)
        assert captured['calendar'] == 'equity'

        with pytest.raises(RuntimeError, match='stop-after-load'):
            ub.run_backtest('S_x', instrument_class='crypto', commit=False)
        assert captured['calendar'] == 'union'  # crypto stays union even with gate on

    def test_gate_off_requests_union(self, monkeypatch):
        captured = {}

        def fake_load(calendar='union'):
            captured['calendar'] = calendar
            raise RuntimeError('stop-after-load')

        monkeypatch.setattr(ub, 'load_prices_panels', fake_load)
        monkeypatch.setattr(ub, 'find_strategy_file', lambda sid: 'x.py')
        monkeypatch.setattr(ub, 'load_strategy_class',
                            lambda fp: type('S', (), {'__name__': 'S', 'active_in_regimes': []}))
        monkeypatch.delenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', raising=False)

        with pytest.raises(RuntimeError, match='stop-after-load'):
            ub.run_backtest('S_x', instrument_class='equity', commit=False)
        assert captured['calendar'] == 'union'
