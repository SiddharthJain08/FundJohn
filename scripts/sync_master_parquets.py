#!/usr/bin/env python3
"""
sync_master_parquets.py

Incremental Parquet sync — appends only rows newer than what's already in each Parquet.
Run after every collection cycle. Much faster than full reload.

Usage:
  python3 scripts/sync_master_parquets.py [--full]
  --full : rebuild entire Parquet from scratch (use only after schema changes)
"""

import os, sys, argparse
import pandas as pd
import psycopg2

MASTER_DIR   = os.path.join(os.path.dirname(__file__), '..', 'data', 'master')
POSTGRES_URI = os.environ['POSTGRES_URI']
FULL_REBUILD = '--full' in sys.argv

os.makedirs(MASTER_DIR, exist_ok=True)
conn = psycopg2.connect(POSTGRES_URI)

def existing_max(parquet_path, date_col='date'):
    """Return max date already in parquet, or None if file doesn't exist / is empty."""
    try:
        df = pd.read_parquet(parquet_path, columns=[date_col])
        if df.empty: return None
        return df[date_col].max()
    except Exception:
        return None

def append_or_create(parquet_path, new_df):
    """Append new rows to existing parquet, or create if missing."""
    if new_df.empty:
        return 0
    if os.path.exists(parquet_path) and not FULL_REBUILD:
        try:
            existing = pd.read_parquet(parquet_path)
            combined = pd.concat([existing, new_df], ignore_index=True)
            combined.to_parquet(parquet_path, index=False)
            return len(new_df)
        except Exception:
            pass
    new_df.to_parquet(parquet_path, index=False)
    return len(new_df)

total_new_rows = 0

# ── prices.parquet ────────────────────────────────────────────────────────────
prices_path = os.path.join(MASTER_DIR, 'prices.parquet')
max_date    = None if FULL_REBUILD else existing_max(prices_path)

if FULL_REBUILD or max_date is None:
    query = "SELECT ticker, date::text AS date, open, high, low, close, volume, vwap, transactions FROM price_data ORDER BY ticker, date"
    params = []
else:
    query = "SELECT ticker, date::text AS date, open, high, low, close, volume, vwap, transactions FROM price_data WHERE date > %s ORDER BY ticker, date"
    params = [max_date]

import warnings
with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    prices_new = pd.read_sql(query, conn, params=params or None)

n = append_or_create(prices_path, prices_new)
total_new_rows += n

total_price_rows = len(pd.read_parquet(prices_path)) if os.path.exists(prices_path) else n
print(f'prices: +{n:,} new rows | total {total_price_rows:,} rows | max date: {existing_max(prices_path)}')

# ── financials.parquet ────────────────────────────────────────────────────────
fin_path = os.path.join(MASTER_DIR, 'financials.parquet')
max_fin_date = None if FULL_REBUILD else existing_max(fin_path)

if FULL_REBUILD or max_fin_date is None:
    fin_query = """SELECT ticker, period, period_end::text AS date, revenue, gross_profit, ebitda,
        net_income, eps, gross_margin, operating_margin, net_margin, revenue_growth_yoy AS revenue_growth,
        ev_revenue, ev_ebitda, pe_ratio, market_cap,
        roe, roic, debt_equity_ratio, p_fcf_ratio
        FROM fundamentals ORDER BY ticker, period_end"""
    fin_params = []
else:
    fin_query = """SELECT ticker, period, period_end::text AS date, revenue, gross_profit, ebitda,
        net_income, eps, gross_margin, operating_margin, net_margin, revenue_growth_yoy AS revenue_growth,
        ev_revenue, ev_ebitda, pe_ratio, market_cap,
        roe, roic, debt_equity_ratio, p_fcf_ratio
        FROM fundamentals WHERE period_end::text > %s ORDER BY ticker, period_end"""
    fin_params = [max_fin_date]

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    fin_new = pd.read_sql(fin_query, conn, params=fin_params or None)

