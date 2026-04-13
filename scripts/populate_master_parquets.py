#!/usr/bin/env python3
"""
populate_master_parquets.py

One-time script: copy data from PostgreSQL into the master Parquet files.
Faster and cheaper than re-fetching from APIs.
Subsequent updates come from the daily data collection pipeline.
"""

import os, sys
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'workspaces', 'default'))

MASTER_DIR   = os.path.join(os.path.dirname(__file__), '..', 'data', 'master')
POSTGRES_URI = os.environ['POSTGRES_URI']

os.makedirs(MASTER_DIR, exist_ok=True)

conn = psycopg2.connect(POSTGRES_URI)

print('=== Populating master Parquet files from PostgreSQL ===\n')

# ── prices.parquet ────────────────────────────────────────────────────────────
print('1/5 prices...')
prices = pd.read_sql("""
    SELECT
        ticker,
        date::text AS date,
        open,
        high,
        low,
        close,
        volume,
        vwap,
        transactions
    FROM price_data
    ORDER BY ticker, date
""", conn)
prices.to_parquet(os.path.join(MASTER_DIR, 'prices.parquet'), index=False)
print(f'    ✅ {len(prices):,} rows | {prices["ticker"].nunique()} tickers | '
      f'{prices["date"].min()} → {prices["date"].max()}')

# ── financials.parquet ────────────────────────────────────────────────────────
print('2/5 financials...')
financials = pd.read_sql("""
    SELECT
        ticker,
        period,
        period_end::text AS date,
        revenue,
        gross_profit,
        ebitda,
        net_income,
        eps,
        gross_margin,
        operating_margin,
        net_margin,
        revenue_growth_yoy AS revenue_growth,
        ev_revenue,
        ev_ebitda,
        pe_ratio,
        market_cap
    FROM fundamentals
    ORDER BY ticker, period_end
""", conn)
financials.to_parquet(os.path.join(MASTER_DIR, 'financials.parquet'), index=False)
print(f'    ✅ {len(financials):,} rows | {financials["ticker"].nunique()} tickers | '
      f'{financials["date"].min()} → {financials["date"].max()}')

# ── options_eod.parquet ───────────────────────────────────────────────────────
print('3/5 options_eod...')
options = pd.read_sql("""
    SELECT
        ticker,
        snapshot_date::text AS date,
        expiry::text AS expiry,
        strike,
        contract_type AS option_type,
        last_price AS market_price,
        iv AS implied_volatility,
        delta,
        gamma,
        theta,
        vega,
        rho,
        open_interest,
        volume,
        bid,
        ask
    FROM options_data
    ORDER BY ticker, snapshot_date, expiry, strike
""", conn)
options.to_parquet(os.path.join(MASTER_DIR, 'options_eod.parquet'), index=False)
print(f'    ✅ {len(options):,} rows | {options["ticker"].nunique()} tickers | '
      f'{options["date"].min()} → {options["date"].max()}')

# ── macro.parquet ─────────────────────────────────────────────────────────────
print('4/5 macro...')
# No macro_data table yet — write empty parquet with expected schema
macro = pd.DataFrame(columns=['date', 'series', 'value', 'source'])
macro.to_parquet(os.path.join(MASTER_DIR, 'macro.parquet'), index=False)
print('    ⚠️  No macro_data table yet — empty parquet written')
print('    Will populate from Alpha Vantage during next collection cycle')

# ── insider.parquet ───────────────────────────────────────────────────────────
print('5/5 insider...')
# No insider_transactions table yet — write empty parquet with expected schema
insider = pd.DataFrame(columns=[
    'ticker', 'date', 'transaction_date', 'insider_name', 'role',
    'transaction_type', 'shares', 'price_per_share', 'net_value', 'shares_owned_after'
])
insider.to_parquet(os.path.join(MASTER_DIR, 'insider.parquet'), index=False)
print('    ⚠️  No insider_transactions table yet — empty parquet written')
print('    Will populate from SEC EDGAR during next collection cycle')

conn.close()

print('\n=== Parquet population complete ===')
print('Run status check: python3 -c "import sys; sys.path.insert(0,\'workspaces/default\'); from tools.master_dataset import get_dataset_status; import json; print(json.dumps(get_dataset_status(), indent=2, default=str))"')
