#!/usr/bin/env python3
"""Rebuild data/master/options_aggregates/<YYYY-MM>.parquet from options_eod.parquet.

WHY THIS EXISTS (2026-07-29): the monthly aggregates feed
`compute_rolling_options_fields.py` -> options_aggregates_enriched.parquet ->
`aux_data_loader._day_slice`, which is the BACKTEST's only source of options
data. The collector path that once produced them stopped running: the newest
file is 2026-04 and the enriched panel ends 2026-04-22. Because `_day_slice`
falls back to "most recent prior date", every backtest bar after 2026-04-22
has been silently served the 2026-04-22 options slice — a classic split-source
failure (live reads options_eod.parquet directly and IS fresh; only the
backtest is stale). 17 strategies declare options_eod.

Definitions mirror `execution/engine.py:load_aux_data`'s per-ticker chain math
(the live parity target) evaluated AS OF each historical date rather than
today, so the backtest panel and the live aux dict are computed the same way
from the same master.

SOURCE BOUNDARY (measured 2026-07-29): options_eod.parquet covers
2026-04-08 -> present and is only dense from 2026-06 (June 1.0M rows, July
17.0M; April 18 rows, May 6k). The pre-2026-04 monthly aggregates came from a
RETIRED provider whose raw rows no longer exist here — they cannot be rebuilt
and are left untouched. This script therefore repairs the recent gap only;
`--validate-month` against a pre-April month compares two different sources
and is not a definitional test.

Master-data safety: writes ONLY to data/master/options_aggregates/ (a derived
directory), atomically (tmp + os.replace), one file per month. Never mutates
options_eod.parquet, prices.parquet, or any other master.

Usage:
  python3 scripts/build_options_aggregates.py --validate-month 2026-04
  python3 scripts/build_options_aggregates.py --start 2026-04-23 --end 2026-07-29
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
OPTS_PATH = ROOT / 'data' / 'master' / 'options_eod.parquet'
OUT_DIR = ROOT / 'data' / 'master' / 'options_aggregates'

# Only the columns the aggregate math touches (options_eod has 28).
COLS = ['ticker', 'date', 'expiry', 'strike', 'option_type', 'open_interest',
        'implied_volatility', 'delta', 'gamma', 'theta', 'vega', 'volume', 'close']

# Schema of the surviving monthly files, in order.
OUT_COLS = ['ticker', 'iv_front', 'dte_front', 'iv_back', 'dte_back', 'term_slope',
            'otm_put_iv', 'otm_call_iv', 'skew', 'call_volume', 'put_volume',
            'put_call_vol_ratio', 'contracts_liquid', 'spot', 'gamma_atm',
            'theta_atm', 'gex', 'iv_centroid_delta', 'surface_premium', 'date']

FRONT_DTE_MAX = 45          # engine: nearest expiry with dte <= 45
ATM_DELTA = (0.40, 0.60)    # engine: gamma_atm / theta_atm / term-structure ATM band
OTM_PUT_DELTA = (-0.25, -0.15)   # engine skew_20d: 20-delta put
OTM_CALL_DELTA = (0.15, 0.25)    # symmetric 20-delta call (aggregate carries both legs)
CHUNK_DAYS = 5                   # read window per pass (July 2026 = 17M rows/month)


def _read_range(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Filtered, column-pruned read — never full-loads the 18M-row parquet."""
    flt = (pc.field('date') >= pc.scalar(start.to_pydatetime())) & \
          (pc.field('date') <= pc.scalar(end.to_pydatetime()))
    try:
        tbl = pq.read_table(OPTS_PATH, columns=COLS, filters=flt)
    except Exception:
        # date stored as string in some vintages — fall back to string bounds.
        flt = (pc.field('date') >= pc.scalar(start.strftime('%Y-%m-%d'))) & \
              (pc.field('date') <= pc.scalar(end.strftime('%Y-%m-%d')))
        tbl = pq.read_table(OPTS_PATH, columns=COLS, filters=flt)
    df = tbl.to_pandas()
    del tbl
    if df.empty:
        return df
    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    df['expiry'] = pd.to_datetime(df['expiry'], errors='coerce')
    df = df.dropna(subset=['date', 'expiry', 'ticker'])
    df['option_type'] = df['option_type'].astype(str).str.upper()
    # SP-1 parity: drop degenerate all-zero-greek rows before aggregating.
    greeks = ['delta', 'gamma', 'theta', 'vega']
    have = [c for c in greeks if c in df.columns]
    if have:
        df = df[~(df[have].fillna(0) == 0).all(axis=1)]
    return df


def _mean(series) -> float | None:
    s = pd.to_numeric(series, errors='coerce').dropna()
    return float(s.mean()) if not s.empty else None


