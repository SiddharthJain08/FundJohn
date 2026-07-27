#!/usr/bin/env python3
"""Build data/derived/ticker_cost_bps.json — per-ticker one-way execution cost
(half-spread + impact allowance, basis points) for the unified backtest's
honest cost model (2026-07-27).

Model:  half_bps = clamp( max( A + K_IMPACT / sqrt(ADV$M), TICK_HALF / price ),
                          MIN_BPS, MAX_BPS )

Calibration (2026-07-27): 146 tickers stratified across ADV 1e4..1e11, median
NBBO spread over 15:55-16:00 ET 2026-07-24 (the live 15:55 execution window),
robust fit half_bps = 2.72 + 5.46/sqrt(ADV$M), median |resid| 2.5bps. K is the
fitted 5.46 x1.5 impact allowance — quoted spread understates realized cost when
order size exceeds displayed depth, which is exactly the thin-name regime that
bled live. TICK_HALF = half a $0.01 tick as bps of price (a $1 stock cannot
quote tighter than 100bps full spread). One-way: the backtest applies this
adversely on entry AND exit, so round-trip = full spread + 2x impact.

ADV$ = median daily close*volume over the last ~252 calendar-covered bars.
Fewer than 20 bars -> excluded (falls back to the flat model at runtime).

Usage: python3 scripts/build_ticker_cost_model.py   (nice -n 19 recommended)
Rerun after large universe changes or quarterly; the artifact carries its
generation date and params for provenance.
"""
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / 'data' / 'master' / 'prices.parquet'
OUT = ROOT / 'data' / 'derived' / 'ticker_cost_bps.json'

A_BPS = 2.7          # fitted intercept (exchange floor / liquid-name half-spread)
K_IMPACT = 8.2       # fitted 5.46 x 1.5 impact allowance on the liquidity term
MIN_BPS = 1.5
MAX_BPS = 150.0
WINDOW_BARS = 252
MIN_BARS = 20


def main() -> int:
    tbl = pq.read_table(PRICES, columns=['ticker', 'date', 'close', 'volume'],
                        read_dictionary=['ticker', 'date'])
    p = tbl.to_pandas()
    del tbl
    p['date'] = p['date'].astype(str)
    last = p['date'].max()
    cutoff = (dt.date.fromisoformat(last[:10]) - dt.timedelta(days=365)).isoformat()
    p = p[p['date'] >= cutoff]
    p['close'] = pd.to_numeric(p['close'], errors='coerce')
    p['volume'] = pd.to_numeric(p['volume'], errors='coerce')
    p = p.dropna(subset=['close', 'volume'])
    p = p[(p['close'] > 0) & (p['volume'] >= 0)]
    p['dv'] = p['close'] * p['volume']
    g = p.groupby(p['ticker'].astype(str), observed=True).agg(
        adv_usd=('dv', 'median'), med_close=('close', 'median'), bars=('dv', 'size'))
    g = g[g['bars'] >= MIN_BARS]

    cost = {}
    adv_out = {}
    px_out = {}
    for t, row in g.iterrows():
        if t.startswith('^') or '-USD' in t or '=F' in t:
            continue
        adv_m = max(row['adv_usd'], 1.0) / 1e6
        curve = A_BPS + K_IMPACT / math.sqrt(adv_m)
        tick_floor = 0.005 / row['med_close'] * 1e4 if row['med_close'] > 0 else 0.0
        bps = min(max(max(curve, tick_floor), MIN_BPS), MAX_BPS)
        cost[t] = round(bps, 2)
        adv_out[t] = round(float(row['adv_usd']))
        px_out[t] = round(float(row['med_close']), 4)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    art = {
        'generated_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'prices_last_date': last[:10],
        'window_bars': WINDOW_BARS,
        'params': {'a_bps': A_BPS, 'k_impact': K_IMPACT, 'min_bps': MIN_BPS,
                   'max_bps': MAX_BPS, 'tick_half_usd': 0.005,
                   'calibration': '2026-07-27 NBBO 15:55ET fit, 146 tickers'},
        'cost_bps': cost,
        # Liquidity-gate inputs (fix 5, 2026-07-27): median dollar ADV + median
        # close over the same window — consumed by the backtest asset gate and
        # the live sizer's entry-hygiene gate (min price / min ADV /
        # participation cap) so both sides share one liquidity truth.
        'adv_usd': adv_out,
        'med_close': px_out,
    }
    tmp = OUT.with_suffix('.json.tmp')
    with open(tmp, 'w') as f:
        json.dump(art, f, separators=(',', ':'))
    os.replace(tmp, OUT)
    vals = sorted(cost.values())
    print(f'[build_ticker_cost_model] wrote {len(cost)} tickers -> {OUT}')
    print(f'  bps: min={vals[0]} p25={vals[len(vals)//4]} med={vals[len(vals)//2]} '
          f'p75={vals[3*len(vals)//4]} max={vals[-1]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
