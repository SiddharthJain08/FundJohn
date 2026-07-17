# tests/test_engine_equity_calendar.py
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402


class _RecordStrat:
    id = 'S_fake'
    def __init__(self):
        self.seen_index = None
    def generate_signals(self, prices, regime, universe, aux):
        self.seen_index = list(prices.index)
        return []


def _panel():
    idx = pd.to_datetime(['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-08'])  # Fri Sat Sun Mon
    return pd.DataFrame({'AAPL': [100.0, np.nan, np.nan, 101.0],
                         'BTC-USD': [4e4, 4.05e4, 4.02e4, 4.1e4]}, index=idx)


def _wire(monkeypatch, instrument_class):
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: instrument_class)
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_apply_regime_overrides_to_signals', lambda *a, **k: None)


def test_gate_off_keeps_weekend_rows(monkeypatch):
    monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
    _wire(monkeypatch, 'equity')
    s = _RecordStrat()
    engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['AAPL'], {})
    assert len(s.seen_index) == 4  # weekend rows present (byte-identical)


def test_gate_on_equity_drops_weekend_rows(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
    _wire(monkeypatch, 'equity')
    s = _RecordStrat()
    engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['AAPL'], {})
    assert len(s.seen_index) == 2  # Sat/Sun dropped


def test_gate_on_crypto_keeps_weekend_rows(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
    _wire(monkeypatch, 'crypto')
    # crypto path needs a regime; stub the loader to a usable state
    monkeypatch.setattr(engine, 'load_crypto_regime_state', lambda: {'state': 'LOW_VOL'})
    s = _RecordStrat()
    engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['BTC-USD'], {})
    assert len(s.seen_index) == 4  # crypto keeps the union calendar
