#!/usr/bin/env python3
"""SEC EDGAR 8-K ingester CLI entry point.

Reads currently-held equity positions, calls the ingester, exits 0
on success. Master gate OPENCLAW_EDGAR_8K_INGEST=1; otherwise no-op.

Usage:
    python3 -m scripts.ingest_edgar_8k --lookback-hours 24
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from src.ingestion.edgar_8k import ingest_8k_filings
from src.pipeline.premarket_helpers import (
    is_trading_day_in_et,
    load_open_equity_positions,
)


log = logging.getLogger(__name__)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--lookback-hours', type=int, default=24)
    parser.add_argument('--tickers', nargs='*',
                        help='override held-position lookup (debugging only)')
    args = parser.parse_args(argv)

    if os.environ.get('OPENCLAW_EDGAR_8K_INGEST', '0') != '1':
        log.info('OPENCLAW_EDGAR_8K_INGEST=0; exiting silently')
        return 0

    if not is_trading_day_in_et():
        log.info('not a trading day in ET; exiting silently')
        return 0

    if args.tickers:
        tickers = args.tickers
    else:
        positions = load_open_equity_positions()
        tickers = [p['symbol'] for p in positions]

    if not tickers:
        log.info('no tickers to ingest; exiting')
        return 0

    max_n = int(os.environ.get('OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN', '50'))
    if len(tickers) > max_n:
        log.warning('truncating %d tickers to max %d', len(tickers), max_n)
        tickers = tickers[:max_n]

    results = asyncio.run(ingest_8k_filings(tickers, args.lookback_hours))
    total_new = sum(results.values())
    log.info('ingested 8-Ks: total new=%d, per-ticker=%s', total_new, results)
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
