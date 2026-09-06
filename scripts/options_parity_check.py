"""SP-4 Phase 0 parity check + VRP calibration.

AUTHORITATIVE GATE (run_iv_gate): IV-MAE of the engine's VIX-anchored synthetic
IV vs real options_eod implied_volatility, on the SUPPORTED index/ETF underlyings
(VALID_OPTION_UNDERLYINGS), ATM ~30d envelope. The engine is trusted for option
strategies on these underlyings when IV-MAE <= IV_MAE_GATE (0.15). This is sound
because the engine uses VIX-anchored IV for these names, which directly tracks
real implied vol across regimes.

2026-09-06: with the surface tier ON by default (spec 2026-09-06 B.3, ruling G8)
the IV gate compares surface to chain on the overlap window and is expected to
be near 0 there; run it with OPENCLAW_OPTIONS_SURFACE_PATH pointed at an empty
path to measure the vix_term tier.

DIAGNOSTIC (run — price-level sweep): price-level MAE from the BS pricer swept
over a (vrp_factor, window) grid. Carries a documented ~22% structural floor from
data we don't have: no option-snapshot underlying, no dividends, American≠European,
small-cap quote spreads. This is NOT the gate; it is a diagnostic only.
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import numpy as np, pandas as pd, pyarrow.parquet as pq

from backtest.options_pricing import bs_price
from backtest.synthetic_iv import realized_vol

MAE_THRESHOLD = 0.15  # PROPOSED gate; record the measured value, revise only with justification

IV_MAE_GATE = 0.15  # authoritative gate: VIX-anchored synthetic IV vs real IV on supported envelope


def iv_mae(synth_iv: list[float], real_iv: list[float]) -> float:
    s = np.asarray(synth_iv, float); r = np.asarray(real_iv, float)
    mask = r > 1e-6
    return float(np.mean(np.abs(s[mask] - r[mask]) / r[mask]))


def run_iv_gate(dte_lo=20, dte_hi=45, atm_band=0.05):
    """AUTHORITATIVE Phase 0 gate: IV-MAE of the engine's VIX-anchored synthetic IV
    vs real options_eod IV, on the SUPPORTED index/ETF underlyings, ATM (~30d).
    The engine is trusted for option strategies on these underlyings only."""
    import pandas as pd
    from backtest.synthetic_iv import synthetic_iv
    from backtest.vol_index import VALID_OPTION_UNDERLYINGS
    closes = _load_underlying_closes()
    op = pq.read_table('data/master/options_eod.parquet',
                       columns=['ticker','date','expiry','strike','option_type','implied_volatility']).to_pandas()
    op = op.dropna(subset=['implied_volatility'])
    op = op[op['implied_volatility'] > 0.01]
    op['date'] = pd.to_datetime(op['date']); op['expiry'] = pd.to_datetime(op['expiry'])
    op['dte'] = (op['expiry'] - op['date']).dt.days
    pooled_s, pooled_r = [], []
    for tk in sorted(VALID_OPTION_UNDERLYINGS):
        s = closes.get(tk)
        if s is None:
            continue
        d = op[(op['ticker'] == tk) & (op['dte'].between(dte_lo, dte_hi))]
        syn, real = [], []
        for _, row in d.iterrows():
            h = s.loc[:row['date']]
            if len(h) < 22:
                continue
            if abs(float(row['strike']) / float(h.iloc[-1]) - 1) < atm_band:
                syn.append(synthetic_iv(h, underlying=tk, as_of=row['date']))
                real.append(float(row['implied_volatility']))
        if len(syn) >= 20:
            print(f'  {tk:6s} n={len(syn):4d} IV-MAE={iv_mae(syn, real):.4f}')
            pooled_s += syn; pooled_r += real
    if len(pooled_s) < 50:
        print('IV-GATE: INSUFFICIENT DATA'); return 2
    m = iv_mae(pooled_s, pooled_r)
    status = 'PASS' if m <= IV_MAE_GATE else 'FAIL'
    print(f'POOLED index/ETF-ATM IV-MAE={m:.4f} vs gate {IV_MAE_GATE} -> {status}')
    return 0 if status == 'PASS' else 1


def mae_fraction(synth: list[float], real: list[float]) -> float:
    s = np.asarray(synth, float); r = np.asarray(real, float)
    mask = r > 1e-6
    return float(np.mean(np.abs(s[mask] - r[mask]) / r[mask]))


def _load_underlying_closes():
    df = pq.read_table('data/master/prices.parquet', columns=['ticker', 'date', 'close']).to_pandas()
    df['date'] = pd.to_datetime(df['date'])
    return {t: g.set_index('date')['close'].sort_index() for t, g in df.groupby('ticker')}


def synth_price_for_row(row, closes, vrp_factor, window):
    s = closes.get(row['ticker'])
    if s is None:
        return None
    asof = pd.Timestamp(row['date'])
    hist = s.loc[:asof]
    if len(hist) < 5:
        return None
    S = float(hist.iloc[-1])
    sigma = max(0.05, realized_vol(hist, window=window) * vrp_factor)
    t = max((pd.Timestamp(row['expiry']) - asof).days / 365.0, 1e-6)
    flag = 'c' if str(row['option_type']).lower().startswith('c') else 'p'
    try:
        return bs_price(flag, S, float(row['strike']), t, sigma)
    except Exception:
        return None


def run(sample=4000):
    opt = pq.read_table('data/master/options_eod.parquet',
                        columns=['ticker', 'date', 'expiry', 'strike', 'option_type',
                                 'market_price', 'implied_volatility']).to_pandas()
    opt = opt.dropna(subset=['market_price', 'strike', 'expiry'])
    opt = opt[opt['market_price'] > 0.05]
    if len(opt) > sample:
        opt = opt.sample(sample, random_state=0)
    closes = _load_underlying_closes()

    best = None
    for vrp in [1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4]:
        for window in [10, 21, 42]:
            synth, real = [], []
            for _, row in opt.iterrows():
                p = synth_price_for_row(row, closes, vrp, window)
                if p is not None:
                    synth.append(p); real.append(float(row['market_price']))
            if len(synth) < 100:
                continue
            m = mae_fraction(synth, real)
            if best is None or m < best['mae']:
                best = {'vrp_factor': vrp, 'window': window, 'mae': m, 'n': len(synth)}
            print(f'vrp={vrp} window={window} n={len(synth)} MAE={m:.4f}')

    print('\nBEST:', best)
    if best is None:
        print('PARITY: INSUFFICIENT DATA'); return 2
    status = 'PASS' if best['mae'] <= MAE_THRESHOLD else 'FAIL'
    print(f"PARITY {status} (MAE={best['mae']:.4f} vs threshold {MAE_THRESHOLD})")
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    print('=== AUTHORITATIVE GATE: IV-MAE (VIX-anchored synthetic IV vs real IV, supported index/ETF ATM) ===')
    rc = run_iv_gate()
    print('\n=== DIAGNOSTIC: price-level MAE (carries ~22% structural floor: no snapshot underlying / dividends / American / quote spreads — NOT the gate) ===')
    run()
    raise SystemExit(rc)
