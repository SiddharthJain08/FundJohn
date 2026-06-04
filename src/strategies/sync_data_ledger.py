"""
sync_data_ledger.py — Populate data_columns from actual parquet files.

Scans data/master/*.parquet, computes coverage stats (min_date, max_date,
row_count, ticker_count), upserts into data_columns, then refreshes the
data_ledger materialized view.

Run at bot startup and on-demand. Safe to run multiple times (idempotent).

Output: JSON {"synced": N, "columns": [...]}
"""
from __future__ import annotations
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / 'data' / 'master'

# Map parquet filename stem → canonical column_name used in strategy data_requirements
PARQUET_MAP = {
    'prices':            'prices',
    'options_eod':       'options_eod',
    'macro':             'macro',
    'financials':        'financials',
    'earnings':          'earnings',
    'insider':           'insider',
    'prices_30m':        'prices_30m',
    'vol_indices':       'vol_indices',
    'iv_history':        'iv_history',
    'earnings_calendar': 'earnings_calendar',
    'intraday_features': 'intraday_features',  # 5-min HMM regime features (Stage 2)
}

# Derived columns computable from prices — inherit prices coverage
DERIVED_FROM_PRICES = [
    ('returns',       'computed'),
    ('log_returns',   'computed'),
    ('realized_vol',  'computed'),
]

# Derived columns computed from options_aggregates_enriched.parquet (which
# itself derives from options_aggregates/*.parquet via the rolling-fields
# enrichment script scripts/compute_rolling_options_fields.py). These
# inherit the enriched parquet's coverage so the staging-approval coverage
# check can pass once the enrichment has been run at least once.
#
# 2026-05-19: registered for HV-series strategies (Triple-Gate Fear etc.)
# that gate on unusual_options_flow as part of their setup logic. The
# heuristic is currently `pc_ratio > 1.5` — see compute_rolling_options_fields.py:124.
DERIVED_FROM_OPTIONS_AGGREGATES = [
    ('unusual_options_flow', 'derived_from_options_aggregates'),
]

# Provider mapping. AlphaVantage was removed 2026-04-28 — macro now sources
# from yfinance (matches the actual ingestion path in
# src/ingestion/fetch_vol_indices.py, which writes 'source': 'yfinance').
PROVIDERS = {
    'prices':            'polygon',
    'options_eod':       'polygon',     # Alpaca CLI alpha-preview returns 0 greeks; polygon stays primary
    'macro':             'yfinance',
    'financials':        'fmp',
    'earnings':          'yfinance',  # FMP /earnings-surprises returns 404; retargeted 2026-04-30
    'insider':           'sec_edgar',
    'prices_30m':        'alpaca',
    'vol_indices':       'yfinance',    # FMP/Polygon return 403/NOT_AUTHORIZED on indices
    'iv_history':        'polygon',     # derived from polygon-sourced options_eod
    'earnings_calendar': 'yfinance',    # FMP earning_calendar bulk endpoint returns 403
    'returns':           'computed',
    'log_returns':       'computed',
    'realized_vol':      'computed',
    'intraday_features': 'polygon+alpaca',     # synthetic VIX from option chain + intraday equity bars
    'unusual_options_flow': 'derived_from_options_aggregates',  # pc_ratio > 1.5 heuristic
}


def _stats(parquet_path: Path) -> dict:
    """Return {min_date, max_date, row_count, ticker_count} for a parquet file."""
    try:
        import pandas as pd
        df = pd.read_parquet(parquet_path)
    except Exception as e:
        return {'min_date': None, 'max_date': None, 'row_count': 0, 'ticker_count': 0, 'error': str(e)}

    row_count = len(df)
    if row_count == 0:
        return {'min_date': None, 'max_date': None, 'row_count': 0, 'ticker_count': 0}

    # Find date column. ts_utc is added for intraday-cadence parquets
    # (intraday_features.parquet); same coverage semantics as the daily
    # ones — we just truncate the timestamp to a date for ledger stats.
    date_col = next((c for c in ['date', 'Date', 'timestamp', 'Timestamp', 'ts_utc']
                      if c in df.columns), None)
    min_date = max_date = None
    if date_col:
        try:
            dates = pd.to_datetime(df[date_col], errors='coerce').dropna()
            if not dates.empty:
                # Only count historical dates (not future)
                today = pd.Timestamp.today().normalize()
                hist = dates[dates <= today]
                if not hist.empty:
                    min_date = hist.min().date().isoformat()
                    max_date = hist.max().date().isoformat()
        except Exception:
            pass

    # Count distinct tickers
    ticker_col = next((c for c in ['ticker', 'Ticker', 'symbol', 'Symbol'] if c in df.columns), None)
    ticker_count = int(df[ticker_col].nunique()) if ticker_col else 0

    return {
        'min_date':     min_date,
        'max_date':     max_date,
        'row_count':    row_count,
        'ticker_count': ticker_count,
    }


