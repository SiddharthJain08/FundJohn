#!/usr/bin/env python3
"""
daily_data_pipeline.py — Complete self-healing daily data pipeline.

Ensures all master parquets are fully populated on every run:
  - Adds today's rows for all tickers/series
  - Detects and fills any historical gaps
  - Handles column lifecycle: pending_add columns get backfilled, pending_remove
    columns get dropped — all in the same cycle

Schema lifecycle:
  Edit data/master/schema_registry.json to queue column changes:
    "pending_add":    [{"col": "...", "dtype": "...", "source": "..."}]
    "pending_remove": ["col1", "col2"]
  The pipeline applies them and clears the queue.

Usage:
  python3 scripts/daily_data_pipeline.py [--date YYYY-MM-DD] [--full-backfill]
  --full-backfill   ignore existing data, re-fetch everything
  --date            override run date (default: today)
"""

import os, sys, json, time, math, argparse, warnings, logging, tempfile
import asyncio, aiohttp
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import pandas as pd
import numpy as np
import psycopg2
import psycopg2.extras

warnings.filterwarnings('ignore')

ROOT      = Path(__file__).resolve().parent.parent
MASTER    = ROOT / 'data' / 'master'
SCHEMA_F  = MASTER / 'schema_registry.json'
LOG_DIR   = ROOT / 'logs'
LOG_DIR.mkdir(exist_ok=True)
MASTER.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [PIPELINE] %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / 'daily_data_pipeline.log'),
    ]
)
log = logging.getLogger(__name__)


# ── Configuration ─────────────────────────────────────────────────────────────

FMP_KEY  = os.environ.get('FMP_API_KEY', '')
AV_KEY   = os.environ.get('ALPHA_VANTAGE_API_KEY', '')
PG_URI   = os.environ.get('POSTGRES_URI', '')
FMP_BASE = 'https://financialmodelingprep.com/api/v3'
FMP_STABLE = 'https://financialmodelingprep.com/stable'
AV_BASE  = 'https://www.alphavantage.co/query'

# FMP Starter: 300 req/min → safe with 5 concurrent + 0.22s delay
FMP_CONCURRENT = 5
FMP_DELAY      = 0.22   # seconds between requests per slot
AV_DELAY       = 12.5   # Alpha Vantage free: 5 req/min = 12s gap

# How many years of price history to backfill
PRICE_BACKFILL_YEARS = 5
FIN_QUARTERS_BACKFILL = 20   # 5 years quarterly

# Macro series to fetch via yfinance (free, no rate limit)
MACRO_YFINANCE = {
    'VIX':   '^VIX',
    'VIX3M': '^VIX3M',
    'TLT':   'TLT',
    'HYG':   'HYG',
    'LQD':   'LQD',
    'SPY':   'SPY',
    'IWM':   'IWM',
    'GLD':   'GLD',
    'USO':   'USO',
    'DXY':   'DX-Y.NYB',
}


# ── Schema Registry ───────────────────────────────────────────────────────────

DEFAULT_SCHEMA = {
    "prices": {
        "columns": ["ticker","date","open","high","low","close","volume","vwap","transactions"],
        "pending_add": [],
        "pending_remove": []
    },
    "financials": {
        "columns": ["ticker","period","date","revenue","gross_profit","ebitda","net_income","eps",
                    "gross_margin","operating_margin","net_margin","revenue_growth",
                    "ev_revenue","ev_ebitda","pe_ratio","market_cap","roe","roic","debt_equity_ratio","p_fcf_ratio"],
        "pending_add": [],
        "pending_remove": []
    },
    "options_eod": {
        "columns": ["ticker","date","expiry","strike","option_type","market_price","implied_volatility",
                    "delta","gamma","theta","vega","rho","open_interest","volume","bid","ask"],
        "pending_add": [],
        "pending_remove": []
    },
    "insider": {
        "columns": ["ticker","date","transaction_date","insider_name","role","transaction_type",
                    "shares","price_per_share","net_value","shares_owned_after"],
        "pending_add": [],
        "pending_remove": []
    },
    "macro": {
        "columns": ["date","series","value","source"],
        "pending_add": [],
        "pending_remove": []
    },
    "earnings": {
        "columns": ["ticker","date","eps_actual","eps_estimated","revenue_actual","revenue_estimated"],
        "pending_add": [],
        "pending_remove": []
    }
}


