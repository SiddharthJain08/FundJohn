#!/usr/bin/env python3
"""SP-5.1b-i live paper smoke — mleg long straddle/strangle end-to-end.

Three sub-smokes:
  D) Long straddle: route a real (marketable) mleg buy, poll to terminal, close
     (held-legs), CONFIRM BOTH legs flat.
  E) Long strangle (target_delta): same.
  F) Gate-OFF: unset OPENCLAW_OPTION_EXEC → _route_option_order returns None for
     a structure order (equity path byte-identical).

PRECONDITIONS:
  - RTH (options RTH-only): alpaca clock is_open == true.
  - .env grep for ALPACA_* (do NOT source — unquoted parens break bash).
  - OPENCLAW_INSTRUMENT_CLASS_ROUTING=1 + OPENCLAW_OPTION_EXEC=1 process-scoped.

SAFETY:
  - D buys 1 ATM SPY straddle (~$24 net x100 ~= $2.4k), E buys 1 OTM strangle
    (~$1-1.5k). Each is bought then IMMEDIATELY closed and FLAT-CONFIRMED.
  - Never trust the ack: poll the open to terminal, poll/verify each leg flat via
    _options_current_qty, re-flatten once, exit non-zero on any residual.

INVOCATION:
  python3 scripts/sp5_1b_smoke.py [--dry-run]
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
                        '2026-06-01-sp5.1b-grounding-snapshot.md')

def log(msg: str):
    ts = dt.datetime.now(dt.UTC).isoformat()
    print(f'[sp5.1b-smoke {ts}] {msg}', flush=True)

def append_snapshot(block: str):
    with open(SNAPSHOT, 'a') as f:
        f.write(block + '\n')

def _structure_order(structure, strike_rule, moneyness=None, underlying='SPY'):
    spec = OptionSpec(
        underlying=underlying, right='call', strike_rule=strike_rule,
        moneyness=moneyness, target_delta=0.30, dte_target=22,
        structure=structure, hedge='none',
    )
    return {
        'ticker': underlying, 'strategy_id': 'sp5_1b_smoke', 'direction': 'long',
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

def _smoke_structure(label, structure, strike_rule, moneyness, dry_run):
    log(f'SMOKE {label}: long {structure} ({strike_rule}) end-to-end')
    order = _structure_order(structure, strike_rule, moneyness)
    coid = f'sp5-1b-{label}-{int(time.time())}'
    equity = 100_000.0
    if dry_run:
        log(f'[dry-run] would route {structure} {strike_rule} coid={coid}')
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
    # Close the held legs
    close_order = dict(order); close_order['close_only'] = True
    log('closing structure via close_only (held-legs)')
    close_res = _route_option_order(close_order, equity=equity, coid=f'{coid}-close')
    log(f'close: {json.dumps({k: (close_res or {}).get(k) for k in ("status","legs","reason")})}')
    # FLAT CONFIRMATION on every leg
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

def smoke_f_gate_off(dry_run):
    log('SMOKE F: gate-OFF verification (structure order)')
    prev = os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    try:
        order = _structure_order('straddle', 'atm')
        res = _route_option_order(order, equity=100_000.0, coid='sp5-1b-F-gate-off')
    finally:
        if prev is not None:
            os.environ['OPENCLAW_OPTION_EXEC'] = prev
    if res is None:
        log('SMOKE F PASS: helper returned None with gate OFF')
        return {'smoke': 'F', 'status': 'ok', 'result': None}
    log(f'SMOKE F FAIL: expected None, got {res}')
    return {'smoke': 'F', 'status': 'fail', 'result': res}

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
    results['D'] = _smoke_structure('D', 'straddle', 'atm', None, args.dry_run)
    results['E'] = _smoke_structure('E', 'strangle', 'target_delta', None, args.dry_run)
    results['F'] = smoke_f_gate_off(args.dry_run)

    block = ['', f'## SP-5.1b smoke results — {dt.datetime.now(dt.UTC).isoformat()}' + (' (dry-run)' if args.dry_run else '')]
    for k in 'DEF':
        block += ['', f'### Smoke {k}', '```', json.dumps(results[k], default=str, indent=2), '```']
    append_snapshot('\n'.join(block))
    log(f'results appended to {SNAPSHOT}')
    all_ok = all(r.get('status') in ('ok', 'dry-run', 'skipped') for r in results.values())
    print('\nSUMMARY:', {k: v.get('status') for k, v in results.items()})
    sys.exit(0 if all_ok else 1)

if __name__ == '__main__':
    main()
