#!/usr/bin/env python3
"""SP-1 one-shot: backfill options_eod.parquet for the cutover gap.

Window: [polygon revocation date + 1] through [yesterday]. Operator runs ONCE
after SP-1 PR deploys and before Monday open. Re-runnable; deduped by the
underlying _append_parquet helper on (date, contract_symbol).

Refactored 2026-05-22 (SP-2 Phase B Task 9) — now a thin wrapper around
`scripts/backfill_universe_5y.py --target options`. Original CLI surface
(`--from-date`, `--to-date`) preserved for operator muscle-memory. The
universe is loaded from `universe_config WHERE active=TRUE` here (the
driver's default reads `data/.backfill_universe_v1.txt` which is the 5y
backfill universe, not the live tradeable universe) and passed explicitly
via `--tickers`.

Strategy: per missing date D, enumerate contract symbols from the current
chain (those with expiry >= D existed back then), batch alpaca data option bars
100 at a time for D. Each (date, ticker) is one chunk in the audit + Redis
checkpoint stream.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_active_universe() -> list[str]:
    """Read the live tradeable universe from universe_config.

    Matches the legacy cutover-gap behavior (the driver's default reads a
    different file — the 5y backfill universe — which would silently widen
    or narrow what gets backfilled).
    """
    import psycopg2
    with psycopg2.connect(os.environ['POSTGRES_URI']) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ticker FROM universe_config WHERE active = TRUE ORDER BY ticker"
        )
        return [r[0] for r in cur.fetchall()]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description='SP-1 cutover-gap options backfill (thin wrapper around '
                    'backfill_universe_5y.py --target options).',
    )
    ap.add_argument('--from-date', required=True,
                    help='YYYY-MM-DD (Polygon revocation date + 1)')
    ap.add_argument('--to-date', required=True,
                    help='YYYY-MM-DD (yesterday)')
    ap.add_argument('--tickers', default=None,
                    help='Comma-separated ticker override (default: '
                         'all active rows in universe_config).')
    ap.add_argument('--dry-run', action='store_true',
                    help='Plan + audit only; no parquet writes.')
    args = ap.parse_args(argv)

    if args.tickers:
        tickers = args.tickers
    else:
        tickers = ','.join(_load_active_universe())
        if not tickers:
            sys.stderr.write('ERROR: universe_config empty — pass --tickers explicitly.\n')
            sys.exit(2)

    driver_argv = [
        '--target', 'options',
        '--tickers', tickers,
        '--start-date', args.from_date,
        '--end-date', args.to_date,
    ]
    if args.dry_run:
        driver_argv.append('--dry-run')

    # Late import: keeps `--help` fast and avoids loading psycopg2 paths at
    # import time of the wrapper.
    import backfill_universe_5y as driver
    driver.main(driver_argv)


if __name__ == '__main__':
    main()