def load_schema() -> dict:
    if SCHEMA_F.exists():
        try:
            return json.loads(SCHEMA_F.read_text())
        except Exception:
            pass
    SCHEMA_F.write_text(json.dumps(DEFAULT_SCHEMA, indent=2))
    return DEFAULT_SCHEMA


def save_schema(schema: dict):
    SCHEMA_F.write_text(json.dumps(schema, indent=2))


# ── Parquet Manager ───────────────────────────────────────────────────────────

def parquet_path(name: str) -> Path:
    return MASTER / f'{name}.parquet'


def read_parquet(name: str) -> pd.DataFrame:
    p = parquet_path(name)
    if not p.exists():
        return pd.DataFrame()
    try:
        return pd.read_parquet(p)
    except Exception as e:
        log.warning(f'read_parquet({name}): {e}')
        return pd.DataFrame()


def write_parquet_atomic(name: str, df: pd.DataFrame):
    """Write parquet atomically via temp file rename."""
    p = parquet_path(name)
    tmp = p.with_suffix('.tmp.parquet')
    df.to_parquet(tmp, index=False)
    tmp.rename(p)
    log.info(f'  {name}.parquet → {len(df):,} rows written')


def apply_schema_changes(name: str, df: pd.DataFrame, schema: dict) -> pd.DataFrame:
    """Apply pending_add (backfill NaN) and pending_remove (drop) column changes."""
    entry = schema.get(name, {})

    for col_spec in entry.get('pending_add', []):
        col = col_spec if isinstance(col_spec, str) else col_spec['col']
        if col not in df.columns:
            df[col] = np.nan
            log.info(f'  schema: added column {name}.{col} (NaN — backfill required)')

    for col in entry.get('pending_remove', []):
        if col in df.columns:
            df = df.drop(columns=[col])
            log.info(f'  schema: removed column {name}.{col}')

    # Clear queues after applying
    schema.setdefault(name, {})['pending_add']    = []
    schema.setdefault(name, {})['pending_remove']  = []

    return df


def merge_and_write(name: str, new_df: pd.DataFrame, key_cols: List[str], schema: dict) -> int:
    """
    Merge new_df into existing parquet:
    - Deduplicate on key_cols (new rows win)
    - Apply pending schema changes
    - Atomic write
    Returns number of net new rows added.
    """
    if new_df.empty:
        return 0

    existing = read_parquet(name)
    if existing.empty:
        combined = new_df.copy()
    else:
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=key_cols, keep='last')
        combined = combined.sort_values(key_cols).reset_index(drop=True)

    combined = apply_schema_changes(name, combined, schema)
    write_parquet_atomic(name, combined)
    return len(new_df)


def max_date(name: str, col: str = 'date') -> Optional[str]:
    df = read_parquet(name)
    if df.empty or col not in df.columns:
        return None
    val = df[col].max()
    return str(val) if val else None


def get_universe() -> List[str]:
    """Get ticker universe from prices.parquet or universe_config table."""
    df = read_parquet('prices')
    if not df.empty and 'ticker' in df.columns:
        tickers = sorted(df['ticker'].unique().tolist())
        if tickers:
            return tickers
    # Fallback: read from DB
    if PG_URI:
        try:
            conn = psycopg2.connect(PG_URI)
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT ticker FROM price_data ORDER BY ticker")
            rows = cur.fetchall()
            conn.close()
            if rows:
                return [r[0] for r in rows]
        except Exception:
            pass
    log.error('Cannot determine universe — prices.parquet missing and DB unavailable')
    return []


# ── Rate Limiter ──────────────────────────────────────────────────────────────

class TokenBucket:
    """Simple token bucket for rate limiting."""
    def __init__(self, rate_per_sec: float):
        self.rate  = rate_per_sec
        self.tokens = rate_per_sec
        self._last = time.monotonic()

    def acquire(self):
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
        if self.tokens >= 1:
            self.tokens -= 1
        else:
            sleep_for = (1 - self.tokens) / self.rate
            time.sleep(sleep_for)
            self.tokens = 0


fmp_limiter = TokenBucket(rate_per_sec=300/60)   # 5 req/sec


# ── FMP Helpers ───────────────────────────────────────────────────────────────

