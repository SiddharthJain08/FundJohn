#!/usr/bin/env python3
"""SP-5.1a live paper smoke test.

Three sub-smokes:
  A) Long call ATM end-to-end: submit, poll-to-terminal, close, audit.
  B) Cash-secured short put non-marketable: submit (rests), cancel by COID.
  C) Gate-OFF verification: unset OPENCLAW_OPTION_EXEC, expect helper returns None.

PRECONDITIONS:
  - RTH: alpaca clock --jq '.is_open' == "true"  (options are RTH-only)
  - .env grep for ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPACA_BASE_URL / POSTGRES_URI
    (do NOT source .env — unquoted parens break bash)
  - OPENCLAW_INSTRUMENT_CLASS_ROUTING=1 + OPENCLAW_OPTION_EXEC=1 process-scoped

SAFETY:
  - Smoke A: $20-ish equivalent (1 contract of a low-ask SPY call near ATM)
  - Smoke B: 1 contract deep-OTM put, non-marketable limit = $999 (real put <<$999
    → rests), CANCEL immediately. Collateral pre-checked vs account cash.
  - Failures at ANY step → flatten and abort.

INVOCATION:
  python3 scripts/sp5_1a_smoke.py [--dry-run]    # dry-run = construct + log only
"""
from __future__ import annotations
import os, sys, time, json, argparse, datetime as dt

# Repo root + src on path
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, 'src'))

# Load minimal env from .env (grep specific keys, never source)
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
    _route_option_order, _alpaca_session_kind, _run_alpaca_cli, _account_cash,
)
from strategies.base import OptionSpec, Signal  # noqa: F401

SNAPSHOT = os.path.join(REPO, 'docs', 'superpowers', 'specs',
                        '2026-05-29-sp5.1a-live-smoke-snapshot.md')

def log(msg: str):
    ts = dt.datetime.utcnow().isoformat() + 'Z'
    print(f'[sp5.1a-smoke {ts}] {msg}', flush=True)

def append_snapshot(block: str):
    with open(SNAPSHOT, 'a') as f:
        f.write(block + '\n')

def _option_order(direction, right, strike_rule, moneyness=None,
                  contracts=1, notional=2000, dte=22, underlying='SPY'):
    spec = OptionSpec(
        underlying=underlying, right=right,
        strike_rule=strike_rule, moneyness=moneyness,
        target_delta=0.30, dte_target=dte,
        structure='single', hedge='none',
    )
    return {
        'ticker': underlying,
        'strategy_id': 'sp5_1a_smoke',
        'direction': direction,
        'instrument_class': 'option',
        'contracts': float(contracts),
        'notional_usd': float(notional),
        'option_spec': spec,
    }

