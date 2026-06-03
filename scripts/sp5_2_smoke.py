#!/usr/bin/env python3
"""SP-5.2 supervised live paper smoke — debit call vertical end-to-end.

Two sub-smokes:
  V) Long debit CALL vertical: route a real (marketable) mleg buy, poll to
     terminal, close (close_only → per-leg), CONFIRM BOTH legs flat.
  W) Gate-OFF: unset OPENCLAW_OPTION_EXEC → _route_option_order returns None
     for a vertical.

PRECONDITIONS:
  - RTH (options RTH-only): alpaca clock is_open == true.
  - .env grep for ALPACA_* (do NOT source — unquoted parens break bash).
  - OPENCLAW_INSTRUMENT_CLASS_ROUTING=1 + OPENCLAW_OPTION_EXEC=1 process-scoped.

SAFETY:
  - V buys 1 ATM SPY call vertical (~$100-200 net debit x100 ~= $150-200 total).
    It is bought then IMMEDIATELY closed (per-leg) and FLAT-CONFIRMED.
  - Never trust the ack: poll the open to terminal, poll/verify each leg flat via
    _options_current_qty, re-flatten once, exit non-zero on any residual.
  - NOTE (concern for operator): the per-leg close path (_route_mleg_close) calls
    per-leg `position close` — it buys back the short far leg. This is the first
    smoke to exercise closing a short option leg; confirm broker accepts it.

INVOCATION:
  python3 scripts/sp5_2_smoke.py [--dry-run]
"""
from __future__ import annotations
import os, sys, time, json, argparse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, 'src'))