def fmp_get(path: str, params: dict = None, stable: bool = False) -> dict | list:
    """Synchronous FMP GET with rate limiting."""
    import requests
    fmp_limiter.acquire()
    base   = FMP_STABLE if stable else FMP_BASE
    url    = f'{base}{path}'
    p      = {'apikey': FMP_KEY, **(params or {})}
    try:
        r = requests.get(url, params=p, timeout=15)
        if r.status_code == 429:
            log.warning('FMP rate limit hit — sleeping 10s')
            time.sleep(10)
            return fmp_get(path, params, stable)
        if not r.ok:
            return []
        return r.json()
    except Exception as e:
        log.debug(f'FMP error {path}: {e}')
        return []


# ── Fetcher: Prices ───────────────────────────────────────────────────────────

def fetch_prices(tickers: List[str], since: str, full_backfill: bool) -> pd.DataFrame:
    """
    Fetch OHLCV prices from FMP for all tickers since `since` date.
    Full backfill: goes back PRICE_BACKFILL_YEARS years.
    """
    start = (date.today() - timedelta(days=PRICE_BACKFILL_YEARS * 365)).isoformat() \
            if full_backfill or not since else since
    log.info(f'Fetching prices: {len(tickers)} tickers from {start}')

    rows = []
    for i, ticker in enumerate(tickers):
        data = fmp_get(f'/historical-price-full/{ticker}', {'from': start, 'serietype': 'line'})
        hist = data.get('historical', []) if isinstance(data, dict) else []
        for bar in hist:
            rows.append({
                'ticker': ticker,
                'date':   bar.get('date', ''),
                'open':   bar.get('open'),
                'high':   bar.get('high'),
                'low':    bar.get('low'),
                'close':  bar.get('adjClose', bar.get('close')),
                'volume': bar.get('volume'),
                'vwap':   bar.get('vwap'),
                'transactions': bar.get('changeOverTime'),
            })
        if (i + 1) % 50 == 0:
            log.info(f'  prices: {i+1}/{len(tickers)} tickers fetched')

    df = pd.DataFrame(rows)
    if not df.empty:
        df['date'] = df['date'].astype(str)
        df = df[df['date'] > (since or '1900-01-01')]
    return df


# ── Fetcher: Financials ───────────────────────────────────────────────────────

def fetch_financials(tickers: List[str], full_backfill: bool) -> pd.DataFrame:
    """
    Fetch quarterly fundamentals from FMP for all tickers.
    Merges income statement, key metrics, and ratios.
    """
    log.info(f'Fetching financials: {len(tickers)} tickers')
    rows = []

    for i, ticker in enumerate(tickers):
        inc  = fmp_get(f'/income-statement/{ticker}', {'period': 'quarter', 'limit': FIN_QUARTERS_BACKFILL})
        km   = fmp_get(f'/key-metrics/{ticker}',      {'period': 'quarter', 'limit': FIN_QUARTERS_BACKFILL})
        rat  = fmp_get(f'/ratios/{ticker}',            {'period': 'quarter', 'limit': FIN_QUARTERS_BACKFILL})

        km_map  = {r.get('date'): r for r in km  if isinstance(r, dict)}
        rat_map = {r.get('date'): r for r in rat if isinstance(r, dict)}

        for stmt in (inc if isinstance(inc, list) else []):
            d = stmt.get('date', '')
            k = km_map.get(d, {})
            r = rat_map.get(d, {})
            rows.append({
                'ticker':           ticker,
                'period':           stmt.get('period', ''),
                'date':             d,
                'revenue':          stmt.get('revenue'),
                'gross_profit':     stmt.get('grossProfit'),
                'ebitda':           stmt.get('ebitda'),
                'net_income':       stmt.get('netIncome'),
                'eps':              stmt.get('eps'),
                'gross_margin':     r.get('grossProfitMargin',    stmt.get('grossProfitRatio')),
                'operating_margin': r.get('operatingProfitMargin',stmt.get('operatingIncomeRatio')),
                'net_margin':       r.get('netProfitMargin',       stmt.get('netIncomeRatio')),
                'revenue_growth':   stmt.get('revenueGrowth'),
                'ev_revenue':       k.get('evToSales'),
                'ev_ebitda':        k.get('enterpriseValueOverEBITDA'),
                'pe_ratio':         r.get('priceEarningsRatio'),
                'market_cap':       k.get('marketCap'),
                'roe':              k.get('roe'),
                'roic':             k.get('roic'),
                'debt_equity_ratio':r.get('debtEquityRatio'),
                'p_fcf_ratio':      k.get('priceToFreeCashFlowsRatio'),
            })

        if (i + 1) % 50 == 0:
            log.info(f'  financials: {i+1}/{len(tickers)} tickers fetched')

    df = pd.DataFrame(rows)
    if not df.empty:
        df['date'] = df['date'].astype(str)
        df = df[df['date'].notna() & (df['date'] != '')]
    return df


