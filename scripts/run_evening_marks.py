#!/usr/bin/env python3
"""Evening measurement pass — parity marks, ledger, live-days, stale trackers.

WHY (2026-07-29 same-day pivot): these five passes run inside the engine's
`signals` step. Under the EOD flow that step fired at 16:15 ET — AFTER the
15:55 fills — so a day's fills were marked the same evening. Under same-day
execution the signals step runs at 15:00 ET, BEFORE the 15:55 fills, so the
pass sees no fills for today and each day's measurement lags by one cycle
(verified live: 123 signals still lifecycle=COMPUTED after the first same-day
execution). This script runs the identical pass in the 16:15 ET slot, after
the fills, restoring same-evening measurement.

Deliberately NOT a signal producer: it never calls run_strategies or
write_signals. The 15:00 chain remains the sole source of signals.

Closes are taken from the master price panel's latest bar per ticker —
the same construction the engine uses (last non-NaN close per column).

Run: python3 scripts/run_evening_marks.py [--date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date as _date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('evening_marks')

WORKSPACE = os.environ.get('OPENCLAW_WORKSPACE', 'default')


def _closes_for(universe_hint=None) -> dict:
    """{ticker: latest close} from the engine's own price loader, so the mark
    prices are identical to what the signals-step pass would have used."""
    from execution.engine import load_prices, load_approved_strategies  # noqa: F401
    import pandas as pd  # noqa: F401
    prices = load_prices(universe_hint or [])
    closes: dict = {}
    if prices is not None and not prices.empty:
        for tk in prices.columns:
            ts = prices[tk].dropna()
            if not ts.empty:
                closes[tk] = float(ts.iloc[-1])
    return closes


WEBHOOK_HELPER = ROOT / 'src' / 'agent' / 'curators' / '_discord_webhook.js'


def _post_discord(text: str, channel: str = 'data-alerts') -> None:
    """Non-fatal Discord post via the Node webhook helper."""
    if not WEBHOOK_HELPER.exists():
        return
    import subprocess
    js = ("const { postToChannel } = require(%r);"
          "let c=[];process.stdin.on('data',d=>c.push(d));"
          "process.stdin.on('end',async()=>{"
          "await postToChannel('botjohn',%r,Buffer.concat(c).toString('utf8'));});"
          ) % (str(WEBHOOK_HELPER), channel)
    try:
        subprocess.run(['node', '-e', js], input=text.encode('utf-8'),
                       capture_output=True, timeout=30, env={**os.environ})
    except Exception as exc:  # noqa: BLE001
        logger.warning('discord post failed: %s', exc)


def _grade_tier1_ingest() -> None:
    """Grade the 14:30 tier-1 acting-set ingest and alert if it under-delivered.

    The engine's overlay injection falls OPEN by design — a thin or missing
    overlay silently serves yesterday's options surface rather than blocking
    the cycle. Something has to observe that, and the only weekday
    system_checks run is openclaw-botjohn-maintenance at 12:00 ET, three
    hours BEFORE the ingest it would grade. This slot is the first one after
    the fact, so the operator learns the same evening rather than after a
    second stale day."""
    try:
        from system_checks.checks.acting_ingest_coverage import _acting_ingest_coverage
        from system_checks.types import Status
        status, detail = _acting_ingest_coverage()
        logger.info('tier-1 ingest grade: %s — %s', status, detail)
        if status is not Status.PASS:
            _post_discord(f'⚠️ tier-1 acting-set ingest {getattr(status, "name", status)} '
                          f'— today\'s acting strategies may have decided on the '
                          f'previous EOD collect.\n{detail}')
    except Exception as exc:  # noqa: BLE001
        logger.warning('tier-1 ingest grading skipped: %s', exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=None, help='run date (default: today)')
    args = ap.parse_args()
    run_date = _date.fromisoformat(args.date) if args.date else _date.today()

    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        logger.error('POSTGRES_URI not set; aborting')
        return 2

    import psycopg2
    import psycopg2.extras
    from execution.parity_mark import (finalize_parity_marks,
                                       finalize_execution_ledger,
                                       backfill_broker_fill_truth,
                                       refresh_live_days,
                                       close_stale_trackers)

    # The close-proxy injection is a SIGNAL-path device (it fabricates a
    # today-dated row from a ~15:00 snapshot). Marks must use real closes, so
    # it is explicitly disabled for this pass regardless of the live flag.
    os.environ['OPENCLAW_CLOSE_PROXY_SNAPSHOT'] = '0'

    closes = _closes_for()
    if not closes:
        logger.error('no closes available — refusing to mark against an empty panel')
        return 1
    logger.info('closes loaded for %d tickers', len(closes))

    conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur = conn.cursor()
        n_marks = finalize_parity_marks(cur, closes, run_date, WORKSPACE)
        logger.info('parity marks finalized: %s', n_marks)
        n_ledger = finalize_execution_ledger(cur, closes, run_date)
        logger.info('execution ledger finalized: %s', n_ledger)
        backfill_broker_fill_truth(cur, run_date, WORKSPACE)
        n_live = refresh_live_days(cur)
        logger.info('live_days refreshed: %s', n_live)
        try:
            from execution.regime_blended_sizer import _load_broker_positions_usd
            held = _load_broker_positions_usd()
            close_stale_trackers(
                cur, held_tickers=(set(held) if held is not None else None))
        except Exception as exc:  # noqa: BLE001
            logger.warning('stale-tracker pass skipped: %s', exc)
        conn.commit()
        logger.info('evening marks committed for %s', run_date)
        # After the commit: grading must never be able to roll back the marks.
        _grade_tier1_ingest()
        return 0
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        logger.error('evening marks FAILED (rolled back): %s', exc)
        return 1
    finally:
        conn.close()


if __name__ == '__main__':
    raise SystemExit(main())
