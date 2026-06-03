#!/usr/bin/env python3
"""SP-5.2b supervised live paper smoke — credit vertical + iron condor end-to-end.

Three sub-smokes:
  C) Short credit CALL vertical: route a real (marketable) mleg credit order,
     poll to terminal, close (close_only → per-leg), CONFIRM BOTH legs flat.
  D) Iron condor: same flow for a 4-leg condor; flat-verify ALL 4 legs.
  E) Gate-OFF: unset OPENCLAW_OPTION_EXEC → _route_option_order returns None
     for a credit vertical.

PRECONDITIONS:
  - RTH (options RTH-only): alpaca clock is_open == true.
  - .env grep for ALPACA_* (do NOT source — unquoted parens break bash).
  - OPENCLAW_INSTRUMENT_CLASS_ROUTING=1 + OPENCLAW_OPTION_EXEC=1 process-scoped.

SAFETY:
  - C sells 1 SPY call vertical (net credit receipt ~$50-200 x100 = $50-200 total).
    It is submitted then IMMEDIATELY closed (per-leg) and FLAT-CONFIRMED.
  - D sells 1 SPY iron condor (net credit receipt ~$100-350 x100 = $100-350 total).
    It is submitted then IMMEDIATELY closed (per-leg, all 4 legs) and FLAT-CONFIRMED.
  - Never trust the ack: poll mleg to terminal, poll/verify each leg flat via
    _options_current_qty, re-flatten once on residual, exit non-zero on any orphan.
  - NOTE (concern for operator): the per-leg close path (_route_mleg_close) buys
    back the short legs (sell_to_open → buy_to_close). The close path is
    structure-agnostic (reads held OCC positions from the book). Confirm the
    broker accepts the close orders before relying on flat-verification.

INVOCATION:
  python3 scripts/sp5_2b_smoke.py [--dry-run]
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
                        '2026-06-03-sp5.2b-grounding-snapshot.md')

def log(msg: str):
    ts = dt.datetime.now(dt.UTC).isoformat()
    print(f'[sp5.2b-smoke {ts}] {msg}', flush=True)

def append_snapshot(block: str):
    with open(SNAPSHOT, 'a') as f:
        f.write(block + '\n')

def _credit_vertical_order(underlying='SPY'):
    spec = OptionSpec(
        underlying=underlying, right='call', strike_rule='target_delta',
        target_delta=0.25, dte_target=22, structure='credit_vertical',
        spread_width_pct=0.03, hedge='none',
    )
    return {
        'ticker': underlying, 'strategy_id': 'sp5_2b_smoke', 'direction': 'short',
        'instrument_class': 'option', 'contracts': 1.0,
        'option_spec': spec,
    }

def _iron_condor_order(underlying='SPY'):
    spec = OptionSpec(
        underlying=underlying, right='call', strike_rule='target_delta',
        target_delta=0.25, dte_target=22, structure='iron_condor',
        spread_width_pct=0.03, hedge='none',
    )
    return {
        'ticker': underlying, 'strategy_id': 'sp5_2b_smoke', 'direction': 'short',
        'instrument_class': 'option', 'contracts': 1.0,
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

def smoke_c_credit_call_vertical(dry_run):
    label = 'C'
    log(f'SMOKE {label}: short credit call vertical (target_delta=0.25, dte=22, width=3%) end-to-end')
    order = _credit_vertical_order()
    coid = f'sp5-2b-{label}-{int(time.time())}'
    equity = 100_000.0
    if dry_run:
        log(f'[dry-run] would route credit call vertical coid={coid}')
        spec = order['option_spec']
        log(f'[dry-run] spec: underlying={spec.underlying} right={spec.right} '
            f'structure={spec.structure} strike_rule={spec.strike_rule} '
            f'target_delta={spec.target_delta} dte_target={spec.dte_target} '
            f'spread_width_pct={spec.spread_width_pct}')
        log(f'[dry-run] order: direction={order["direction"]} contracts={order["contracts"]}')
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
    # Close the held legs (per-leg close_only; structure-agnostic)
    close_order = dict(order); close_order['close_only'] = True
    log('closing credit vertical via close_only (per-leg)')
    close_res = _route_option_order(close_order, equity=equity, coid=f'{coid}-close')
    log(f'close: {json.dumps({k: (close_res or {}).get(k) for k in ("status","legs","reason")})}')
    # FLAT CONFIRMATION on both legs (short near + long far)
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

def smoke_d_iron_condor(dry_run):
    label = 'D'
    log(f'SMOKE {label}: short iron condor (target_delta=0.25, dte=22, width=3%) end-to-end')
    order = _iron_condor_order()
    coid = f'sp5-2b-{label}-{int(time.time())}'
    equity = 100_000.0
    if dry_run:
        log(f'[dry-run] would route iron condor coid={coid}')
        spec = order['option_spec']
        log(f'[dry-run] spec: underlying={spec.underlying} structure={spec.structure} '
            f'strike_rule={spec.strike_rule} target_delta={spec.target_delta} '
            f'dte_target={spec.dte_target} spread_width_pct={spec.spread_width_pct}')
        log(f'[dry-run] order: direction={order["direction"]} contracts={order["contracts"]}')
        log(f'[dry-run] note: condor = 4 legs (call near/far sell/buy + put near/far sell/buy)')
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
    if len(legs) != 4:
        log(f'SMOKE {label} WARN: expected 4 legs, got {len(legs)}: {legs}')
    log(f'polling mleg {order_id} to terminal...')
    term = _poll_order(order_id, timeout=30.0) or {}
    log(f'terminal: status={term.get("status")} filled_qty={term.get("filled_qty")}')
    # Close all 4 legs (per-leg close_only; structure-agnostic)
    close_order = dict(order); close_order['close_only'] = True
    log('closing iron condor via close_only (per-leg, all 4 legs)')
    close_res = _route_option_order(close_order, equity=equity, coid=f'{coid}-close')
    log(f'close: {json.dumps({k: (close_res or {}).get(k) for k in ("status","legs","reason")})}')
    # FLAT CONFIRMATION on ALL 4 legs
    is_flat, residual = _verify_legs_flat(legs, timeout=20.0)
    if not is_flat:
        log(f'SMOKE {label} WARN: residual {residual} — re-flattening once')
        _route_option_order(close_order, equity=equity, coid=f'{coid}-close2')
        is_flat, residual = _verify_legs_flat(legs, timeout=25.0)
    if not is_flat:
        log(f'SMOKE {label} FAIL: ORPHAN legs {residual} after close+retry — MANUAL FLATTEN REQUIRED')
        return {'smoke': label, 'status': 'fail', 'reason': f'orphan {residual}', 'submit': res}
    log(f'SMOKE {label}: confirmed FLAT on all 4 legs {legs}')
    return {'smoke': label, 'status': 'ok', 'flat': True, 'leg_count': len(legs), 'submit': res, 'terminal': term}

def smoke_e_gate_off(dry_run):
    log('SMOKE E: gate-OFF verification (credit vertical order)')
    prev = os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    try:
        order = _credit_vertical_order()
        res = _route_option_order(order, equity=100_000.0, coid='sp5-2b-E-gate-off')
    finally:
        if prev is not None:
            os.environ['OPENCLAW_OPTION_EXEC'] = prev
    if res is None:
        log('SMOKE E PASS: helper returned None with gate OFF')
        return {'smoke': 'E', 'status': 'ok', 'result': None}
    log(f'SMOKE E FAIL: expected None, got {res}')
    return {'smoke': 'E', 'status': 'fail', 'result': res}

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
    results['C'] = smoke_c_credit_call_vertical(args.dry_run)
    results['D'] = smoke_d_iron_condor(args.dry_run)
    results['E'] = smoke_e_gate_off(args.dry_run)

    block = ['', f'## SP-5.2b smoke results — {dt.datetime.now(dt.UTC).isoformat()}' + (' (dry-run)' if args.dry_run else '')]
    for k in 'CDE':
        block += ['', f'### Smoke {k}', '```', json.dumps(results[k], default=str, indent=2), '```']
    append_snapshot('\n'.join(block))
    log(f'results appended to {SNAPSHOT}')
    all_ok = all(r.get('status') in ('ok', 'dry-run', 'skipped') for r in results.values())
    print('\nSUMMARY:', {k: v.get('status') for k, v in results.items()})
    sys.exit(0 if all_ok else 1)

if __name__ == '__main__':
    main()
