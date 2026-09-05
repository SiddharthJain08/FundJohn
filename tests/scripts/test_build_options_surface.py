from __future__ import annotations
import importlib.util, json, sys
from pathlib import Path
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def _mod():
    spec = importlib.util.spec_from_file_location('build_options_surface', ROOT / 'scripts' / 'build_options_surface.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_build_rows_from_real_chain_fixture():
    m = _mod()
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    spots = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))['spots']
    rows = m.build_rows(chain, {(t, pd.Timestamp('2026-09-03')): s for t, s in spots.items()})
    assert set(rows['ticker']) == {'SPY', 'AAPL', 'XOM'}
    spy = rows[rows.ticker == 'SPY'].iloc[0]
    assert 0.08 < spy['iv30'] < 0.20            # true 30d ATM, not the 0.40 chain mean
    assert spy['n_expiries_fit'] >= 5 and spy['options_features_version'] == 2
    assert 'built_at' in rows.columns and rows['date'].astype(str).unique().tolist() == ['2026-09-03']
    for c in m.SCALAR_KEYS:
        assert c in rows.columns


def test_run_writes_master_upsert(tmp_path, monkeypatch):
    m = _mod()
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    spots = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))['spots']
    monkeypatch.setattr(m, '_read_range', lambda s, e, tickers=None: chain)
    monkeypatch.setattr(m, 'read_spots', lambda tickers, s, e: {(t, pd.Timestamp('2026-09-03')): v for t, v in spots.items()})
    out = tmp_path / 'options_surface.parquet'
    assert m.run('2026-09-03', '2026-09-03', path=out) == 0
    assert m.run('2026-09-03', '2026-09-03', path=out) == 0        # idempotent upsert
    df = pd.read_parquet(out)
    assert len(df) == 3 and df.duplicated(['ticker', 'date']).sum() == 0
