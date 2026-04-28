#!/usr/bin/env python3
"""
alpaca_corporate_actions.py — Phase 2.3 of alpaca-cli integration.

Pulls corporate actions (forward / reverse splits, cash dividends, mergers,
spinoffs) from `alpaca data corporate-actions` and writes them to
data/master/corporate_actions.parquet. Append-only (per the master-data
invariant in CLAUDE.md): rows are merged with existing parquet and
deduplicated on `id`. Run as a daily collector phase or one-shot CLI.

Usage:
    python3 src/pipeline/alpaca_corporate_actions.py \\
        [--symbols AAPL,MSFT,...] [--start 2020-01-01] [--end 2026-04-28]

If --symbols is omitted, the active universe_config tickers are used.
If --start is omitted, defaults to 1 year before today.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

ALPACA_CLI         = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
PARQUET_PATH       = ROOT / 'data' / 'master' / 'corporate_actions.parquet'

ACTION_KIND_KEYS = [
    ('forward_splits',  'forward_split'),
    ('reverse_splits',  'reverse_split'),
    ('cash_dividends',  'cash_dividend'),
    ('stock_dividends', 'stock_dividend'),
    ('mergers',         'merger'),
    ('spinoffs',        'spinoff'),
    ('redemptions',     'redemption'),
]


def log(msg: str) -> None:
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'{ts} [CORP_ACT] {msg}')


def fetch_corporate_actions(symbols, start, end, *, types=None, page_size=100, max_pages=50):
    """Page through `alpaca data corporate-actions` and return a flat list of
    rows. Each row has at minimum (symbol, ex_date, action_type) plus
    type-specific fields (new_rate, old_rate for splits; rate, foreign_tax,
    etc. for dividends).
    """
    rows = []
    page_token = None
    type_filter = ','.join(types) if types else None
    for _ in range(max_pages):
        args = [ALPACA_CLI, 'data', 'corporate-actions',
                '--symbols', ','.join(symbols),
                '--start',   start,
                '--end',     end,
                '--limit',   str(page_size),
                '--sort',    'asc']
        if type_filter:
            args += ['--types', type_filter]
        if page_token:
            args += ['--page-token', page_token]
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f'alpaca data corporate-actions failed: {proc.stderr[:300]}')
        if not proc.stdout.strip():
            break
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'CLI returned non-JSON stdout: {exc}')
        actions = (payload.get('corporate_actions') or {})
        for parquet_key, action_type in ACTION_KIND_KEYS:
            for row in (actions.get(parquet_key) or []):
                # ratio: meaningful for splits only. For other types, leave None.
                ratio = None
                if action_type in ('forward_split', 'reverse_split'):
                    new_rate = row.get('new_rate')
                    old_rate = row.get('old_rate')
                    if new_rate is not None and old_rate not in (None, 0):
                        ratio = float(new_rate) / float(old_rate)
                rows.append({
                    'id':           row.get('id'),
                    'symbol':       row.get('symbol'),
                    'action_type':  action_type,
                    'ex_date':      row.get('ex_date')      or row.get('process_date'),
                    'payable_date': row.get('payable_date'),
                    'record_date':  row.get('record_date'),
                    'new_rate':     row.get('new_rate'),
                    'old_rate':     row.get('old_rate'),
                    'ratio':        ratio,
                    'cash_amount':  row.get('rate'),    # cash dividend dollar amount
                    'cusip':        row.get('cusip'),
                    'raw':          json.dumps(row),    # keep full payload for forward-compat
                })
        page_token = payload.get('next_page_token')
        if not page_token:
            break
    return rows


def merge_into_parquet(new_rows):
    """Append new_rows into corporate_actions.parquet, deduplicating by `id`.
    Returns (n_total_after, n_added)."""
    import pandas as pd
    PARQUET_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_df = pd.DataFrame(new_rows)
    if not new_df.empty and 'id' in new_df.columns:
        new_df = new_df.drop_duplicates(subset=['id'])

    if PARQUET_PATH.exists():
        existing = pd.read_parquet(PARQUET_PATH)
        before = len(existing)
        # Outer merge by id, keeping new rows for collisions (they may have
        # corrected fields).
        if 'id' in existing.columns and not new_df.empty:
            existing_filtered = existing[~existing['id'].isin(new_df['id'])]
            merged = pd.concat([existing_filtered, new_df], ignore_index=True)
        else:
            merged = pd.concat([existing, new_df], ignore_index=True)
        added = len(merged) - before
    else:
        merged = new_df
        added = len(merged)

    if not merged.empty:
        merged = merged.sort_values(['symbol', 'ex_date']).reset_index(drop=True)
        merged.to_parquet(PARQUET_PATH, index=False)
    return len(merged), added


def get_active_universe_tickers(conn):
    cur = conn.cursor()
    cur.execute("SELECT ticker FROM universe_config WHERE active=true")
    out = [r[0] for r in cur.fetchall()]
    cur.close()
    return out


def main():
    # Always load .env so the CLI subprocess inherits ALPACA_API_KEY/SECRET,
    # not just when we need POSTGRES_URI for the universe lookup.
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument('--symbols', help='comma-separated; defaults to active universe')
    ap.add_argument('--start',   help='YYYY-MM-DD; default = 365d ago')
    ap.add_argument('--end',     default=str(date.today()))
    ap.add_argument('--types',   help='comma-separated; default = all')
    ap.add_argument('--dry-run', action='store_true',
                    help='Fetch from CLI; print what would be merged without '
                         'touching corporate_actions.parquet.')
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    else:
        try:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
            symbols = get_active_universe_tickers(conn)
            conn.close()
        except Exception as exc:
            log(f'failed to read universe: {exc}')
            sys.exit(1)

    start = args.start or (date.today() - timedelta(days=365)).isoformat()
    types = [t.strip() for t in args.types.split(',')] if args.types else None
    log(f'pulling corp actions: {len(symbols)} symbols, {start} → {args.end}')

    # Alpaca's --symbols param has a length cap (~500 chars). Page through chunks.
    chunk_size = 50
    all_rows = []
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        try:
            rows = fetch_corporate_actions(chunk, start, args.end, types=types)
        except RuntimeError as exc:
            log(f'chunk {i}-{i+chunk_size} failed: {exc}')
            continue
        all_rows.extend(rows)
        log(f'  chunk {i // chunk_size + 1}: +{len(rows)} actions')

    if args.dry_run:
        log(f'DRY-RUN: would merge {len(all_rows)} fetched actions '
            f'into corporate_actions.parquet (skipped)')
        # Sample first few for operator preview
        for r in all_rows[:5]:
            log(f'  sample: {r["symbol"]} {r["action_type"]} {r["ex_date"]} ratio={r["ratio"]}')
    else:
        total, added = merge_into_parquet(all_rows)
        log(f'parquet: {total} total rows, +{added} new')


if __name__ == '__main__':
    main()
