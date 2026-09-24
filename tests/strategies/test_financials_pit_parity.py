"""A1 parity (spec 2026-09-12 §A1): the backtest financials slice must have the
SAME dict shape as engine.py's live aux['financials'] on the same frame — PIT
must not add, drop, or rename a key any strategy reads.

Synthetic frames only. engine.load_aux_data's financials block reads
ROOT/'data'/'master'/'financials.parquet', so ROOT is monkeypatched at the
module to a tmp dir holding ONLY that file; every other aux block (insider,
sentiment, options) is absent or DB-backed and degrades to a warning, which is
exactly what we want — this test asserts on aux['financials'] and nothing else.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from strategies import aux_data_loader as adl

FIN = pd.DataFrame({
    'ticker':            ['AAA', 'BBB'],
    'period':            ['2026Q1', '2026Q1'],
    'date':              ['2026-03-31', '2026-03-31'],
    'roe':               [0.31, 0.12],
    'roic':              [0.22, 0.09],
    'gross_margin':      [0.45, 0.30],
    'debt_equity_ratio': [0.80, 1.40],
    'ev_ebitda':         [14.0, 9.0],
    'p_fcf_ratio':       [22.0, 11.0],
    'total_assets':      [3.6e11, 1.0e11],
    'total_liabilities': [2.6e11, 0.7e11],
    'retained_earnings': [0.5e11, 0.2e11],
    'working_capital':   [1.2e10, 0.4e10],
    'operating_income':  [1.1e11, 0.2e11],
    'market_cap':        [3.0e12, 4.0e11],
    'net_income':        [0.9e11, 0.1e11],
})
EARN = pd.DataFrame({'ticker': ['AAA', 'BBB'], 'date': ['2026-05-05', '2026-05-06']})


@pytest.fixture()
def _shared_frames(monkeypatch, tmp_path):
    master = tmp_path / 'data' / 'master'
    master.mkdir(parents=True)
    FIN.to_parquet(master / 'financials.parquet', index=False)
    EARN.to_parquet(master / 'earnings.parquet', index=False)
    monkeypatch.setattr(adl, 'FINANCIALS_PATH', master / 'financials.parquet')
    monkeypatch.setattr(adl, 'EARNINGS_PATH', master / 'earnings.parquet')
    monkeypatch.setattr(adl, '_FIN_DF', None)
    monkeypatch.setattr(adl, '_FIN_AVAIL_DF', None)
    monkeypatch.setattr(adl, '_EARNINGS_DF', None)
    from execution import engine
    monkeypatch.setattr(engine, 'ROOT', tmp_path)
    return engine


@pytest.mark.parametrize('pit', ['0', '1'])
def test_backtest_slice_shape_equals_live_shape(_shared_frames, monkeypatch, pit):
    engine = _shared_frames
    monkeypatch.setenv('OPENCLAW_FINANCIALS_PIT', pit)
    live = engine.load_aux_data(['AAA', 'BBB'], as_of='2026-06-30').get('financials', {})
    bt = adl._financials_slice('2026-06-30')
    assert set(bt) == set(live) == {'AAA', 'BBB'}
    for ticker in ('AAA', 'BBB'):
        assert set(bt[ticker]) == set(live[ticker]), ticker
        assert bt[ticker]['returnOnEquity'] == live[ticker]['returnOnEquity']
        assert bt[ticker]['totalAssets'] == live[ticker]['totalAssets']
    assert 'available_at' not in bt['AAA']
    assert 'ticker' not in bt['AAA']