def _aggregate_day(grp: pd.DataFrame, as_of: pd.Timestamp) -> dict | None:
    """One (ticker, date) row, mirroring the live engine's chain math."""
    future = grp[grp['expiry'] >= as_of]
    if future.empty:
        return None
    dte = (future['expiry'] - as_of).dt.days
    future = future.assign(_dte=dte)

    near = future[future['_dte'] <= FRONT_DTE_MAX]
    if near.empty:
        near = future
    front_exp = near['_dte'].min()
    chain = near[near['_dte'] == front_exp]

    later = future[future['_dte'] > front_exp]
    back = later[later['_dte'] == later['_dte'].min()] if not later.empty else None

    iv_front = _mean(chain['implied_volatility'])
    iv_back = _mean(back['implied_volatility']) if back is not None else None
    dte_back = float(back['_dte'].iloc[0]) if back is not None and not back.empty else None

    calls = chain[chain['option_type'] == 'CALL']
    puts = chain[chain['option_type'] == 'PUT']
    otm_put_iv = _mean(puts[puts['delta'].between(*OTM_PUT_DELTA)]['implied_volatility'])
    otm_call_iv = _mean(calls[calls['delta'].between(*OTM_CALL_DELTA)]['implied_volatility'])

    call_volume = float(pd.to_numeric(calls['volume'], errors='coerce').fillna(0).sum())
    put_volume = float(pd.to_numeric(puts['volume'], errors='coerce').fillna(0).sum())

    # OPEN INTEREST IS 100% NULL in the current provider's feed (measured
    # 2026-07-29 on options_eod 2026-07-27..28). Every OI-weighted field must
    # therefore report UNKNOWN (None), never a computed zero: a gex of 0.0
    # asserts "no dealer gamma imbalance", which is a fabricated fact, while
    # None lets a consumer skip. (The live engine computes these the same way
    # and currently emits the same false zeros — operator follow-up.)
    oi_raw = pd.to_numeric(chain['open_interest'], errors='coerce')
    has_oi = bool(oi_raw.notna().any() and (oi_raw.fillna(0) > 0).any())
    oi = oi_raw.fillna(0)
    contracts_liquid = int((oi > 0).sum()) if has_oi else None

    spot = _mean(chain['close']) if 'close' in chain.columns else None

    atm = chain[chain['delta'].abs().between(*ATM_DELTA)]
    gamma_atm = _mean(atm['gamma'])
    theta_atm = _mean(atm['theta'])

    if has_oi:
        gc = float((pd.to_numeric(calls['gamma'], errors='coerce').fillna(0) *
                    pd.to_numeric(calls['open_interest'], errors='coerce').fillna(0)).sum())
        gp = float((pd.to_numeric(puts['gamma'], errors='coerce').fillna(0) *
                    pd.to_numeric(puts['open_interest'], errors='coerce').fillna(0)).sum())
        gex = round((gc - gp) * 100, 2)
    else:
        gex = None

    iv_centroid_delta = surface_premium = None
    w = (pd.to_numeric(chain['vega'], errors='coerce').abs().fillna(0) * oi)
    tw = float(w.sum())
    if has_oi and tw > 0:
        d = pd.to_numeric(chain['delta'], errors='coerce').fillna(0)
        iv = pd.to_numeric(chain['implied_volatility'], errors='coerce').fillna(0)
        iv_centroid_delta = round(float((d * w).sum() / tw), 4)
        vwiv = float((iv * w).sum() / tw)
        atm5 = chain[chain['delta'].abs().between(0.45, 0.55)]
        atm_iv5 = _mean(atm5['implied_volatility'])
        surface_premium = round(vwiv - (atm_iv5 if atm_iv5 is not None else vwiv), 4)

    return {
        'ticker': grp['ticker'].iloc[0],
        'iv_front': iv_front,
        'dte_front': int(front_exp),
        'iv_back': iv_back,
        'dte_back': dte_back,
        'term_slope': (iv_back - iv_front) if (iv_back is not None and iv_front is not None) else None,
        'otm_put_iv': otm_put_iv,
        'otm_call_iv': otm_call_iv,
        'skew': (otm_put_iv - otm_call_iv) if (otm_put_iv is not None and otm_call_iv is not None) else None,
        'call_volume': call_volume,
        'put_volume': put_volume,
        'put_call_vol_ratio': (put_volume / call_volume) if call_volume > 0 else None,
        'contracts_liquid': contracts_liquid,
        'spot': spot,
        'gamma_atm': gamma_atm,
        'theta_atm': theta_atm,
        'gex': gex,
        'iv_centroid_delta': iv_centroid_delta,
        'surface_premium': surface_premium,
        'date': as_of,
    }