# ── Fetcher: Insider ──────────────────────────────────────────────────────────

def fetch_insider(tickers: List[str], lookback_days: int = 180) -> pd.DataFrame:
    """
    Fetch insider transactions from FMP for all tickers.
    Uses /insider-trading endpoint (last 50 transactions per ticker).
    """
    log.info(f'Fetching insider transactions: {len(tickers)} tickers')
    since = (date.today() - timedelta(days=lookback_days)).isoformat()
    rows  = []

    for i, ticker in enumerate(tickers):
        data = fmp_get(f'/insider-trading', {'symbol': ticker, 'limit': 50})
        for txn in (data if isinstance(data, list) else []):
            filing_date = txn.get('filingDate', txn.get('transactionDate', ''))[:10]
            if filing_date < since:
                continue
            rows.append({
                'ticker':           ticker,
                'date':             filing_date,
                'transaction_date': txn.get('transactionDate', '')[:10],
                'insider_name':     txn.get('reportingName', txn.get('insiderName', '')),
                'role':             txn.get('typeOfOwner', txn.get('role', '')),
                'transaction_type': txn.get('transactionType', txn.get('acquistionOrDisposition', '')),
                'shares':           txn.get('securitiesTransacted', txn.get('shares')),
                'price_per_share':  txn.get('price'),
                'net_value':        txn.get('securitiesOwned'),
                'shares_owned_after': txn.get('securitiesOwned'),
            })

        if (i + 1) % 100 == 0:
            log.info(f'  insider: {i+1}/{len(tickers)} tickers fetched')

    df = pd.DataFrame(rows)
    if not df.empty:
        df['date'] = df['date'].astype(str)
    return df


# ── Fetcher: Earnings ─────────────────────────────────────────────────────────

