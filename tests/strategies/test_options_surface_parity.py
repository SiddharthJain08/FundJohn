# tests/strategies/test_options_surface_parity.py
from __future__ import annotations
import importlib.util, json
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'
SHARED = ['iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d', 'ts_ratio',
          'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm', 'call_volume', 'put_volume', 'volume',
          'pc_ratio', 'expiry_date', 'n_expiries_fit', 'n_strikes_30d', 'options_features_version',
          'mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d',
          'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d']


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str):
        return a == b
    return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)))


def test_live_and_builder_agree_on_every_shared_key(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE_PATH', raising=False)
    from execution import options_aux_v2 as v2
    bos = _script('build_options_surface')
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    day = pd.Timestamp('2026-09-03')
    built = bos.build_rows(chain.assign(date=pd.to_datetime(chain['date'])), {(t, day): s for t, s in meta['spots'].items()})
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c} for t, m in meta['closes'].items() for d, c in m.items()])
    master = tmp_path / 'master'; master.mkdir()
    live = v2.build(chain.assign(date=pd.to_datetime(chain['date']), expiry=pd.to_datetime(chain['expiry'])),
                    ['SPY', 'AAPL', 'XOM'], day, master, px)
    for t in ('SPY', 'AAPL', 'XOM'):
        brow = built[built.ticker == t].iloc[0]
        for k in SHARED:
            assert _same(live[t][k], brow[k]), (t, k, live[t][k], brow[k])


def test_panel_row_equals_series_features_on_the_same_history(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE_PATH', raising=False)
    from execution import options_aux_v2 as v2
    crof = _script('compute_rolling_options_fields')
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    day = pd.Timestamp('2026-09-03')
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c} for t, m in meta['closes'].items() for d, c in m.items()])
    hist = pd.DataFrame([{'ticker': t, 'date': d.date(), 'iv30': 0.20 + 0.002 * i, 'pc_ratio': 1.0 + 0.01 * i,
                          'options_features_version': 2}
                         for t in ('SPY', 'AAPL', 'XOM') for i, d in enumerate(pd.bdate_range('2026-07-20', '2026-09-02'))])
    master = tmp_path / 'master'; master.mkdir()
    hist.to_parquet(master / 'options_surface.parquet', index=False)
    live = v2.build(chain.assign(date=pd.to_datetime(chain['date']), expiry=pd.to_datetime(chain['expiry'])),
                    ['SPY', 'AAPL', 'XOM'], day, master, px)
    surf = pd.concat([hist, pd.DataFrame([{'ticker': t, 'date': day.date(), 'iv30': live[t]['iv30'],
                                           'pc_ratio': live[t]['pc_ratio'], 'options_features_version': 2}
                                          for t in live])], ignore_index=True)
    panel = crof.build_panel(surf, px)
    for t in live:
        prow = panel[(panel.ticker == t) & (panel.date == day)].iloc[0]
        assert live[t]['iv_rank'] == pytest.approx(prow['iv_rank'])
        assert live[t]['rv_20'] == pytest.approx(prow['rv_20'])
        assert live[t]['vrp'] == pytest.approx(prow['vrp'])
        assert live[t]['iv_rank_history'] == pytest.approx(list(prow['iv_rank_history']))
