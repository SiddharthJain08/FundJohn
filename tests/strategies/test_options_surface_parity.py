# tests/strategies/test_options_surface_parity.py
from __future__ import annotations
import importlib.util, json, math
from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'
SHARED = ['iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d', 'ts_ratio',
          'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm', 'call_volume', 'put_volume', 'volume',
          'pc_ratio', 'expiry_date', 'n_expiries_fit', 'n_strikes_30d', 'options_features_version',
          'mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d',
          'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d',
          'iv30_source', 'n_expiries_atm']


def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / f'{name}.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _thin_rows(as_of='2026-09-03', spot=100.0):
    """Synthetic thin chain (amendment 2026-09-06 §H): 3 strikes at 14 and 42
    DTE — below MIN_STRIKES, so both expiries fall through to atm_band_fit.
    Exercises the parity check on a band-sourced iv30/iv30_source/n_expiries_atm."""
    out = []
    for dte, iv in ((14, 0.30), (42, 0.34)):
        t = dte / 365
        exp = (pd.Timestamp(as_of) + pd.Timedelta(days=dte)).date()
        for K in (95.0, 100.0, 105.0):
            d1 = (math.log(spot / K) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
            dc = float(norm.cdf(d1))
            for flag, d in (('CALL', dc), ('PUT', dc - 1.0)):
                out.append({'ticker': 'THIN', 'date': as_of, 'expiry': exp, 'strike': float(K),
                            'option_type': flag, 'implied_volatility': iv, 'delta': d,
                            'gamma': 0.01, 'theta': -0.02, 'vega': 0.1, 'volume': 1.0})
    return out


def _isnull(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _same(a, b):
    # A band-only fit (amendment 2026-09-06 §H) legitimately leaves smile-only
    # keys None on the live dict; the builder's DataFrame upcasts that same
    # None to NaN in a float64 column — both sides must compare equal.
    if _isnull(a) or _isnull(b):
        return _isnull(a) and _isnull(b)
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
    # amendment 2026-09-06 §H (fix round 1): exercise a band fit in the parity
    # check, not just the smile-only tickers.
    chain = pd.concat([chain, pd.DataFrame(_thin_rows(as_of='2026-09-03', spot=100.0))], ignore_index=True)
    spots = {(t, day): s for t, s in meta['spots'].items()}
    spots[('THIN', day)] = 100.0
    built = bos.build_rows(chain.assign(date=pd.to_datetime(chain['date'])), spots)
    thin_dates = sorted(next(iter(meta['closes'].values())).keys())
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c} for t, m in meta['closes'].items() for d, c in m.items()]
                      + [{'ticker': 'THIN', 'date': pd.Timestamp(d), 'close': 100.0} for d in thin_dates])
    master = tmp_path / 'master'; master.mkdir()
    live = v2.build(chain.assign(date=pd.to_datetime(chain['date']), expiry=pd.to_datetime(chain['expiry'])),
                    ['SPY', 'AAPL', 'XOM', 'THIN'], day, master, px)
    for t in ('SPY', 'AAPL', 'XOM', 'THIN'):
        brow = built[built.ticker == t].iloc[0]
        for k in SHARED:
            assert _same(live[t][k], brow[k]), (t, k, live[t][k], brow[k])
    assert live['THIN']['iv30_source'] == 'atm_band'
    assert live['THIN']['n_expiries_atm'] == 2


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
