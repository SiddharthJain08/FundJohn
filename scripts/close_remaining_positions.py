#!/usr/bin/env python3
"""close_remaining_positions.py — one-shot RTH flatten with accurate audit.

Why this exists: the daily regime_liquidator fires at 9:01 AM ET pre-open
and submits market closes with `time_in_force=opg`. When the open cross
doesn't match (paper account, low liquidity, or unmatched MOO imbalance),
the orders expire seconds after the open. On 2026-05-18, 214/224 OPG
closes expired this way, leaving the portfolio half-flattened with the
DB still saying `result_status='closed'` because the liquidator marks
status from the submission ack, not the fill outcome.

This script:
  1. Refuses to run unless the market is currently open (RTH).
  2. Iterates every currently-open broker position.
  3. Submits `alpaca position close --symbol-or-asset-id <SYM>` (RTH market
     DAY order — the path Alpaca uses when the market is open).
  4. After all submits, sleeps a few seconds, then polls each resulting
     order until terminal state (or 90s timeout per order).
  5. Writes one `alpaca_liquidations` row per symbol with the REAL outcome
     (filled / partial / pending / failed / rejected) — not just "submitted".
  6. Posts a summary to #trade-reports.

Audit rows tag `regime_from='SCHEDULED_CLEANUP'` so they're distinguishable
from daily regime fires and `MANUAL_FORCE` runs.

DRY-RUN: set `OPENCLAW_CLOSE_REMAINING_DRY_RUN=1` to skip submissions and
just log intent. Live submission requires the same flag as the regular
liquidator: `OPENCLAW_ALPACA_LIVE_LIQUIDATE=1`.

Invocation:
  /usr/bin/python3 scripts/close_remaining_positions.py
  (env: ALPACA_*, POSTGRES_URI; optional REDIS_URL for cooldown read)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)sZ [%(levelname)s] %(message)s',
)
logger = logging.getLogger('close_remaining')

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

POLL_INTERVAL_S = 3
POLL_TIMEOUT_S = 90
TERMINAL_STATUSES = {
    'filled', 'canceled', 'expired', 'rejected', 'replaced',
    'done_for_day', 'failed',
}


def _is_dry_run() -> bool:
    return (os.environ.get('OPENCLAW_CLOSE_REMAINING_DRY_RUN') == '1'
            or os.environ.get('OPENCLAW_ALPACA_LIVE_LIQUIDATE') != '1')


def _run_cli(args, timeout=30):
    """Same shape as regime_liquidator._run_cli."""
    proc = subprocess.run(
        [ALPACA_CLI, *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if proc.returncode == 0:
        try:
            return True, json.loads(proc.stdout), None
        except json.JSONDecodeError:
            return True, proc.stdout, None
    err = {'exit_code': proc.returncode, 'raw_stderr': proc.stderr,
           'status': None, 'error': proc.stderr.strip()}
    try:
        ej = json.loads(proc.stderr)
        err.update({'status': ej.get('status'),
                    'error': ej.get('error') or err['error'],
                    'code': ej.get('code'), 'error_json': ej})
    except json.JSONDecodeError:
        pass
    return False, None, err


def _market_is_open() -> bool:
    ok, payload, _err = _run_cli(['clock'], timeout=10)
    if not ok or not isinstance(payload, dict):
        return False
    return bool(payload.get('is_open'))


def _list_positions() -> list[dict]:
    ok, payload, err = _run_cli(['position', 'list'], timeout=20)
    if not ok or not isinstance(payload, list):
        logger.error('position list failed: %s', (err or {}).get('error'))
        return []
    return payload


def _close_position(symbol: str) -> tuple[bool, dict]:
    ok, payload, err = _run_cli(
        ['position', 'close', '--symbol-or-asset-id', symbol], timeout=20,
    )
    return ok, (payload if ok else (err or {}))


def _get_order(order_id: str) -> dict | None:
    ok, payload, _err = _run_cli(
        ['order', 'get', '--order-id', order_id], timeout=10,
    )
    if ok and isinstance(payload, dict):
        return payload
    return None


def _classify_outcome(order: dict | None, submit_ok: bool) -> str:
    """Translate a final-state order dict (or None) into result_status.

    Returns one of: filled | partial | pending | rejected | submit_error
    """
    if not submit_ok:
        return 'submit_error'
    if order is None:
        return 'pending'
    status = (order.get('status') or '').lower()
    filled_qty = float(order.get('filled_qty') or 0)
    requested = float(order.get('qty') or 0)
    if status == 'filled' or (requested and filled_qty >= requested):
        return 'filled'
    if filled_qty > 0:
        return 'partial'
    if status in {'rejected', 'failed', 'expired', 'canceled', 'done_for_day'}:
        return 'rejected'
    return 'pending'


def _connect_postgres():
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL')
    if not uri:
        logger.warning('POSTGRES_URI not set — skipping audit writes')
        return None
    try:
        import psycopg2
    except ImportError:
        logger.warning('psycopg2 not installed — skipping audit writes')
        return None
    try:
        return psycopg2.connect(uri, connect_timeout=10)
    except Exception as e:
        logger.warning('postgres connect failed: %s — skipping audit writes', e)
        return None


def _record_audit(conn, run_date, symbol, qty, side_closed,
                  close_response, result_status, error_msg):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO alpaca_liquidations
          (run_date, regime_from, regime_to, symbol, qty, side_closed,
           parent_client_order_ids, cancel_results, close_result,
           result_status, error_msg)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (run_date, 'SCHEDULED_CLEANUP', 'SCHEDULED_CLEANUP',
         symbol, qty, side_closed, [], json.dumps({}),
         json.dumps(close_response) if close_response is not None else None,
         result_status, error_msg),
    )
    cur.close()


def _post_discord(channel: str, msg: str) -> bool:
    uri = os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL')
    if not uri:
        return False
    try:
        import psycopg2
        import requests
    except ImportError:
        return False
    url = None
    try:
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT webhook_urls FROM agent_registry WHERE webhook_urls IS NOT NULL"
        )
        for (hooks,) in cur.fetchall():
            if hooks and channel in hooks:
                url = hooks[channel]
                break
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning('webhook lookup failed: %s', e)
        return False
    if not url:
        return False
    try:
        r = requests.post(url, json={'content': msg[:1900]}, timeout=10)
        return bool(r.ok)
    except Exception as e:
        logger.warning('webhook post failed: %s', e)
        return False


def main() -> int:
    dry_run = _is_dry_run()
    mode = 'DRY-RUN' if dry_run else 'LIVE'
    logger.info('close_remaining_positions starting (%s)', mode)

    if not _market_is_open():
        logger.error('market is not open — refusing to submit (use RTH only)')
        return 1

    positions = _list_positions()
    if not positions:
        logger.info('no open positions — nothing to do')
        return 0
    logger.info('found %d open positions', len(positions))

    # Submit closes ───────────────────────────────────────────────────────
    submissions: list[dict] = []
    for p in positions:
        sym = p.get('symbol')
        try:
            qty = float(p.get('qty') or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if not sym or qty == 0:
            continue
        side_closed = 'long_close' if qty > 0 else 'short_close'

        if dry_run:
            logger.info('DRY-RUN would close %s qty=%s', sym, qty)
            submissions.append({'symbol': sym, 'qty': qty,
                                'side_closed': side_closed,
                                'submit_ok': True, 'order_id': None,
                                'response': {'dry_run': True}})
            continue

        ok, payload = _close_position(sym)
        order_id = (payload.get('id') if isinstance(payload, dict) else None)
        submissions.append({'symbol': sym, 'qty': qty,
                            'side_closed': side_closed,
                            'submit_ok': ok, 'order_id': order_id,
                            'response': payload})
        if not ok:
            logger.warning('submit failed for %s: %s', sym,
                           payload.get('error') if isinstance(payload, dict)
                           else payload)

    # Poll for terminal status ────────────────────────────────────────────
    if not dry_run:
        logger.info('sleeping 5s before polling fills')
        time.sleep(5)

    deadline = time.time() + POLL_TIMEOUT_S
    for sub in submissions:
        sub['final_order'] = None
        if dry_run or not sub.get('submit_ok') or not sub.get('order_id'):
            continue
        while time.time() < deadline:
            o = _get_order(sub['order_id'])
            if o is None:
                time.sleep(POLL_INTERVAL_S)
                continue
            sub['final_order'] = o
            status = (o.get('status') or '').lower()
            if status in TERMINAL_STATUSES:
                break
            time.sleep(POLL_INTERVAL_S)

    # Audit + tally ───────────────────────────────────────────────────────
    conn = _connect_postgres()
    run_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    tally = {'filled': 0, 'partial': 0, 'pending': 0,
             'rejected': 0, 'submit_error': 0}
    failed_symbols: list[str] = []
    for sub in submissions:
        final = sub.get('final_order') or sub.get('response') or {}
        outcome = _classify_outcome(sub.get('final_order'), sub.get('submit_ok'))
        tally[outcome] = tally.get(outcome, 0) + 1
        if outcome not in {'filled'}:
            failed_symbols.append(f"{sub['symbol']}({outcome})")
        if conn is not None and not dry_run:
            err_msg = None
            if outcome == 'submit_error':
                err_msg = json.dumps(sub.get('response') or {})[:500]
            try:
                _record_audit(
                    conn, run_date, sub['symbol'], sub['qty'],
                    sub['side_closed'], final, outcome, err_msg,
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning('audit insert failed for %s: %s',
                               sub['symbol'], e)
    if conn is not None:
        conn.close()

    summary = (
        f"close_remaining_positions ({mode}) {run_date}: "
        f"submitted={len(submissions)} "
        f"filled={tally['filled']} partial={tally['partial']} "
        f"pending={tally['pending']} rejected={tally['rejected']} "
        f"submit_error={tally['submit_error']}"
    )
    logger.info(summary)
    if failed_symbols:
        logger.info('non-filled: %s', ', '.join(failed_symbols[:50]))

    if not dry_run:
        msg = summary
        if failed_symbols:
            msg += '\nNon-filled: ' + ', '.join(failed_symbols[:30])
        _post_discord('trade-reports', msg)

    # Exit non-zero if anything didn't fill so the systemd unit shows as failed
    if tally['rejected'] + tally['submit_error'] + tally['pending'] > 0:
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
