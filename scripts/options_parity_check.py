"""SP-4 Phase 0 parity check + VRP calibration.

Over the ~7-week real-chain overlap (options_eod.parquet), for a sample of
real (ticker, date, expiry, strike, option_type) contracts, compute the
synthetic BS price using synthetic_iv and compare to the real market_price.
Reports mean-absolute-error-as-fraction-of-price, swept over a grid of
(vrp_factor, window) to find the value that minimizes MAE.

CALIBRATION GATE: the engine is "trusted" only if best-fit MAE <= MAE_THRESHOLD.
Record the measured best (vrp_factor, window, MAE) — DO NOT hardcode an
optimistic number. The chosen vrp_factor becomes synthetic_iv.DEFAULT_VRP_FACTOR
(applied in Task 11) and threshold calibration may not proceed until this passes.
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
    raise SystemExit(run())