def build_frame(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (ticker, as_of), grp in df.groupby(['ticker', 'date'], sort=True):
        row = _aggregate_day(grp, as_of)
        if row is not None:
            rows.append(row)
    out = pd.DataFrame(rows, columns=OUT_COLS)
    return out.sort_values(['date', 'ticker']).reset_index(drop=True)


def _month_bounds(month: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(month + '-01')
    return start, start + pd.offsets.MonthEnd(1)


def validate_month(month: str) -> int:
    """Rebuild an existing month and report per-field agreement — the only way
    to pin definitions without the original builder's source."""
    ref_path = OUT_DIR / f'{month}.parquet'
    if not ref_path.exists():
        print(f'no reference file {ref_path}', file=sys.stderr)
        return 2
    start, end = _month_bounds(month)
    df = _read_range(start, end)
    if df.empty:
        print('no options_eod rows in range', file=sys.stderr)
        return 2
    built = build_frame(df)
    ref = pd.read_parquet(ref_path)
    ref['date'] = pd.to_datetime(ref['date'])
    key = ['ticker', 'date']
    m = ref.merge(built, on=key, suffixes=('_ref', '_new'), how='inner')
    print(f'{month}: ref rows={len(ref)} built rows={len(built)} matched keys={len(m)}')
    for col in OUT_COLS:
        if col in key:
            continue
        a = pd.to_numeric(m.get(f'{col}_ref'), errors='coerce')
        b = pd.to_numeric(m.get(f'{col}_new'), errors='coerce')
        if a is None or b is None:
            continue
        both = a.notna() & b.notna()
        if both.sum() == 0:
            print(f'  {col:20s} no overlapping non-null values '
                  f'(ref nulls={a.isna().sum()}, new nulls={b.isna().sum()})')
            continue
        denom = a[both].abs().clip(lower=1e-9)
        rel = ((a[both] - b[both]).abs() / denom)
        print(f'  {col:20s} n={both.sum():6d}  median_rel_err={rel.median():.4f}  '
              f'p90={rel.quantile(0.9):.4f}  exact={(rel < 1e-6).mean():.1%}')
    return 0


def build_range(start: str, end: str) -> int:
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    months = pd.period_range(start_ts, end_ts, freq='M')
    total = 0
    for per in months:
        m_start = max(per.start_time, start_ts)
        m_end = min(per.end_time.normalize(), end_ts)
        # Day-chunked: July 2026 alone is 17M rows — reading a whole month
        # would be a multi-GB transient on the 8GB box. Aggregate rows are
        # tiny (one per ticker/date), so accumulating chunk outputs is cheap.
        parts = []
        chunk_start = m_start
        while chunk_start <= m_end:
            chunk_end = min(chunk_start + pd.Timedelta(days=CHUNK_DAYS - 1), m_end)
            chunk = _read_range(chunk_start, chunk_end)
            if not chunk.empty:
                parts.append(build_frame(chunk))
            del chunk
            chunk_start = chunk_end + pd.Timedelta(days=1)
        if not parts:
            print(f'{per}: no rows in {m_start.date()}..{m_end.date()} — skipped')
            continue
        built = pd.concat(parts, ignore_index=True)
        del parts
        if built.empty:
            print(f'{per}: aggregation produced no rows — skipped')
            continue
        out_path = OUT_DIR / f'{per}.parquet'
        if out_path.exists():
            # Append-only within the month: keep pre-existing dates, add new ones.
            prev = pd.read_parquet(out_path)
            prev['date'] = pd.to_datetime(prev['date'])
            keep = prev[~prev['date'].isin(built['date'].unique())]
            built = pd.concat([keep, built], ignore_index=True)
            built = built.sort_values(['date', 'ticker']).reset_index(drop=True)
        tmp = out_path.with_suffix('.parquet.tmp')
        built.to_parquet(tmp, index=False)
        os.replace(tmp, out_path)
        n_dates = built['date'].nunique()
        print(f'{per}: wrote {len(built)} rows / {n_dates} dates / '
              f'{built["ticker"].nunique()} tickers -> {out_path.name}')
        total += len(built)
    print(f'done: {total} aggregate rows')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--validate-month', help='YYYY-MM: rebuild and compare, no writes')
    ap.add_argument('--start')
    ap.add_argument('--end')
    args = ap.parse_args()
    if args.validate_month:
        return validate_month(args.validate_month)
    if not (args.start and args.end):
        ap.error('--start and --end required (or --validate-month)')
    return build_range(args.start, args.end)


if __name__ == '__main__':
    raise SystemExit(main())
