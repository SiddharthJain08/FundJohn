#!/usr/bin/env python3
"""Pre-market news fetch — puts overnight articles in scope before the gate.

Runs shortly before the 09:15 ET pre-market gate (`src/execution/premarket_gate.py`)
and fetches raw Alpaca news into `market_news` for exactly the tickers that gate
is about to score.

THE DEFECT THIS CLOSES (measured 2026-07-31)
--------------------------------------------
`market_news` is written by `ingest_alpaca_news()`, called only from
`run_sentiment_step.py` stage 6b — i.e. inside the 15:00/16:15 ET compute chain,
always AFTER the 13:15Z gate. Fetch clock-time by day:

    pre-pivot  (compute 16:15 ET):  21:20Z 21:29Z 21:22Z 21:20Z
    post-pivot (compute 15:00 ET):  19:00:45Z  19:00:43Z

The gate's 18h window opens at 19:15Z the prior day. The old 21:20Z fetch landed
just after that floor, so the gate saw a ~2h sliver of the previous evening
(24–40 rows). The same-day pivot moved the fetch to 19:00Z — 15 minutes BEFORE
the floor — so the window became structurally empty: 0 rows on 07-29, 07-30 and
07-31, every subject APPROVED fail-open on no data.

Note what that means historically: the gate has NEVER seen genuine overnight or
pre-market news. The feed carries it (published_at histogram over 14d shows a
real pre-market ramp — 09Z 75, 10Z 78, 11Z 112, 12Z 174 rows) but those articles
were always fetched ~6h later at the next compute. This job is the first time
they are available when the gate runs.

SUBJECT SCOPING
---------------
Mirrors the gate's own subject resolution rather than re-deriving it:
  * same-day protect mode -> the HELD BOOK (4 names on 07-30) = one 50-symbol
    chunk, seconds.
  * otherwise             -> today's COMPUTED signal register (~100 tickers).

Writes ONLY `market_news` (see ingest_market_news_only): the full ingest also
stamps `ticker_sentiment_daily.alpaca_news_*` for EVERY requested symbol
including article-less ones, which scoped to the held book would publish zeros
hours before the full-universe run overwrites them.

Fail-open by construction: any error exits 0. This job must never be able to
stop the gate from running — a gate on stale news is the status quo, a gate that
did not run is strictly worse.

Usage: python3 scripts/run_premarket_news.py [--hours 24] [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
log = logging.getLogger('premarket_news')


def _subjects() -> tuple[list[str], str]:
    """(tickers, mode_label) for whatever the gate is about to score."""
    from execution import premarket_gate as pg

    if pg.sameday_protect_mode():
        held = pg._load_held_subjects()
        return sorted({s['ticker'] for s in held}), 'sameday-hold(book)'

    conn = pg._get_db_conn()
    try:
        cur = conn.cursor()
        target_date = datetime.now(timezone.utc).date()
        signals = pg._load_carried_signals(cur, target_date)
        return sorted({s['ticker'] for s in signals}), 'signal-register'
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--hours', type=int, default=24,
                    help='lookback for the Alpaca fetch (default 24)')
    ap.add_argument('--dry-run', action='store_true',
                    help='resolve subjects and exit without fetching')
    args = ap.parse_args()

    from execution import premarket_gate as pg
    if not pg.is_enabled():
        log.info('%s not set — gate disabled, nothing to pre-fetch for',
                 pg.ENV_GATE)
        return 0

    tickers, mode = _subjects()
    if not tickers:
        log.warning('no gate subjects resolved (mode=%s) — nothing to fetch. '
                    'The gate will have no subjects either.', mode)
        return 0
    log.info('mode=%s subjects=%d %s', mode, len(tickers),
             tickers if len(tickers) <= 25 else f'{tickers[:25]}…')

    if args.dry_run:
        log.info('dry-run — no fetch')
        return 0

    from ingestion.alpaca_news import ingest_market_news_only
    inserted = ingest_market_news_only(tickers, hours=args.hours)
    log.info('done: %d new market_news row(s) for %d subject(s)',
             inserted, len(tickers))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # Fail-open: never let a news-fetch failure block the gate that runs
        # minutes later.
        log.error('pre-market news fetch failed (non-fatal, gate still runs): '
                  '%s', exc, exc_info=True)
        sys.exit(0)