def _load_alpaca_env():
    keys = ('ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'ALPACA_BASE_URL', 'POSTGRES_URI')
    with open(os.path.join(REPO, '.env')) as f:
        for line in f:
            line = line.strip()
            for k in keys:
                if line.startswith(f'{k}='):
                    os.environ[k] = line.split('=', 1)[1].strip().strip('"\'')
                    break
_load_alpaca_env()

from execution.alpaca_executor import (
    _route_option_order, _run_alpaca_cli, _options_current_qty,
)
from strategies.base import OptionSpec  # noqa: F401

SNAPSHOT = os.path.join(REPO, 'docs', 'superpowers', 'specs',
                        '2026-06-03-sp5.2-grounding-snapshot.md')

def log(msg: str):
    ts = dt.datetime.now(dt.UTC).isoformat()
    print(f'[sp5.2-smoke {ts}] {msg}', flush=True)

def append_snapshot(block: str):
    with open(SNAPSHOT, 'a') as f:
        f.write(block + '\n')

def _vertical_order(underlying='SPY'):
    spec = OptionSpec(
        underlying=underlying, right='call', strike_rule='atm', dte_target=22,
        structure='vertical', spread_width_pct=0.03, hedge='none',
    )
    return {
        'ticker': underlying, 'strategy_id': 'sp5_2_smoke', 'direction': 'long',
        'instrument_class': 'option', 'contracts': 1.0, 'notional_usd': 500.0,
        'option_spec': spec,
    }

def _poll_order(order_id: str, timeout: float = 30.0, poll: float = 1.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, payload, _ = _run_alpaca_cli(['order', 'get', '--order-id', order_id])
        if ok and payload:
            status = (payload.get('status') or '').lower()
            if status in ('filled', 'canceled', 'expired', 'rejected', 'done_for_day'):
                return payload
        time.sleep(poll)
    return None

def _verify_legs_flat(legs, timeout: float = 20.0, poll: float = 1.5):
    """Poll until every leg OCC has qty 0. Returns (all_flat, {occ: qty})."""
    deadline = time.time() + timeout
    residual = {occ: _options_current_qty(occ) for occ in legs}
    while any(q != 0 for q in residual.values()) and time.time() < deadline:
        time.sleep(poll)
        residual = {occ: _options_current_qty(occ) for occ in legs}
    return (all(q == 0 for q in residual.values()), residual)

def _confirm_rth():
    ok, payload, _ = _run_alpaca_cli(['clock'])
    if not ok or not payload:
        log('ABORT: cannot fetch alpaca clock'); sys.exit(2)
    if not payload.get('is_open'):
        log(f"ABORT: market closed (next_open={payload.get('next_open')})"); sys.exit(1)

def smoke_v_debit_call_vertical(dry_run):
    label = 'V'
    log(f'SMOKE {label}: long debit call vertical (atm, dte=22, width=3%) end-to-end')
    order = _vertical_order()
    coid = f'sp5-2-{label}-{int(time.time())}'
    equity = 100_000.0
    if dry_run:
        log(f'[dry-run] would route vertical call spread coid={coid}')
        spec = order['option_spec']
        log(f'[dry-run] spec: underlying={spec.underlying} right={spec.right} '
            f'structure={spec.structure} strike_rule={spec.strike_rule} '
            f'dte_target={spec.dte_target} spread_width_pct={spec.spread_width_pct}')
        log(f'[dry-run] order: contracts={order["contracts"]} notional_usd={order["notional_usd"]}')
        return {'smoke': label, 'status': 'dry-run'}
    res = _route_option_order(order, equity=equity, coid=coid)
    if res is None:
        return {'smoke': label, 'status': 'fail', 'reason': 'helper returned None'}
    log(f'submit: {json.dumps({k: res.get(k) for k in ("ticker","structure","status","order_id","qty","entry","legs","reason")})}')
    if res.get('status') == 'skipped':
        return {'smoke': label, 'status': 'skipped', 'reason': res.get('reason'), 'result': res}
    order_id, legs = res.get('order_id'), res.get('legs') or []
    if not order_id or not legs:
        return {'smoke': label, 'status': 'fail', 'reason': 'no order_id/legs', 'result': res}
    log(f'polling mleg {order_id} to terminal...')
    term = _poll_order(order_id, timeout=30.0) or {}
    log(f'terminal: status={term.get("status")} filled_qty={term.get("filled_qty")}')
    # Close the held legs (per-leg close_only; no net-credit package needed)
    close_order = dict(order); close_order['close_only'] = True
    log('closing vertical via close_only (per-leg)')
    close_res = _route_option_order(close_order, equity=equity, coid=f'{coid}-close')
    log(f'close: {json.dumps({k: (close_res or {}).get(k) for k in ("status","legs","reason")})}')
    # FLAT CONFIRMATION on every leg (buy leg + short far leg)
    is_flat, residual = _verify_legs_flat(legs, timeout=20.0)
    if not is_flat:
        log(f'SMOKE {label} WARN: residual {residual} — re-flattening once')
        _route_option_order(close_order, equity=equity, coid=f'{coid}-close2')
        is_flat, residual = _verify_legs_flat(legs, timeout=25.0)
    if not is_flat:
        log(f'SMOKE {label} FAIL: ORPHAN legs {residual} after close+retry — MANUAL FLATTEN REQUIRED')
        return {'smoke': label, 'status': 'fail', 'reason': f'orphan {residual}', 'submit': res}
    log(f'SMOKE {label}: confirmed FLAT on all legs {legs}')
    return {'smoke': label, 'status': 'ok', 'flat': True, 'submit': res, 'terminal': term}

def smoke_w_gate_off(dry_run):
    log('SMOKE W: gate-OFF verification (vertical order)')
    prev = os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    try:
        order = _vertical_order()
        res = _route_option_order(order, equity=100_000.0, coid='sp5-2-W-gate-off')
    finally:
        if prev is not None:
            os.environ['OPENCLAW_OPTION_EXEC'] = prev
    if res is None:
        log('SMOKE W PASS: helper returned None with gate OFF')
        return {'smoke': 'W', 'status': 'ok', 'result': None}
    log(f'SMOKE W FAIL: expected None, got {res}')
    return {'smoke': 'W', 'status': 'fail', 'result': res}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if not args.dry_run:
        _confirm_rth()
        if os.environ.get('OPENCLAW_INSTRUMENT_CLASS_ROUTING') != '1':
            log('ABORT: OPENCLAW_INSTRUMENT_CLASS_ROUTING != 1'); sys.exit(2)
        if os.environ.get('OPENCLAW_OPTION_EXEC') != '1':
            log('ABORT: OPENCLAW_OPTION_EXEC != 1'); sys.exit(2)

    results = {}
    results['V'] = smoke_v_debit_call_vertical(args.dry_run)
    results['W'] = smoke_w_gate_off(args.dry_run)

    block = ['', f'## SP-5.2 smoke results — {dt.datetime.now(dt.UTC).isoformat()}' + (' (dry-run)' if args.dry_run else '')]
    for k in 'VW':
        block += ['', f'### Smoke {k}', '```', json.dumps(results[k], default=str, indent=2), '```']
    append_snapshot('\n'.join(block))
    log(f'results appended to {SNAPSHOT}')
    all_ok = all(r.get('status') in ('ok', 'dry-run', 'skipped') for r in results.values())
    print('\nSUMMARY:', {k: v.get('status') for k, v in results.items()})
    sys.exit(0 if all_ok else 1)

if __name__ == '__main__':
    main()
