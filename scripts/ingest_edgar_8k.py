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

# Per-run ticker cap. Was 50 until 2026-08-23, which silently dropped ~80% of
# a ~270-name open book on every premarket run ("truncating 271 tickers to
# max 50"). EDGARClient throttles itself to 10 req/s (one submissions call per
# ticker + one document fetch per NEW filing), so a 400-name book is ~40-60s
# per run — well inside the premarket window; the unit has no RuntimeMaxSec.
DEFAULT_MAX_TICKERS_PER_RUN = 400


def _max_tickers_per_run() -> int:
    return int(os.environ.get('OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN',
                              str(DEFAULT_MAX_TICKERS_PER_RUN)))


def _cap_tickers(tickers: list[str], max_n: int) -> list[str]:
    """Truncate to max_n, WARNing with the dropped count (never silent)."""
    if len(tickers) <= max_n:
        return tickers
    dropped = len(tickers) - max_n
    log.warning(
        'truncating %d tickers to max %d — dropped %d names this run '
        '(raise OPENCLAW_EDGAR_8K_MAX_TICKERS_PER_RUN to cover the full book)',
        len(tickers), max_n, dropped,
    )
    return tickers[:max_n]


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

    tickers = _cap_tickers(tickers, _max_tickers_per_run())

    results = asyncio.run(ingest_8k_filings(tickers, args.lookback_hours))
    total_new = sum(results.values())
    log.info('ingested 8-Ks: total new=%d, per-ticker=%s', total_new, results)

    # Discord post — only when something new was ingested (skip silent days).
    if total_new > 0:
        try:
            from src.execution.pipeline_orchestrator import post_channel
            ch = os.environ.get('OPENCLAW_EDGAR_DISCORD_WEBHOOK_NAME', 'pre-market-alerts')
            hits = sorted([(t, n) for t, n in results.items() if n > 0],
                          key=lambda x: -x[1])
            lines = [f'**EDGAR 8-K ingest** — {total_new} new filing(s) across {len(hits)} ticker(s)']
            lines += [f'  · {t}: {n}' for t, n in hits[:20]]
            if len(hits) > 20:
                lines.append(f'  · …and {len(hits) - 20} more')
            post_channel(ch, '\n'.join(lines))
        except Exception as e:
            log.warning('discord post failed (non-fatal): %s', e)

    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
