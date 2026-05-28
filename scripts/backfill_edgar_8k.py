#!/usr/bin/env python3
"""One-shot 7-day EDGAR 8-K backfill.

Operator-invoked. Does NOT honor OPENCLAW_EDGAR_8K_INGEST (the operator
is explicitly running the script; that's authorization enough).

Usage:
    python3 -m scripts.backfill_edgar_8k --days 7
    python3 -m scripts.backfill_edgar_8k --days 30 --tickers GLW AAPL MSFT
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from src.ingestion.edgar_8k import ingest_8k_filings
from src.pipeline.premarket_helpers import load_open_equity_positions


log = logging.getLogger(__name__)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--tickers', nargs='*',
                        help='override held-position lookup; mostly for backfilling '
                             'a known target like GLW for the post-mortem')
    args = parser.parse_args(argv)

    if args.tickers:
        tickers = args.tickers
    else:
        positions = load_open_equity_positions()
        tickers = [p['symbol'] for p in positions]

    if not tickers:
        log.warning('no tickers — nothing to backfill')
        return 0

    log.info('backfilling %d tickers, lookback=%dd: %s',
             len(tickers), args.days, tickers)
    results = asyncio.run(ingest_8k_filings(tickers, args.days * 24))
    total_new = sum(results.values())
    log.info('backfill complete: total new=%d, per-ticker=%s', total_new, results)
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