def fetch_earnings(tickers: List[str]) -> pd.DataFrame:
    """
    Fetch historical earnings surprises from FMP.
    Also fetches upcoming earnings calendar (next 90 days).
    """
    log.info(f'Fetching earnings: {len(tickers)} tickers (historical)')
    rows = []

    # Historical per-ticker (10 years, limit=40)
    for i, ticker in enumerate(tickers):
        data = fmp_get(f'/historical/earning_calendar/{ticker}', {'limit': 40})
        for e in (data if isinstance(data, list) else []):
            rows.append({
                'ticker':             ticker,
                'date':               str(e.get('date', ''))[:10],
                'eps_actual':         e.get('eps'),
                'eps_estimated':      e.get('epsEstimated'),
                'revenue_actual':     e.get('revenue'),
                'revenue_estimated':  e.get('revenueEstimated'),
            })

        if (i + 1) % 100 == 0:
            log.info(f'  earnings: {i+1}/{len(tickers)} tickers fetched')

    # Upcoming earnings calendar (next 90 days)
    from_d = date.today().isoformat()
    to_d   = (date.today() + timedelta(days=90)).isoformat()
    upcoming = fmp_get('/earning_calendar', {'from': from_d, 'to': to_d}, stable=True)
    for e in (upcoming if isinstance(upcoming, list) else []):
        ticker = e.get('symbol', '')
        if not ticker:
            continue
        rows.append({
            'ticker':             ticker,
            'date':               str(e.get('date', ''))[:10],
            'eps_actual':         e.get('eps'),
            'eps_estimated':      e.get('epsEstimated'),
            'revenue_actual':     e.get('revenue'),
            'revenue_estimated':  e.get('revenueEstimated'),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df['date'] = df['date'].astype(str)
        df = df[df['date'].notna() & (df['date'] != '')]
    return df


# ── Fetcher: Macro ────────────────────────────────────────────────────────────

def fetch_macro(since: str) -> pd.DataFrame:
    """
    Fetch macro/market series via yfinance (free):
      VIX, VIX3M, TLT, HYG, LQD, SPY, IWM, GLD, USO, DXY
    Optionally Alpha Vantage for economic data (CPI, GDP, fed funds).
    """
    import yfinance as yf
    log.info('Fetching macro series via yfinance')

    start = since or (date.today() - timedelta(days=PRICE_BACKFILL_YEARS * 365)).isoformat()
    rows  = []

    for series_name, yf_ticker in MACRO_YFINANCE.items():
        try:
            df = yf.download(yf_ticker, start=start, end=date.today().isoformat(),
                             auto_adjust=True, progress=False)
            if df.empty:
                log.warning(f'  macro: {series_name} ({yf_ticker}) — no data')
                continue
            closes = df['Close']
            if hasattr(closes, 'columns'):   # multi-ticker response
                closes = closes.iloc[:, 0]
            for dt, val in closes.items():
                dt_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
                if dt_str <= (since or '1900-01-01'):
                    continue
                if pd.notna(val):
                    rows.append({'date': dt_str, 'series': series_name,
                                 'value': float(val), 'source': 'yfinance'})
        except Exception as e:
            log.warning(f'  macro: {series_name} fetch failed: {e}')

    # Alpha Vantage economic indicators (if key available)
    if AV_KEY:
        av_series = {
            'FED_FUNDS_RATE': 'FEDERAL_FUNDS_RATE',
            'CPI':            'CPI',
            'UNEMPLOYMENT':   'UNEMPLOYMENT',
            'REAL_GDP':       'REAL_GDP',
        }
        import requests as _req
        for series_name, av_func in av_series.items():
            try:
                time.sleep(AV_DELAY)
                r = _req.get(AV_BASE, params={'function': av_func, 'apikey': AV_KEY,
                                               'datatype': 'json'}, timeout=15)
                if r.ok:
                    data = r.json().get('data', [])
                    for point in data:
                        dt_str = point.get('date', '')
                        if dt_str <= (since or '1900-01-01'):
                            continue
                        val = point.get('value', '')
                        if val and val != '.':
                            rows.append({'date': dt_str[:10], 'series': series_name,
                                         'value': float(val), 'source': 'alpha_vantage'})
            except Exception as e:
                log.warning(f'  macro: AV {series_name} failed: {e}')

    df = pd.DataFrame(rows)
    if not df.empty:
        df['date'] = df['date'].astype(str)
    return df


# ── DB Sync ───────────────────────────────────────────────────────────────────

def sync_to_db(df: pd.DataFrame, table: str, key_cols: List[str]):
    """Upsert DataFrame rows into PostgreSQL table (if DB configured)."""
    if not PG_URI or df.empty:
        return
    try:
        conn = psycopg2.connect(PG_URI)
        cur  = conn.cursor()
        cols = list(df.columns)
        placeholders = ','.join(['%s'] * len(cols))
        update_cols  = [c for c in cols if c not in key_cols]
        update_set   = ','.join(f'{c}=EXCLUDED.{c}' for c in update_cols)

        sql = (
            f'INSERT INTO {table} ({",".join(cols)}) VALUES ({placeholders})'
            + (f' ON CONFLICT ({",".join(key_cols)}) DO UPDATE SET {update_set}'
               if update_cols else f' ON CONFLICT ({",".join(key_cols)}) DO NOTHING')
        )
        rows = [tuple(row) for row in df.itertuples(index=False, name=None)]
        psycopg2.extras.execute_batch(cur, sql, rows, page_size=500)
        conn.commit()
        conn.close()
        log.info(f'  DB sync: {table} ← {len(df):,} rows upserted')
    except Exception as e:
        log.warning(f'  DB sync {table} failed: {e}')


# ── Gap Detection ─────────────────────────────────────────────────────────────

def detect_price_gaps(tickers: List[str], trading_days: List[str]) -> List[Tuple[str, str]]:
    """
    Return (ticker, date) pairs missing from prices.parquet.
    Uses the known trading calendar from existing price data.
    """
    df = read_parquet('prices')
    if df.empty:
        return [(t, d) for t in tickers for d in trading_days]

    have = set(zip(df['ticker'].tolist(), df['date'].astype(str).tolist()))
    expected = [(t, d) for t in tickers for d in trading_days]
    missing  = [(t, d) for t, d in expected if (t, d) not in have]
    return missing


def get_trading_calendar() -> List[str]:
    """Return sorted list of trading dates from prices.parquet."""
    df = read_parquet('prices')
    if df.empty:
        return []
    return sorted(df['date'].astype(str).unique().tolist())


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date',          default=date.today().isoformat())
    parser.add_argument('--full-backfill', action='store_true')
    args = parser.parse_args()

    run_date     = args.date
    full_backfill = args.full_backfill

    log.info(f'=== Daily Data Pipeline — {run_date} (full_backfill={full_backfill}) ===')
    if not FMP_KEY:
        log.error('FMP_API_KEY not set — aborting')
        sys.exit(1)

    schema = load_schema()
    stats  = {}
    t0     = time.time()

    # ── 1. Universe ───────────────────────────────────────────────────────────
    tickers = get_universe()
    if not tickers:
        log.error('Empty universe — cannot proceed')
        sys.exit(1)
    log.info(f'Universe: {len(tickers)} tickers')

    # ── 2. Prices ─────────────────────────────────────────────────────────────
    log.info('--- PRICES ---')
    prices_since = None if full_backfill else max_date('prices')
    price_df = fetch_prices(tickers, prices_since, full_backfill)
    n = merge_and_write('prices', price_df, ['ticker', 'date'], schema)
    stats['prices'] = n
    if PG_URI:
        sync_to_db(price_df, 'price_data', ['ticker', 'date'])

    # ── 3. Financials ─────────────────────────────────────────────────────────
    log.info('--- FINANCIALS ---')
    fin_df = fetch_financials(tickers, full_backfill)
    n = merge_and_write('financials', fin_df, ['ticker', 'date', 'period'], schema)
    stats['financials'] = n
    if PG_URI:
        sync_to_db(fin_df, 'fundamentals', ['ticker', 'period_end'])

    # ── 4. Insider ────────────────────────────────────────────────────────────
    log.info('--- INSIDER ---')
    ins_lookback = 365 * PRICE_BACKFILL_YEARS if full_backfill else 180
    ins_df = fetch_insider(tickers, ins_lookback)
    n = merge_and_write('insider', ins_df, ['ticker', 'date', 'insider_name', 'transaction_type'], schema)
    stats['insider'] = n
    if PG_URI:
        sync_to_db(ins_df, 'insider_transactions',
                   ['ticker', 'filing_date', 'insider_name', 'transaction_type'])

    # ── 5. Earnings ───────────────────────────────────────────────────────────
    log.info('--- EARNINGS ---')
    earn_df = fetch_earnings(tickers)
    n = merge_and_write('earnings', earn_df, ['ticker', 'date'], schema)
    stats['earnings'] = n

    # ── 6. Macro ──────────────────────────────────────────────────────────────
    log.info('--- MACRO ---')
    macro_since = None if full_backfill else max_date('macro')
    macro_df = fetch_macro(macro_since)
    n = merge_and_write('macro', macro_df, ['date', 'series'], schema)
    stats['macro'] = n

    # ── 7. Gap check (prices) ─────────────────────────────────────────────────
    log.info('--- GAP CHECK ---')
    cal   = get_trading_calendar()
    recent_cal = [d for d in cal if d >= (date.today() - timedelta(days=30)).isoformat()]
    gaps  = detect_price_gaps(tickers, recent_cal)
    if gaps:
        log.warning(f'  {len(gaps)} price gaps detected in last 30 days — fetching...')
        gap_tickers = sorted({t for t, _ in gaps})
        gap_min_date = min(d for _, d in gaps)
        gap_df = fetch_prices(gap_tickers, gap_min_date, False)
        n2 = merge_and_write('prices', gap_df, ['ticker', 'date'], schema)
        stats['price_gap_fill'] = n2
    else:
        log.info('  No price gaps in last 30 days.')

    # ── 8. Save schema (clears any applied pending changes) ───────────────────
    save_schema(schema)

    elapsed = time.time() - t0
    log.info(f'=== Pipeline complete in {elapsed:.1f}s ===')
    log.info(f'  Rows added: {json.dumps(stats)}')

    # Final parquet summary
    for name in ['prices', 'financials', 'options_eod', 'insider', 'macro', 'earnings']:
        p = parquet_path(name)
        if p.exists():
            try:
                df  = pd.read_parquet(p)
                mdt = df['date'].max() if 'date' in df.columns else 'n/a'
                log.info(f'  {name}: {len(df):,} rows | max_date={mdt}')
            except Exception:
                pass


if __name__ == '__main__':
    main()
