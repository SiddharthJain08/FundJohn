#!/usr/bin/env python3
"""sync_brackets.py — Phase B of the bracket-update plan (2026-05-19).

Diff DB intent (execution_signals.stop_loss) vs live broker bracket leg per
ticker; fire alpaca_replace_stop where they differ by more than the
threshold. Default DRY-RUN. Set OPENCLAW_DAILY_BRACKET_REPLACE=1 to fire
live replacements.

The flow:
  1. alpaca position list      → tickers actually held + side
  2. alpaca_submissions latest → parent client_order_id per (strategy_id,
                                  ticker) that established the live bracket
  3. execution_signals (open)  → current DB intent for stop_loss
  4. alpaca order get <coid>   → live parent + child legs; read the
                                  stop-leg's current stop_price
  5. diff DB vs broker → if |Δ| / entry > 5 bps AND gate is ON, call
     alpaca_replace_stop.replace_stop_for_coid(coid, new_stop)

Why per (strategy_id, ticker), not per ticker:
  The alpaca_submissions row carries the strategy_id of whichever
  strategy's signal led to that submission (the direction-leader pick
  in the regime_blended sizer). The bracket leg attached at the broker
  belongs to that strategy. Other strategies signaling the same ticker
  share the position but the parent bracket is owned by the leader.
  We replace based on the leader's current intent — same strategy whose
  bracket geometry was used at entry.

Exit codes:
  0 — success (zero or more replacements applied, all dry-run OR all
      live replacements succeeded)
  1 — partial failure (some replacements failed); details in stdout
  2 — fatal (POSTGRES_URI missing, alpaca CLI failed, etc.)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)sZ [%(levelname)s] %(message)s',
)
logger = logging.getLogger('sync_brackets')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')
# 5 basis points threshold — below this the difference is noise (quote
# drift, rounding). Above, the strategy's intent meaningfully shifted.
REPLACE_THRESHOLD_BPS = 5.0


def _alpaca_call(args, timeout=30):
    """Call alpaca CLI, return parsed payload or None on failure."""
    try:
        proc = subprocess.run(
            [ALPACA_CLI, *args], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        if proc.returncode != 0:
            logger.warning('alpaca %s exit=%d stderr=%s',
                           ' '.join(args), proc.returncode,
                           (proc.stderr or '').strip()[:200])
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            logger.warning('alpaca %s returned non-JSON: %s',
                           ' '.join(args), proc.stdout[:200])
            return None
    except subprocess.TimeoutExpired:
        logger.warning('alpaca %s timed out', ' '.join(args))
        return None


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true',
                        help='Force dry-run regardless of gate (default if '
                             'OPENCLAW_DAILY_BRACKET_REPLACE unset/0)')
    args = parser.parse_args(argv)

    gate_on = os.environ.get('OPENCLAW_DAILY_BRACKET_REPLACE') == '1'
    dry_run = args.dry_run or not gate_on
    mode = 'DRY-RUN' if dry_run else 'LIVE'
    logger.info('sync_brackets %s mode (gate OPENCLAW_DAILY_BRACKET_REPLACE=%s)',
                mode, '1' if gate_on else 'off')

    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        logger.error('POSTGRES_URI not set')
        return 2

    import psycopg2
    import psycopg2.extras

    # 1. broker positions: ticker → side
    positions = _alpaca_call(['position', 'list'])
    if not isinstance(positions, list):
        logger.error('alpaca position list returned non-list — aborting')
        return 2
    broker_tickers = {p['symbol'] for p in positions if p.get('symbol')}
    logger.info('broker holds %d positions', len(broker_tickers))

    if not broker_tickers:
        logger.info('nothing to sync')
        return 0

    # 2. + 3. join alpaca_submissions ↔ execution_signals for the bracket-leader
    #    per (strategy_id, ticker). Take the most recent submission per ticker
    #    as the parent COID; pull current DB stop_loss from the matching
    #    execution_signals open row.
    with psycopg2.connect(pg_uri) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                WITH latest_sub AS (
                  SELECT DISTINCT ON (ticker)
                         ticker, strategy_id, client_order_id,
                         stop_price AS submitted_stop,
                         entry_price AS submitted_entry,
                         direction, submitted_at
                    FROM alpaca_submissions
                   WHERE client_order_id IS NOT NULL
                   ORDER BY ticker, submitted_at DESC
                )
                SELECT ls.ticker, ls.strategy_id, ls.client_order_id,
                       ls.submitted_stop, ls.submitted_entry, ls.direction,
                       es.stop_loss      AS db_stop,
                       es.entry_price    AS db_entry,
                       es.id             AS sig_id
                  FROM latest_sub ls
                  LEFT JOIN execution_signals es
                    ON es.strategy_id = ls.strategy_id
                   AND es.ticker      = ls.ticker
                   AND es.status      = 'open'
                 WHERE ls.ticker = ANY(%s)
            """, (list(broker_tickers),))
            rows = cur.fetchall()

    logger.info('candidates: %d (held by broker AND had a bracket submission)', len(rows))

    n_no_db_intent = 0
    n_no_drift     = 0
    n_replace      = 0
    n_failed       = 0
    n_skipped_dry  = 0

    # Lazy import — alpaca_replace_stop reads env at call time.
    from execution.alpaca_replace_stop import replace_stop_for_coid

    for r in rows:
        ticker = r['ticker']
        coid   = r['client_order_id']
        db_stop  = r['db_stop']
        sub_stop = r['submitted_stop']
        entry    = r['db_entry'] or r['submitted_entry']
        if db_stop is None:
            n_no_db_intent += 1
            continue
        if entry is None or float(entry) <= 0:
            continue
        # Compute drift as fraction of entry price (basis points).
        delta_bps = abs(float(db_stop) - float(sub_stop or 0)) / float(entry) * 1e4
        if delta_bps < REPLACE_THRESHOLD_BPS:
            n_no_drift += 1
            continue
        logger.info('%s %s: db_stop=%.4f submitted_stop=%.4f Δ=%.1f bps  coid=%s',
                    ticker, r['strategy_id'], float(db_stop),
                    float(sub_stop or 0), delta_bps, coid)
        if dry_run:
            n_skipped_dry += 1
            continue
        result = replace_stop_for_coid(coid, float(db_stop))
        if result.get('ok'):
            logger.info('  ✓ replaced stop-leg id=%s new_stop=%.4f',
                        result.get('stop_leg_id'), float(db_stop))
            n_replace += 1
        else:
            logger.warning('  ✗ replace failed: %s', result.get('error'))
            n_failed += 1

    logger.info('summary: candidates=%d no_db_intent=%d no_drift=%d '
                'dry_run_skipped=%d replaced=%d failed=%d',
                len(rows), n_no_db_intent, n_no_drift, n_skipped_dry,
                n_replace, n_failed)
    return 1 if n_failed else 0


if __name__ == '__main__':
    sys.exit(main())
