#!/usr/bin/env python3
"""SP-1 one-shot: backfill options_eod.parquet for the cutover gap.

Window: [polygon revocation date + 1] through [yesterday]. Operator runs ONCE
after SP-1 PR deploys and before Monday open. Re-runnable; deduped by the
underlying _append_parquet helper on (date, contract_symbol).

Strategy: per missing date D, enumerate contract symbols from the current
chain (those with expiry >= D existed back then), batch alpaca data option bars
100 at a time for D.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

ALPACA_BIN = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def _chunk(xs: list, n: int) -> Iterable[list]:
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _alpaca(args: list[str]) -> dict:
    res = subprocess.run([ALPACA_BIN] + args, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise RuntimeError(f'alpaca rc={res.returncode}: {res.stderr.strip()}')
    return json.loads(res.stdout)


def enumerate_contracts_for_date(d: date, universe: list[str]) -> list[str]:
    """For each ticker, pull current chain, filter to contracts with expiry >= d."""
    contracts = []
    for ticker in universe:
        try:
            page = _alpaca(['data', 'option', 'chain', '--underlying-symbol', ticker, '--limit', '100'])
            for sym in (page.get('snapshots') or {}).keys():
                # Parse YYMMDD from OCC symbol (root + YYMMDD + C/P + 8-digit strike)
                for i, ch in enumerate(sym):
                    if ch.isdigit():
                        try:
                            yy, mm, dd = sym[i:i+2], sym[i+2:i+4], sym[i+4:i+6]
                            expiry = date(2000 + int(yy), int(mm), int(dd))
                        except (ValueError, IndexError):
                            expiry = None
                        break
                else:
                    expiry = None
                if expiry is not None and expiry >= d:
                    contracts.append(sym)
        except Exception as e:
            log.warning('chain enum failed for %s: %s', ticker, e)
    return contracts


def fetch_bars_batch(symbols: list[str], d: date) -> list[dict]:
    rows = []
    for chunk in _chunk(symbols, 100):
        try:
            payload = _alpaca([
                'data', 'option', 'bars',
                '--symbols', ','.join(chunk),
                '--start', d.isoformat(),
                '--end', d.isoformat(),
                '--timeframe', '1Day',
            ])
            for sym, bars in (payload.get('bars') or {}).items():
                for b in bars:
                    rows.append({
                        'date': d.isoformat(),
                        'contract_symbol': sym,
                        'open': b.get('o'), 'high': b.get('h'),
                        'low': b.get('l'), 'close': b.get('c'),
                        'volume': b.get('v'), 'vwap': b.get('vw'),
                        'transactions': b.get('n'),
                        'data_source': 'alpaca_aat_plus_backfill',
                    })
        except Exception as e:
            log.warning('bars batch fail %s: %s', chunk[:3], e)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-date', required=True, help='YYYY-MM-DD (Polygon revocation date + 1)')
    ap.add_argument('--to-date', required=True, help='YYYY-MM-DD (yesterday)')
    args = ap.parse_args()

    from_d = date.fromisoformat(args.from_date)
    to_d = date.fromisoformat(args.to_date)

    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE ORDER BY ticker")
        universe = [r[0] for r in cur.fetchall()]

    log.info('cutover backfill window=%s..%s universe=%d', from_d, to_d, len(universe))

    d = from_d
    while d <= to_d:
        log.info('backfill date=%s', d)
        contracts = enumerate_contracts_for_date(d, universe)
        log.info('  contracts to fetch: %d', len(contracts))
        rows = fetch_bars_batch(contracts, d)
        log.info('  rows fetched: %d', len(rows))
        if rows:
            from src.pipeline.backfillers.alpaca_options import _append_parquet
            _append_parquet(rows)
        d += timedelta(days=1)

    log.info('cutover backfill complete')


if __name__ == '__main__':
    main()
