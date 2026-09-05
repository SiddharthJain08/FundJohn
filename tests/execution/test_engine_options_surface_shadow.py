from __future__ import annotations
import json, logging
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def _inputs(tmp_path):
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    chain['date'] = pd.to_datetime(chain['date']); chain['expiry'] = pd.to_datetime(chain['expiry'])
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c}
                       for t, m in meta['closes'].items() for d, c in m.items()])
    master = tmp_path / 'master'; master.mkdir()
    # 30 prior sessions of surface history so iv_rank is computable
    hist = pd.DataFrame([{'ticker': t, 'date': d.date(), 'iv30': 0.20 + 0.001 * i, 'pc_ratio': 1.0,
                          'options_features_version': 2}
                         for t in ('SPY', 'AAPL', 'XOM') for i, d in enumerate(pd.bdate_range('2026-07-20', '2026-09-02'))])
    hist.to_parquet(master / 'options_surface.parquet', index=False)
    return chain, px, master


def test_build_returns_v2_keys_with_iv_rank(tmp_path):
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    out = v2.build(chain, ['SPY', 'AAPL', 'XOM', 'ZZZT'], pd.Timestamp('2026-09-03'), master, px)
    assert set(out) == {'SPY', 'AAPL', 'XOM'}
    spy = out['SPY']
    assert spy['options_features_version'] == 2 and 0.08 < spy['iv30'] < 0.20
    assert spy['iv_rank'] is not None and 0 <= spy['iv_rank'] <= 100
    assert spy['last_price'] == pytest.approx(px[(px.ticker == 'SPY') & (px.date == '2026-09-03')]['close'].iloc[0])
    assert isinstance(spy['hv20_history'], list) and spy['rv_20'] > 0
    for k in ('gamma_atm', 'theta_atm', 'pc_ratio', 'vrp', 'expiry_date', 'earnings_dte'):
        assert k in spy


def test_master_row_precedence(tmp_path):
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    m = pd.read_parquet(master / 'options_surface.parquet')
    m = pd.concat([m, pd.DataFrame([{'ticker': 'SPY', 'date': pd.Timestamp('2026-09-03').date(), 'iv30': 0.777,
                                     'pc_ratio': 1.0, 'options_features_version': 2}])], ignore_index=True)
    m.to_parquet(master / 'options_surface.parquet', index=False)
    out = v2.build(chain, ['SPY'], pd.Timestamp('2026-09-03'), master, px)
    assert out['SPY']['iv30'] == pytest.approx(0.777)


def test_shadow_summary_line():
    from execution import options_aux_v2 as v2
    old = {'A': {'iv30': 0.40, 'iv_rank': 50.0}, 'B': {'iv30': 0.50, 'iv_rank': 50.0}}
    new = {'A': {'iv30': 0.20, 'iv_rank': 33.0}, 'B': {'iv30': 0.25, 'iv_rank': None}}
    line = v2.shadow_summary(old, new)
    assert line.startswith('[options_surface] shadow n=2 iv30 old/new median=2.000') and 'iv_rank_nonnull=50%' in line and 'version=2' in line


def test_engine_flag_selects_dict(monkeypatch, caplog, tmp_path):
    from execution import engine, options_aux_v2 as v2
    old = {'SPY': {'iv30': 0.4, 'iv_rank': 50.0}}
    new = {'SPY': {'iv30': 0.12, 'iv_rank': 40.0, 'options_features_version': 2}}
    monkeypatch.setattr(v2, 'build', lambda *a, **k: new)
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE', raising=False)
    with caplog.at_level(logging.INFO):
        assert engine._apply_options_surface(old, None, [], None, None, None) is old
    assert any('[options_surface] shadow' in r.message for r in caplog.records)
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE', '1')
    assert engine._apply_options_surface(old, None, [], None, None, None) is new
