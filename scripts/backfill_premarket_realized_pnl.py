#!/usr/bin/env python3
"""EOD realized-PnL backfill for premarket_panic_alerts.

Fires at 16:05 ET. Reads alerts with NULL realized_backfilled_at and a
fully-elapsed trading_day. Looks up that ticker's open + close from
data/master/prices.parquet (the authoritative daily-bars source — no Postgres
daily-bars table exists; all OHLCV lives in the parquet), plus the next
trading day's open, and writes the two realized-pct columns. Idempotent.

NOTE on data source:
  The plan referenced 'alpaca_bars_daily' (a Postgres table) — that table does
  NOT exist. Daily OHLCV for all tickers lives in
  /root/openclaw/data/master/prices.parquet
  (columns: ticker, date[str], open, high, low, close, volume, vwap,
   transactions, source).
  This script reads from that parquet using pyarrow predicate pushdown.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd


log = logging.getLogger(__name__)

_PRICES_PATH = Path(__file__).parent.parent / 'data' / 'master' / 'prices.parquet'


def _fetch_unfilled_alerts() -> list[dict]:
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT id, ticker, trading_day
              FROM premarket_panic_alerts
             WHERE realized_backfilled_at IS NULL
               AND trading_day <= (now() AT TIME ZONE 'America/New_York')::date
        """)
        return [dict(r) for r in cur.fetchall()]


def _fetch_bars_for(ticker: str, trading_day: date) -> dict | None:
    """Return {'open': float, 'close': float, 'open_next': float} or None.

    Reads from data/master/prices.parquet — the authoritative daily-bars store.
    No Postgres daily-bars table exists in this project.

    Returns None when:
    - trading_day has no bar for ticker
    - next trading day's open is not yet available
    """
    trading_day_str = trading_day.isoformat()
    # Read a 10-day forward window to find the next trading day's open,
    # accounting for weekends and market holidays.
    cutoff_str = (trading_day + timedelta(days=10)).isoformat()

    try:
        df = pd.read_parquet(
            _PRICES_PATH,
            filters=[
                ('ticker', '==', ticker),
                ('date', '>=', trading_day_str),
                ('date', '<=', cutoff_str),
            ],
            columns=['ticker', 'date', 'open', 'close'],
        )
    except Exception as e:
        log.warning('parquet read error for %s %s: %s', ticker, trading_day_str, e)
        return None

    if df.empty:
        return None

    df = df.sort_values('date').reset_index(drop=True)

    # First row must be the requested trading_day
    if str(df.iloc[0]['date']) != trading_day_str:
        return None

    # Need at least 2 rows (trading_day + next trading day)
    if len(df) < 2:
        return None

    today_row = df.iloc[0]
    next_row = df.iloc[1]

    return {
        'open': float(today_row['open']),
        'close': float(today_row['close']),
        'open_next': float(next_row['open']),
    }


def _compute_pnl(open_t: float, close_t: float, open_tplus1: float) -> dict:
    return {
        'open_to_close': (close_t - open_t) / open_t,
        'open_to_open':  (open_tplus1 - open_t) / open_t,
    }


def _write_pnl_back(alert_id: int, pnl: dict) -> None:
    dsn = os.environ['POSTGRES_URI']
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            UPDATE premarket_panic_alerts
               SET realized_open_to_close_pct = %s,
                   realized_open_to_open_pct  = %s,
                   realized_backfilled_at     = now()
             WHERE id = %s
        """, (pnl['open_to_close'], pnl['open_to_open'], alert_id))
        conn.commit()


def backfill_rows() -> int:
    rows = _fetch_unfilled_alerts()
    if not rows:
        log.info('no alerts pending backfill')
        return 0
    written = 0
    for r in rows:
        bars = _fetch_bars_for(r['ticker'], r['trading_day'])
        if bars is None:
            log.info('bars unavailable for %s %s; skipping (will retry next EOD)',
                     r['ticker'], r['trading_day'])
            continue
        pnl = _compute_pnl(
            open_t=bars['open'],
            close_t=bars['close'],
            open_tplus1=bars['open_next'],
        )
        _write_pnl_back(r['id'], pnl)
        written += 1
    log.info('backfilled %d / %d alerts', written, len(rows))
    return written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    backfill_rows()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