def sync() -> dict:
    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        return {'error': 'POSTGRES_URI not set', 'synced': 0, 'columns': []}

    import psycopg2

    synced_cols = []
    prices_stats = None

    conn = psycopg2.connect(pg_uri)
    try:
        cur = conn.cursor()

        # Ensure coverage columns exist (migration may not have run yet)
        for col in ['min_date DATE', 'max_date DATE', 'row_count BIGINT DEFAULT 0', 'ticker_count INT DEFAULT 0']:
            try:
                cur.execute(f'ALTER TABLE data_columns ADD COLUMN IF NOT EXISTS {col}')
            except Exception:
                conn.rollback()

        for stem, col_name in PARQUET_MAP.items():
            parquet = DATA_DIR / f'{stem}.parquet'
            if not parquet.exists():
                continue

            stats = _stats(parquet)
            if stem == 'prices':
                prices_stats = stats

            provider = PROVIDERS.get(col_name, 'unknown')
            cur.execute(
                """
                INSERT INTO data_columns
                  (column_name, provider, introduced_at, refresh_cadence,
                   estimated_monthly_cost, min_date, max_date, row_count, ticker_count)
                VALUES (%s, %s, NOW(), 'daily', 0,
                        %s::date, %s::date, %s, %s)
                ON CONFLICT (column_name) DO UPDATE SET
                  provider      = EXCLUDED.provider,
                  min_date      = EXCLUDED.min_date,
                  max_date      = EXCLUDED.max_date,
                  row_count     = EXCLUDED.row_count,
                  ticker_count  = EXCLUDED.ticker_count
                """,
                (
                    col_name, provider,
                    stats['min_date'], stats['max_date'],
                    stats['row_count'], stats['ticker_count'],
                )
            )
            synced_cols.append({
                'column': col_name,
                'min_date': stats['min_date'],
                'max_date': stats['max_date'],
                'row_count': stats['row_count'],
            })

        # Register derived columns computed from options_aggregates_enriched —
        # inherit that parquet's coverage. Cadence is monthly because the
        # upstream options_aggregates collector + the rolling-fields
        # enrichment script currently run irregularly (last refresh
        # 2026-04-22 as of registration). Bump to 'daily' once the
        # collector + enrichment are back on a daily cron.
        opts_agg_path = DATA_DIR / 'options_aggregates_enriched.parquet'
        if opts_agg_path.exists():
            opts_agg_stats = _stats(opts_agg_path)
            for col_name, provider in DERIVED_FROM_OPTIONS_AGGREGATES:
                cur.execute(
                    """
                    INSERT INTO data_columns
                      (column_name, provider, introduced_at, refresh_cadence,
                       estimated_monthly_cost, min_date, max_date, row_count, ticker_count)
                    VALUES (%s, %s, NOW(), 'monthly', 0,
                            %s::date, %s::date, %s, %s)
                    ON CONFLICT (column_name) DO UPDATE SET
                      provider     = EXCLUDED.provider,
                      refresh_cadence = EXCLUDED.refresh_cadence,
                      min_date     = EXCLUDED.min_date,
                      max_date     = EXCLUDED.max_date,
                      row_count    = EXCLUDED.row_count,
                      ticker_count = EXCLUDED.ticker_count
                    """,
                    (
                        col_name, provider,
                        opts_agg_stats['min_date'], opts_agg_stats['max_date'],
                        opts_agg_stats['row_count'], opts_agg_stats['ticker_count'],
                    )
                )
                synced_cols.append({'column': col_name,
                                    'derived_from': 'options_aggregates_enriched'})

        # Register derived columns — inherit prices coverage
        if prices_stats:
            for col_name, provider in DERIVED_FROM_PRICES:
                cur.execute(
                    """
                    INSERT INTO data_columns
                      (column_name, provider, introduced_at, refresh_cadence,
                       estimated_monthly_cost, min_date, max_date, row_count, ticker_count)
                    VALUES (%s, %s, NOW(), 'daily', 0,
                            %s::date, %s::date, %s, %s)
                    ON CONFLICT (column_name) DO UPDATE SET
                      min_date     = EXCLUDED.min_date,
                      max_date     = EXCLUDED.max_date,
                      row_count    = EXCLUDED.row_count,
                      ticker_count = EXCLUDED.ticker_count
                    """,
                    (
                        col_name, provider,
                        prices_stats['min_date'], prices_stats['max_date'],
                        prices_stats['row_count'], prices_stats['ticker_count'],
                    )
                )
                synced_cols.append({'column': col_name, 'derived_from': 'prices'})

        conn.commit()

        # Refresh the materialized view
        cur.execute('REFRESH MATERIALIZED VIEW CONCURRENTLY data_ledger')
        conn.commit()

    except Exception as e:
        conn.rollback()
        return {'error': str(e), 'synced': 0, 'columns': []}
    finally:
        conn.close()

    return {'synced': len(synced_cols), 'columns': synced_cols}


if __name__ == '__main__':
    result = sync()
    print(json.dumps(result))
    if 'error' in result:
        sys.exit(1)