n = append_or_create(fin_path, fin_new)
total_new_rows += n
total_fin_rows = len(pd.read_parquet(fin_path)) if os.path.exists(fin_path) else n
print(f'financials: +{n:,} new rows | total {total_fin_rows:,} rows')

# ── options_eod.parquet ───────────────────────────────────────────────────────
opt_path = os.path.join(MASTER_DIR, 'options_eod.parquet')
max_opt_date = None if FULL_REBUILD else existing_max(opt_path)

if FULL_REBUILD or max_opt_date is None:
    opt_query = """SELECT ticker, snapshot_date::text AS date, expiry::text AS expiry, strike,
        contract_type AS option_type, last_price AS market_price, iv AS implied_volatility,
        delta, gamma, theta, vega, rho, open_interest, volume, bid, ask
        FROM options_data ORDER BY ticker, snapshot_date, expiry, strike"""
    opt_params = []
else:
    opt_query = """SELECT ticker, snapshot_date::text AS date, expiry::text AS expiry, strike,
        contract_type AS option_type, last_price AS market_price, iv AS implied_volatility,
        delta, gamma, theta, vega, rho, open_interest, volume, bid, ask
        FROM options_data WHERE snapshot_date::text > %s ORDER BY ticker, snapshot_date, expiry, strike"""
    opt_params = [max_opt_date]

with warnings.catch_warnings():
    warnings.simplefilter('ignore')
    opt_new = pd.read_sql(opt_query, conn, params=opt_params or None)

n = append_or_create(opt_path, opt_new)
total_new_rows += n
total_opt_rows = len(pd.read_parquet(opt_path)) if os.path.exists(opt_path) else n
print(f'options_eod: +{n:,} new rows | total {total_opt_rows:,} rows')

# ── macro.parquet — append if macro_data table exists ────────────────────────
macro_path = os.path.join(MASTER_DIR, 'macro.parquet')
try:
    macro_conn = psycopg2.connect(POSTGRES_URI)
    macro_max = None if FULL_REBUILD else existing_max(macro_path)
    if macro_max:
        macro_new = pd.read_sql("SELECT * FROM macro_data WHERE date::text > %s ORDER BY date", macro_conn, params=[macro_max])
    else:
        macro_new = pd.read_sql("SELECT * FROM macro_data ORDER BY date", macro_conn)
    macro_conn.close()
    n = append_or_create(macro_path, macro_new)
    total_new_rows += n
    print(f'macro: +{n:,} new rows')
except Exception as e:
    print(f'macro: skipped ({str(e).split(chr(10))[0]})')

# ── insider.parquet — append if insider_transactions table exists ─────────────
insider_path = os.path.join(MASTER_DIR, 'insider.parquet')
try:
    ins_conn = psycopg2.connect(POSTGRES_URI)
    ins_max = None if FULL_REBUILD else existing_max(insider_path, 'date')
    if ins_max:
        ins_new = pd.read_sql("SELECT ticker, filing_date::text AS date, transaction_date::text, insider_name, role, transaction_type, shares, price_per_share, total_value AS net_value, shares_owned_after FROM insider_transactions WHERE filing_date::text > %s ORDER BY filing_date", ins_conn, params=[ins_max])
    else:
        ins_new = pd.read_sql("SELECT ticker, filing_date::text AS date, transaction_date::text, insider_name, role, transaction_type, shares, price_per_share, total_value AS net_value, shares_owned_after FROM insider_transactions ORDER BY filing_date", ins_conn)
    ins_conn.close()
    n = append_or_create(insider_path, ins_new)
    total_new_rows += n
    print(f'insider: +{n:,} new rows')
except Exception as e:
    print(f'insider: skipped ({str(e).split(chr(10))[0]})')

conn.close()
print(f'\nSync complete — {total_new_rows:,} total new rows written to Parquets')
