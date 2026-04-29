#!/usr/bin/env python3
"""
parquet_reader.py — CLI wrapping parquet_store for Node consumption.

Usage:
  python3 parquet_reader.py --op <op> --args '<json>'

Ops:
  prices            {ticker, days?, limit?}
  options           {ticker, limit?, type?}       # type = CALL|PUT|null
  fundamentals      {ticker, limit?}
  insider           {ticker, limit?}
  macro             {series, days?}
  chart             {ticker, limit?}              # ordered ASC by date for plotting
  market_overview   {}                            # latest-per-ticker + change_pct
  latest_snapshots  {tickers?: string[]}
  freshness         {}                            # max_date per dataset
  write_prices      {rows: [...]}                 # from collector
  write_options     {rows: [...]}
  write_fundamentals {rows: [...]}
  write_insider     {rows: [...]}
  write_macro       {rows: [...]}

All output is newline-delimited JSON on stdout. Errors to stderr with non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from datetime import date, datetime, timedelta

# Ensure src/ importable
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import duckdb

from src.data import parquet_store as ps


def _json_default(o):
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()
    if hasattr(o, 'item'):
        return o.item()
    return str(o)


def _df_to_records(df: pd.DataFrame) -> list:
    if df.empty:
        return []
    out = df.to_dict(orient='records')
    # Normalize pandas/numpy scalars → JSON-safe
    for row in out:
        for k, v in list(row.items()):
            if pd.isna(v):
                row[k] = None
            elif hasattr(v, 'isoformat'):
                row[k] = v.isoformat()
            elif hasattr(v, 'item'):
                row[k] = v.item()
    return out


# ── Read ops ─────────────────────────────────────────────────────────────────

def op_prices(args):
    ticker = args['ticker']
    days   = args.get('days')
    limit  = args.get('limit')
    where = f"ticker='{ticker}'"
    if days:
        cutoff = (date.today() - timedelta(days=int(days))).isoformat()
        where += f" AND date >= '{cutoff}'"
    df = ps.read_filtered(ps.PRICES_PATH, where=where, order_by='date DESC', limit=limit)
    return _df_to_records(df)


def op_options(args):
    ticker = args['ticker']
    limit  = args.get('limit', 30)
    otype  = args.get('type')
    where = f"ticker='{ticker}'"
    if otype:
        where += f" AND option_type='{otype}'"
    # Match legacy DB behavior: most recent snapshot first, then highest open interest.
    df = ps.read_filtered(ps.OPTIONS_PATH, where=where,
                          order_by='date DESC, open_interest DESC NULLS LAST',
                          limit=limit)
    return _df_to_records(df)


def op_fundamentals(args):
    ticker = args['ticker']
    limit  = args.get('limit', 4)
    df = ps.read_filtered(ps.FUNDAMENTALS_PATH, where=f"ticker='{ticker}'",
                          order_by='date DESC', limit=limit)
    return _df_to_records(df)


def op_insider(args):
    ticker = args['ticker']
    limit  = args.get('limit', 50)
    df = ps.read_filtered(ps.INSIDER_PATH, where=f"ticker='{ticker}'",
                          order_by='transaction_date DESC', limit=limit)
    return _df_to_records(df)


def op_macro(args):
    series = args['series']
    days   = args.get('days')
    where = f"series='{series}'"
    if days:
        cutoff = (date.today() - timedelta(days=int(days))).isoformat()
        where += f" AND date >= '{cutoff}'"
    df = ps.read_filtered(ps.MACRO_PATH, where=where, order_by='date ASC')
    return _df_to_records(df)


def op_chart(args):
    """1-year OHLCV ordered ASC for plotting."""
    ticker = args['ticker']
    limit  = args.get('limit', 365)
    # Take last `limit` rows then reverse to ASC
    df = ps.read_filtered(ps.PRICES_PATH, where=f"ticker='{ticker}'",
                          cols=['date', 'close', 'volume'],
                          order_by='date DESC', limit=limit)
    df = df.sort_values('date')
    return _df_to_records(df)


def op_market_overview(args):
    """Latest price per ticker with prev_close and change_pct."""
    sql = f"""
        WITH ranked AS (
            SELECT ticker, date, open, high, low, close, volume, vwap,
                   ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn,
                   LAG(close) OVER (PARTITION BY ticker ORDER BY date) AS prev_close
            FROM read_parquet('{ps.PRICES_PATH}')
        )
        SELECT ticker, date, open, high, low, close, volume, vwap, prev_close,
               CASE WHEN prev_close IS NOT NULL AND prev_close > 0
                    THEN (close - prev_close) / prev_close * 100.0
                    ELSE NULL END AS change_pct
        FROM ranked WHERE rn = 1
        ORDER BY ticker
    """
    df = duckdb.sql(sql).df()
    return _df_to_records(df)


def op_latest_snapshots(args):
    """Replaces the legacy snapshots table. Returns one row per ticker with change_pct."""
    tickers = args.get('tickers')
    rows = op_market_overview({})
    if tickers:
        keep = set(tickers)
        rows = [r for r in rows if r['ticker'] in keep]
    # Shape to match old snapshots schema
    return [
        {
            'ticker':      r['ticker'],
            'price':       r['close'],
            'change_pct':  r['change_pct'],
            'volume':      r['volume'],
            'snapshot_at': r['date'],
        }
        for r in rows
    ]


def op_freshness_per_ticker(args):
    """Per-ticker max(date) across prices + options (replaces legacy getDataFreshness)."""
    tickers = args.get('tickers') or []
    if not tickers:
        return []
    # Safe because tickers are strings from our own universe, but still parameterize.
    tickers_sql = ','.join(f"'{t}'" for t in tickers)
    sql = f"""
        WITH px AS (
            SELECT ticker, MAX(date) AS latest_price
            FROM read_parquet('{ps.PRICES_PATH}')
            WHERE ticker IN ({tickers_sql}) GROUP BY ticker
        ),
        opt AS (
            SELECT ticker, MAX(date) AS latest_options
            FROM read_parquet('{ps.OPTIONS_PATH}')
            WHERE ticker IN ({tickers_sql}) GROUP BY ticker
        )
        SELECT COALESCE(px.ticker, opt.ticker) AS ticker,
               px.latest_price,
               px.latest_price AS latest_snapshot,
               opt.latest_options
        FROM px FULL OUTER JOIN opt ON px.ticker = opt.ticker
        ORDER BY ticker
    """
    df = duckdb.sql(sql).df()
    return _df_to_records(df)


def op_freshness(args):
    """One row per dataset: max_date, row_count, status vs last trading day.

    Per-dataset thresholds (`fresh_max_d`, `stale_max_d`) account for
    different update cadences:
      - daily series (prices/options/macro): fresh ≤ 1d, stale ≤ 3d
      - irregular filings (insider): fresh ≤ 7d, stale ≤ 30d
      - quarterly (fundamentals): fresh ≤ 95d (covers a full earnings
        cycle), stale ≤ 180d. Without this carve-out the digest's
        "stale" alert fires every quarter on the day after period_end
        even though new data isn't expected for ~75 more days.
    """
    # Last trading day (Mon-Fri, ignores holidays — matches SQL VIEW logic from migration 039)
    today = date.today()
    dow = today.weekday()  # Mon=0, Sun=6
    if dow == 5:       # Sat → Fri
        expected = today - timedelta(days=1)
    elif dow == 6:     # Sun → Fri
        expected = today - timedelta(days=2)
    elif dow == 0:     # Mon → Fri (prev week)
        expected = today - timedelta(days=3)
    else:              # Tue-Fri → prev weekday
        expected = today - timedelta(days=1)

    # (dataset, parquet_path, date_col, fresh_max_d, stale_max_d)
    datasets = [
        ('price_data',     ps.PRICES_PATH,       'date',             1,   3),
        ('options_data',   ps.OPTIONS_PATH,      'date',             1,   3),
        ('macro_data',     ps.MACRO_PATH,        'date',             1,   5),
        ('insider',        ps.INSIDER_PATH,      'transaction_date', 7,  30),
        ('fundamentals',   ps.FUNDAMENTALS_PATH, 'date',            95, 180),
    ]
    out = []
    for name, path, dcol, fresh_d, stale_d in datasets:
        md   = ps.max_date(path, dcol)
        rc   = ps.row_count(path)
        if md is None:
            status = 'empty'
            delta = None
        else:
            md_date = datetime.fromisoformat(md[:10]).date() if isinstance(md, str) else md
            delta   = (expected - md_date).days
            if delta <= 0:
                status = 'current'
            elif delta <= fresh_d:
                status = 'fresh'
            elif delta <= stale_d:
                status = 'stale'
            else:
                status = 'very_stale'
        out.append({
            'dataset':       name,
            'max_date':      md,
            'expected_date': expected.isoformat(),
            'delta_days':    delta,
            'row_count':     rc,
            'status':        status,
        })
    # Sort: most stale first, then empty, then current
    out.sort(key=lambda r: (-(r['delta_days'] or -1), r['dataset']))
    return out


# ── Write ops ────────────────────────────────────────────────────────────────

def op_write_prices(args):
    return {'rows_after': ps.write_prices(args.get('rows', []))}

def op_write_options(args):
    return {'rows_after': ps.write_options(args.get('rows', []))}

def op_write_fundamentals(args):
    return {'rows_after': ps.write_fundamentals(args.get('rows', []))}

def op_write_insider(args):
    return {'rows_after': ps.write_insider(args.get('rows', []))}

def op_write_macro(args):
    return {'rows_after': ps.write_macro(args.get('rows', []))}


# ── Dispatch ─────────────────────────────────────────────────────────────────

OPS = {
    'prices':            op_prices,
    'options':           op_options,
    'fundamentals':      op_fundamentals,
    'insider':           op_insider,
    'macro':             op_macro,
    'chart':             op_chart,
    'market_overview':   op_market_overview,
    'market-overview':   op_market_overview,
    'latest_snapshots':  op_latest_snapshots,
    'latest-snapshots':  op_latest_snapshots,
    'freshness':             op_freshness,
    'freshness_per_ticker':  op_freshness_per_ticker,
    'write_prices':       op_write_prices,
    'write_options':      op_write_options,
    'write_fundamentals': op_write_fundamentals,
    'write_insider':      op_write_insider,
    'write_macro':        op_write_macro,
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--op',   required=True, help='Operation name (see OPS dict)')
    parser.add_argument('--args', default='{}',  help='JSON args dict')
    parser.add_argument('--args-file', default=None,
                        help='Read args JSON from file (use for large payloads exceeding argv limit)')
    a = parser.parse_args()

    op = OPS.get(a.op)
    if op is None:
        print(f'Unknown op: {a.op}. Known: {list(OPS)}', file=sys.stderr)
        sys.exit(2)

    if a.args_file:
        with open(a.args_file) as f:
            args = json.load(f)
    else:
        args = json.loads(a.args) if a.args else {}

    try:
        result = op(args)
    except Exception as e:
        print(f'op {a.op} failed: {type(e).__name__}: {e}', file=sys.stderr)
        sys.exit(1)

    json.dump(result, sys.stdout, default=_json_default)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