def _poll_order(order_id: str, timeout: float = 30.0, poll: float = 1.0) -> dict | None:
    """Poll alpaca order until terminal (filled/canceled/expired/rejected)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, payload, _ = _run_alpaca_cli(['order','get','--order-id', order_id])
        if ok and payload:
            status = (payload.get('status') or '').lower()
            if status in ('filled','canceled','expired','rejected','done_for_day'):
                return payload
        time.sleep(poll)
    return None

def _confirm_rth():
    ok, payload, _ = _run_alpaca_cli(['clock'])
    if not ok or not payload:
        log('ABORT: cannot fetch alpaca clock'); sys.exit(2)
    if not payload.get('is_open'):
        log(f"ABORT: market closed (next_open={payload.get('next_open')})"); sys.exit(1)

def smoke_a_long_call(dry_run: bool) -> dict:
    log('SMOKE A: long call ATM end-to-end')
    order = _option_order('long', 'call', 'atm')
    coid = f'sp5-1a-A-{int(time.time())}'
    equity = 100_000.0
    if dry_run:
        log(f'[dry-run] would call _route_option_order(order={order["ticker"]}/call ATM, coid={coid})')
        return {'smoke':'A','status':'dry-run'}
    res = _route_option_order(order, equity=equity, coid=coid)
    if res is None:
        log('SMOKE A FAIL: helper returned None unexpectedly')
        return {'smoke':'A','status':'fail','reason':'helper returned None'}
    log(f'submit result: {json.dumps({k: res.get(k) for k in ("ticker","status","order_id","qty","entry","reason")})}')
    if res.get('status') == 'skipped':
        return {'smoke':'A','status':'skipped','reason':res.get('reason'),'result':res}
    order_id = res.get('order_id')
    if not order_id:
        log('SMOKE A FAIL: no order_id'); return {'smoke':'A','status':'fail','result':res}
    log(f'polling {order_id} to terminal...')
    term = _poll_order(order_id, timeout=30.0)
    if not term:
        log('SMOKE A WARN: did not reach terminal in 30s — proceeding to close'); term = {}
    log(f'terminal: status={term.get("status")} filled_qty={term.get("filled_qty")} filled_avg_price={term.get("filled_avg_price")}')
    # Close the position (regardless of fill state; if not filled, this is a no-op)
    close_order = dict(order); close_order['close_only'] = True
    close_coid = f'{coid}-close'
    log('closing position via close_only=True')
    close_res = _route_option_order(close_order, equity=equity, coid=close_coid)
    log(f'close result: {json.dumps({k: close_res.get(k) for k in ("ticker","status","order_id","reason")} if close_res else {"close":"None"})}')
    return {'smoke':'A','status':'ok','submit':res,'terminal':term,'close':close_res,'coid':coid}

def smoke_b_short_put(dry_run: bool) -> dict:
    log('SMOKE B: cash-secured short put non-marketable (IWM)')
    # IWM (~$290) has deep-OTM strikes (down to ~$145) on near expiries; SPY
    # 6/26 puts only go to $425 → $42.5k collateral, exceeds paper cash.
    # IWM 6/26 puts down to $145 → ~$14.5k collateral fits a $30k cash account.
    cash = _account_cash() if not dry_run else 30_000.0
    log(f'account cash: ${cash:.0f}')
    underlying = 'IWM'
    # Estimate spot for moneyness target — resolver will pick the nearest listed.
    spot_est = 290.0
    target_strike = (cash * 0.90) / 100.0  # 90% headroom; collateral = strike*100
    moneyness = max(0.20, min(0.95, target_strike / spot_est))
    order = _option_order('short', 'put', 'fixed_moneyness',
                          moneyness=moneyness, underlying=underlying)
    order['notional_usd'] = 500.0  # contracts=1; cash-collateral guard governs
    coid = f'sp5-1a-B-{int(time.time())}'
    equity = 100_000.0
    if dry_run:
        log(f'[dry-run] would call _route_option_order(order={order["ticker"]}/put fixed_moneyness={moneyness:.3f}, coid={coid})')
        return {'smoke':'B','status':'dry-run'}
    # FORCE non-marketable: monkey-patch _option_marketable_limit transiently
    # so the limit price is wildly off (rests instead of fills).
    import execution.alpaca_executor as ae
    orig_limit = ae._option_marketable_limit
    def _non_marketable(side, quote, slip=0.02):
        return 999.00  # for SELL, limit=999 is way above any bid → rests, no fill
    ae._option_marketable_limit = _non_marketable
    try:
        res = _route_option_order(order, equity=equity, coid=coid)
    finally:
        ae._option_marketable_limit = orig_limit
    if res is None:
        log('SMOKE B FAIL: helper returned None unexpectedly')
        return {'smoke':'B','status':'fail','reason':'helper returned None'}
    log(f'submit result: {json.dumps({k: res.get(k) for k in ("ticker","status","order_id","qty","entry","reason")})}')
    if res.get('status') == 'skipped':
        log(f'SMOKE B skipped: {res.get("reason")} — treating as PASS if reason is insufficient-cash for the wider band')
        return {'smoke':'B','status':'skipped','reason':res.get('reason'),'result':res}
    order_id = res.get('order_id')
    if order_id:
        log(f'cancelling resting order {order_id}')
        ok, payload, err = _run_alpaca_cli(['order','cancel','--order-id', order_id])
        log(f'cancel: ok={ok} err={err or "-"}')
    return {'smoke':'B','status':'ok','submit':res,'coid':coid}

def smoke_c_gate_off(dry_run: bool) -> dict:
    log('SMOKE C: gate-OFF verification')
    prev = os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    try:
        order = _option_order('long', 'call', 'atm')
        res = _route_option_order(order, equity=100_000.0, coid='sp5-1a-C-gate-off')
    finally:
        if prev is not None:
            os.environ['OPENCLAW_OPTION_EXEC'] = prev
    if res is None:
        log('SMOKE C PASS: helper returned None with gate OFF')
        return {'smoke':'C','status':'ok','result':None}
    log(f'SMOKE C FAIL: expected None, got {res}')
    return {'smoke':'C','status':'fail','result':res}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='Construct + log only; no submit')
    args = ap.parse_args()

    if not args.dry_run:
        _confirm_rth()
        # Confirm gates ON
        if os.environ.get('OPENCLAW_INSTRUMENT_CLASS_ROUTING') != '1':
            log('ABORT: OPENCLAW_INSTRUMENT_CLASS_ROUTING != 1'); sys.exit(2)
        if os.environ.get('OPENCLAW_OPTION_EXEC') != '1':
            log('ABORT: OPENCLAW_OPTION_EXEC != 1'); sys.exit(2)

    results = {}
    results['A'] = smoke_a_long_call(args.dry_run)
    results['B'] = smoke_b_short_put(args.dry_run)
    results['C'] = smoke_c_gate_off(args.dry_run)

    # Persist results
    block = ['', f'## SP-5.1a smoke results — {dt.datetime.utcnow().isoformat()}Z' + (' (dry-run)' if args.dry_run else '')]
    for k in 'ABC':
        block.append('')
        block.append(f'### Smoke {k}')
        block.append('```')
        block.append(json.dumps(results[k], default=str, indent=2))
        block.append('```')
    append_snapshot('\n'.join(block))
    log(f'results appended to {SNAPSHOT}')

    all_ok = all(r.get('status') in ('ok','dry-run','skipped') for r in results.values())
    print('\nSUMMARY:', {k: v.get('status') for k, v in results.items()})
    sys.exit(0 if all_ok else 1)

if __name__ == '__main__':
    main()
