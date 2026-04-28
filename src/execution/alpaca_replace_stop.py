#!/usr/bin/env python3
"""
alpaca_replace_stop.py — replace the stop-loss leg of a live bracket order.

Phase 2.4 of the alpaca-cli integration. Default OFF: the helper logs the
intended replacement and exits without touching live orders. Set
OPENCLAW_ALPACA_LIVE_REPLACE=1 to enable. Live use is intended to be wired
into MastermindJohn's Saturday position-recommendations flow once a
manual review confirms the stop-delta math.

Bracket-order topology:
  parent (type=market, status=filled) → take_profit_leg + stop_loss_leg
  Each leg has its own order_id. Replacing the stop means replacing the
  stop_loss_leg by its order_id, NOT the parent. The CLI handles this via
  `alpaca order replace --order-id <leg_id> --stop-price <new>`.

Public API:
  replace_stop_for_coid(coid, new_stop) → dict
    Looks up the parent order by client_order_id, finds the stop-loss leg,
    issues the replace.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)
ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def _run_cli(args, timeout=30):
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
        err.update({'status': ej.get('status'), 'error': ej.get('error') or err['error'],
                    'code': ej.get('code'), 'error_json': ej})
    except json.JSONDecodeError:
        pass
    return False, None, err


def _is_live() -> bool:
    return os.environ.get('OPENCLAW_ALPACA_LIVE_REPLACE') == '1'


def find_stop_loss_leg(order_payload):
    """Given a parent bracket order JSON, return the stop-loss leg dict
    (with `id`, `stop_price`, etc.) or None if the order has no
    stop-loss leg."""
    if not isinstance(order_payload, dict):
        return None
    legs = order_payload.get('legs') or []
    for leg in legs:
        # Stop-loss leg: type='stop' with a non-null stop_price.
        if (leg.get('type') == 'stop' or leg.get('order_type') == 'stop') \
                and leg.get('stop_price') is not None:
            return leg
        # Some bracket-order shapes report stop-loss as type='stop_limit' too.
        if leg.get('type') == 'stop_limit' and leg.get('stop_price') is not None:
            return leg
    return None


def replace_stop_for_coid(coid: str, new_stop: float) -> dict:
    """Replace the stop-loss leg of the bracket order identified by
    `client_order_id=coid` with `stop_price=new_stop`.

    Returns a dict describing what was attempted. Default mode (env flag
    not set) returns `{'status': 'skipped_dry_run'}` and does NOT issue
    the replace. Live mode shells to `alpaca order replace` and returns
    the broker's order JSON with `{'status': 'replaced'}`.
    """
    new_stop_str = f'{round(float(new_stop), 2):.2f}'
    if not _is_live():
        logger.info('[replace_stop] dry_run: would replace stop on %s → %s',
                    coid, new_stop_str)
        return {'status': 'skipped_dry_run', 'coid': coid, 'new_stop': new_stop_str}

    # 1. Look up the parent order by client_order_id
    ok, parent, err = _run_cli(['order', 'get-by-client-id',
                                '--client-order-id', coid])
    if not ok or not isinstance(parent, dict):
        logger.warning('[replace_stop] get-by-client-id failed for %s: %s',
                       coid, (err or {}).get('error'))
        return {'status': 'lookup_failed', 'coid': coid,
                'error': (err or {}).get('error', 'unknown')}

    # 2. Find the stop-loss leg
    sl_leg = find_stop_loss_leg(parent)
    if not sl_leg:
        return {'status': 'no_stop_loss_leg', 'coid': coid,
                'parent_order_id': parent.get('id')}
    leg_id = sl_leg.get('id')
    if not leg_id:
        return {'status': 'leg_missing_id', 'coid': coid}

    # 3. Replace
    ok2, payload, err2 = _run_cli(['order', 'replace',
                                   '--order-id', leg_id,
                                   '--stop-price', new_stop_str])
    if ok2:
        logger.info('[replace_stop] replaced stop on %s leg=%s → %s',
                    coid, leg_id, new_stop_str)
        return {'status': 'replaced', 'coid': coid, 'leg_id': leg_id,
                'new_stop': new_stop_str, 'response': payload}
    logger.warning('[replace_stop] replace failed for %s: %s',
                   coid, (err2 or {}).get('error'))
    return {'status': 'replace_failed', 'coid': coid, 'leg_id': leg_id,
            'error': (err2 or {}).get('error', 'unknown')}


def _main():
    """CLI entry point so Node callers (position_recommender.js) can shell
    out without re-implementing the gating + lookup logic. Input: --coid
    + --new-stop. Output: a single JSON dict on stdout matching the
    function's return shape. Loads .env automatically."""
    import argparse
    import sys
    try:
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(__file__).resolve().parents[2] / '.env')
    except ImportError:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument('--coid',     required=True)
    ap.add_argument('--new-stop', required=True, type=float)
    args = ap.parse_args()
    result = replace_stop_for_coid(args.coid, args.new_stop)
    print(json.dumps(result))
    # Exit 0 for skipped/replaced (intentional outcomes), non-zero only on
    # replace_failed / lookup_failed so the caller can branch.
    bad = {'replace_failed', 'lookup_failed', 'no_stop_loss_leg', 'leg_missing_id'}
    sys.exit(1 if result.get('status') in bad else 0)


if __name__ == '__main__':
    _main()
