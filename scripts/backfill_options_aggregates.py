#!/usr/bin/env python3
"""
Backfill per-ticker per-day options aggregates from Massive OPRA flat files.

For each trading date:
  1. Download the day's full OPRA OHLCV flat file (~4 MB gzipped).
  2. Parse OCC symbols to (underlying, expiry, strike, type).
  3. Join with spot prices from data/master/prices.parquet.
  4. Filter to liquid contracts with realistic prices / expiries.
  5. Solve Black-Scholes implied volatility for every surviving contract.
  6. Reduce to one row per (underlying_ticker, date) with:
        iv_front, iv_back, term_slope,
        otm_put_iv, otm_call_iv, skew,
        atm_iv_front, call_volume, put_volume, call_oi, put_oi,
        put_call_vol_ratio, contracts_liquid
  7. Append to data/master/options_aggregates.parquet (one partition per month).

Resumable: writes each month-partition atomically; on restart, skips months that
already exist AND skips dates already present in the current partition.

Usage:
    python3 scripts/backfill_options_aggregates.py --start 2022-01-03 --end 2026-04-20
    python3 scripts/backfill_options_aggregates.py --one-day 2026-04-17   # smoke test
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src' / 'ingestion'))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')

from massive_client import download_options_day_bars, list_available_dates  # noqa: E402
from py_vollib_vectorized import (  # noqa: E402
    vectorized_implied_volatility, vectorized_black_scholes_merton,
)
from py_vollib_vectorized.greeks import gamma as vec_gamma, theta as vec_theta  # noqa: E402

PRICES_PATH    = ROOT / 'data' / 'master' / 'prices.parquet'
OUTPUT_DIR     = ROOT / 'data' / 'master' / 'options_aggregates'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

RISK_FREE = 0.045    # flat-rate assumption; fine for aggregate-level signal, not precision pricing
DIVIDEND  = 0.0

OCC_RE = re.compile(r'^O:([A-Z0-9.]{1,6})(\d{6})([CP])(\d{8})$')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [backfill_options_aggregates] %(message)s',
)
log = logging.getLogger(__name__)


def parse_occ(sym: pd.Series) -> pd.DataFrame:
    m = sym.str.extract(OCC_RE)
    m.columns = ['underlying_ticker', 'expiry_raw', 'cp', 'strike_raw']
    m['expiration_date'] = pd.to_datetime('20' + m['expiry_raw'], format='%Y%m%d', errors='coerce')
    m['strike_price'] = pd.to_numeric(m['strike_raw'], errors='coerce') / 1000.0
    m['contract_type'] = m['cp'].map({'C': 'call', 'P': 'put'})
    return m[['underlying_ticker', 'expiration_date', 'strike_price', 'contract_type']]


def compute_day_aggregates(trade_date: str, spot_map: dict[str, float]) -> pd.DataFrame:
    """Download one day and reduce to per-ticker aggregate rows.

    Returns an empty DataFrame on failure (so the loop keeps going).
    """
    t0 = time.time()
    df = download_options_day_bars(trade_date)
    if df.empty:
        log.warning('%s: no flat file', trade_date)
        return pd.DataFrame()

    # Parse OCC symbols
    parsed = parse_occ(df['ticker'])
    df = pd.concat([df.reset_index(drop=True), parsed.reset_index(drop=True)], axis=1)
    df = df.dropna(subset=['underlying_ticker', 'expiration_date', 'strike_price', 'contract_type'])
    if df.empty:
        return pd.DataFrame()

    # Join spot
    df['spot'] = df['underlying_ticker'].map(spot_map)
    df = df[df['spot'].notna()]

    # Filter contracts: liquid + DTE in [7, 180] + priced.
    # DTE<7 means near-expiry contracts where BS IV solve is unstable
    # (time decay dominates; close ≈ intrinsic). DTE>180 is less used by the
    # HV-series strategies we're targeting.
    trade_ts = pd.to_datetime(trade_date)
    df['days_to_exp'] = (df['expiration_date'] - trade_ts).dt.days
    df = df[(df['days_to_exp'] >= 7) & (df['days_to_exp'] <= 180)]
    df['T'] = df['days_to_exp'] / 365.0
    df = df[df['close'] > 0.05]
    df = df[(df['volume'].fillna(0) > 0) | (df['transactions'].fillna(0) > 0)]
    if df.empty:
        return pd.DataFrame()

    # Solve IV vectorized
    df['flag'] = df['contract_type'].map({'call': 'c', 'put': 'p'})
    try:
        iv = vectorized_implied_volatility(
            price=df['close'].to_numpy(dtype=float),
            S=df['spot'].to_numpy(dtype=float),
            K=df['strike_price'].to_numpy(dtype=float),
            t=df['T'].to_numpy(dtype=float),
            r=RISK_FREE, flag=df['flag'].tolist(),
            q=DIVIDEND, return_as='numpy', on_error='ignore',
        )
    except Exception as e:
        log.warning('%s: IV solver failed: %s', trade_date, e)
        return pd.DataFrame()
    df['iv'] = iv
    df = df[(df['iv'] > 0.01) & (df['iv'] < 5.0)]   # prune unsolved / nonsense
    if df.empty:
        return pd.DataFrame()

    # Compute BS gamma & theta per contract — needed for gamma_atm, theta_atm, gex aggregates.
    try:
        df['gamma'] = vec_gamma(
            flag=df['flag'].tolist(),
            S=df['spot'].to_numpy(dtype=float),
            K=df['strike_price'].to_numpy(dtype=float),
            t=df['T'].to_numpy(dtype=float),
            r=RISK_FREE,
            sigma=df['iv'].to_numpy(dtype=float),
            q=DIVIDEND, return_as='numpy',
        )
        df['theta'] = vec_theta(
            flag=df['flag'].tolist(),
            S=df['spot'].to_numpy(dtype=float),
            K=df['strike_price'].to_numpy(dtype=float),
            t=df['T'].to_numpy(dtype=float),
            r=RISK_FREE,
            sigma=df['iv'].to_numpy(dtype=float),
            q=DIVIDEND, return_as='numpy',
        )
    except Exception as e:
        log.warning('%s: Greeks solve failed: %s', trade_date, e)
        df['gamma'] = np.nan
        df['theta'] = np.nan

    df['moneyness'] = df['strike_price'] / df['spot']

    # ── Per (ticker, expiry) ATM IV ─────────────────────────────────────────
    # "ATM" = strike closest to spot. Require near-ATM (moneyness in [0.9, 1.1])
    # so the quote is sensible; otherwise BS inversion is flaky on deep ITM/OTM.
    atm_pool = df[df['moneyness'].between(0.90, 1.10)].copy()
    atm_pool['m_abs'] = (atm_pool['moneyness'] - 1.0).abs()
    atm = (atm_pool.sort_values(['underlying_ticker', 'expiration_date', 'm_abs'])
                   .drop_duplicates(subset=['underlying_ticker', 'expiration_date']))

    # Front = first expiry with DTE in [14, 45] (monthly-ish zone)
    # Back  = first expiry with DTE in [46, 120]
    def pick(bucket_df, dte_lo, dte_hi, col_prefix):
        b = bucket_df[(bucket_df['days_to_exp'] >= dte_lo) & (bucket_df['days_to_exp'] <= dte_hi)]
        b = b.sort_values(['underlying_ticker', 'days_to_exp'])
        b = b.drop_duplicates(subset=['underlying_ticker'], keep='first')
        return b.set_index('underlying_ticker')[['iv', 'days_to_exp']].rename(
            columns={'iv': f'iv_{col_prefix}', 'days_to_exp': f'dte_{col_prefix}'})

    front = pick(atm, 14, 45,  'front')
    back  = pick(atm, 46, 120, 'back')

    agg = front.join(back, how='left')
    agg['term_slope'] = agg['iv_back'] - agg['iv_front']

    # ── Front-expiry skew (OTM put IV – OTM call IV) ────────────────────────
    # Use the front-expiry DTE we picked for iv_front so skew is measured
    # on the same time-slice (14–45 DTE zone), not on 1-DTE weeklies.
    front_exp_per_ticker = (
        atm[(atm['days_to_exp'] >= 14) & (atm['days_to_exp'] <= 45)]
            .sort_values(['underlying_ticker', 'days_to_exp'])
            .drop_duplicates(subset=['underlying_ticker'], keep='first')
            .set_index('underlying_ticker')['expiration_date']
    )
    fe = df.merge(front_exp_per_ticker.rename('_fe'), left_on='underlying_ticker', right_index=True)
    fe = fe[fe['expiration_date'] == fe['_fe']]
    otm_put  = fe[(fe['flag'] == 'p') & fe['moneyness'].between(0.85, 0.97)].groupby('underlying_ticker')['iv'].mean().rename('otm_put_iv')
    otm_call = fe[(fe['flag'] == 'c') & fe['moneyness'].between(1.03, 1.15)].groupby('underlying_ticker')['iv'].mean().rename('otm_call_iv')
    agg = agg.join(otm_put, how='left').join(otm_call, how='left')
    agg['skew'] = agg['otm_put_iv'] - agg['otm_call_iv']

    # ── Volume / OI flow diagnostics ────────────────────────────────────────
    # OPRA day_aggs_v1 flat files don't include open_interest, only OHLCV + transactions.
    # Put/call volume ratio is still useful as a flow proxy.
    vol_pivot = fe.groupby(['underlying_ticker', 'flag'])['volume'].sum().unstack(fill_value=0)
    vol_pivot = vol_pivot.rename(columns={'c': 'call_volume', 'p': 'put_volume'})
    for col in ('call_volume', 'put_volume'):
        if col not in vol_pivot.columns:
            vol_pivot[col] = 0
    agg = agg.join(vol_pivot[['call_volume', 'put_volume']], how='left')
    agg['put_call_vol_ratio'] = (agg['put_volume'].fillna(0) /
                                 agg['call_volume'].replace(0, np.nan))

    # Contract liquidity / spot
    agg['contracts_liquid'] = fe.groupby('underlying_ticker').size()
    agg['spot'] = pd.Series(spot_map)

    # ── Greeks aggregates (ATM, front expiry) ──────────────────────────────
    # gamma_atm / theta_atm = mean γ, θ across contracts within moneyness
    # band [0.97, 1.03] of the front expiry. These are the numbers HV8 reads.
    atm_front = fe[fe['moneyness'].between(0.97, 1.03) & fe['gamma'].notna()]
    gamma_atm = atm_front.groupby('underlying_ticker')['gamma'].mean().rename('gamma_atm')
    theta_atm = atm_front.groupby('underlying_ticker')['theta'].mean().rename('theta_atm')
    agg = agg.join(gamma_atm, how='left').join(theta_atm, how='left')

    # gex (gamma-exposure proxy) — Σ(γ × volume × 100 × spot²) across the full
    # liquid chain per ticker. Sign convention: positive = dealers long gamma
    # (stabilizing); here we use call−put gamma volume since dealer-hedge flow
    # direction isn't in the data. Keep as absolute proxy.
    df['gex_contrib'] = df['gamma'] * df['volume'].fillna(0) * df['spot'].pow(2) * 100.0
    gex_abs = df.groupby('underlying_ticker')['gex_contrib'].sum().rename('gex')
    agg = agg.join(gex_abs, how='left')

    # ── Surface-level metrics ──────────────────────────────────────────────
    # iv_centroid_delta — delta-weighted mean IV across front expiry minus ATM IV.
    # Approximation: |moneyness-1| as a delta proxy (close enough for this signal).
    front_with_iv = fe[['underlying_ticker', 'iv', 'moneyness']].copy()
    front_with_iv['delta_proxy'] = (front_with_iv['moneyness'] - 1.0).abs()
    front_with_iv['w'] = np.exp(-5.0 * front_with_iv['delta_proxy'])   # weight near-ATM higher
    def _weighted_mean(g):
        w = g['w']
        return (g['iv'] * w).sum() / w.sum() if w.sum() > 0 else np.nan
    iv_centroid = front_with_iv.groupby('underlying_ticker').apply(_weighted_mean).rename('iv_centroid')
    agg = agg.join(iv_centroid, how='left')
    agg['iv_centroid_delta'] = agg['iv_centroid'] - agg['iv_front']
    agg.drop(columns=['iv_centroid'], inplace=True)

    # surface_premium — wings (OTM put IV + OTM call IV) / 2 minus ATM IV.
    # Positive means wings priced rich (fat-tail premium); negative means ATM rich.
    agg['surface_premium'] = ((agg['otm_put_iv'].fillna(agg['iv_front']) +
                                agg['otm_call_iv'].fillna(agg['iv_front'])) / 2.0
                              - agg['iv_front'])

    # Keep only tickers with at least a usable iv_front
    agg = agg[agg['iv_front'].notna()].copy()
    agg['date'] = pd.to_datetime(trade_date).date()
    agg = agg.reset_index().rename(columns={'underlying_ticker': 'ticker'})

    log.info('%s: %d tickers aggregated in %.1fs', trade_date, len(agg), time.time() - t0)
    return agg


def load_spot_map(prices_df: pd.DataFrame, trade_date: str) -> dict[str, float]:
    sub = prices_df[prices_df['date'].astype(str).str[:10] == trade_date]
    if sub.empty:
        return {}
    return sub.groupby('ticker')['close'].last().to_dict()


def partition_path(trade_date: str) -> Path:
    yy, mm = trade_date[:4], trade_date[5:7]
    return OUTPUT_DIR / f'{yy}-{mm}.parquet'


def load_existing_dates(month_path: Path) -> set[str]:
    if not month_path.exists():
        return set()
    try:
        df = pd.read_parquet(month_path, columns=['date'])
        return {str(d)[:10] for d in df['date'].unique()}
    except Exception as e:
        log.warning('Could not read %s for dedup: %s', month_path, e)
        return set()


def append_month(month_path: Path, rows: pd.DataFrame) -> None:
    """Append day-rows to a month-partition parquet. Atomic via rename."""
    if month_path.exists():
        prev = pd.read_parquet(month_path)
        rows = pd.concat([prev, rows], ignore_index=True)
    tmp = month_path.with_suffix('.parquet.tmp')
    rows.to_parquet(tmp, index=False)
    tmp.replace(month_path)


def iter_trading_dates(start: str, end: str) -> list[str]:
    # Use Massive's own enumeration — it only lists actual trading days.
    start_year = int(start[:4])
    end_year = int(end[:4])
    out = []
    for y in range(start_year, end_year + 1):
        for d in list_available_dates('us_options_opra', year=y):
            if start <= d <= end:
                out.append(d)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2022-01-03')
    ap.add_argument('--end',   default=None, help='defaults to yesterday')
    ap.add_argument('--one-day', default=None, help='process just this date and exit')
    args = ap.parse_args()

    if args.end is None:
        args.end = (datetime.utcnow().date() - timedelta(days=1)).isoformat()

    log.info('Loading prices master...')
    prices = pd.read_parquet(PRICES_PATH)
    log.info('Prices loaded: %d rows, %d tickers', len(prices), prices['ticker'].nunique())

    if args.one_day:
        dates = [args.one_day]
    else:
        dates = iter_trading_dates(args.start, args.end)
    log.info('Target dates: %d  (%s → %s)', len(dates), dates[0] if dates else '?', dates[-1] if dates else '?')

    # Group by month; read existing partitions once
    done = 0
    skipped = 0
    failed = 0
    current_month = None
    month_rows: list[pd.DataFrame] = []
    existing = set()

    def flush():
        nonlocal month_rows
        if current_month is None or not month_rows:
            return
        combined = pd.concat(month_rows, ignore_index=True)
        append_month(partition_path(current_month + '-01'), combined)
        log.info('Flushed %d rows to month %s', len(combined), current_month)
        month_rows = []

    t_start = time.time()
    for d in dates:
        month = d[:7]
        if month != current_month:
            flush()
            current_month = month
            existing = load_existing_dates(partition_path(d))
        if d in existing:
            skipped += 1
            continue
        spot = load_spot_map(prices, d)
        if not spot:
            log.warning('%s: no spot prices — skipping', d)
            failed += 1
            continue
        try:
            rows = compute_day_aggregates(d, spot)
        except Exception as e:
            log.exception('%s: compute failed: %s', d, e)
            failed += 1
            continue
        if rows.empty:
            failed += 1
            continue
        month_rows.append(rows)
        done += 1
        # Flush every 5 days so resumability is fine-grained
        if done % 5 == 0:
            flush()
            existing = load_existing_dates(partition_path(d))

    flush()
    log.info('Done. ok=%d skipped=%d failed=%d elapsed=%.1fs',
             done, skipped, failed, time.time() - t_start)


if __name__ == '__main__':
    main()
