#!/usr/bin/env python3
"""SP-5 Phase 3 T9 — supervised live-paper delta-hedge smoke (end-to-end).

Mirrors the sp5_2/sp5_2b supervised-smoke pattern (fill -> poll terminal ->
per-leg close -> re-flatten retry -> flat-verify -> gate-off sub-check ->
independent post-verify) for a LONG delta-hedged ATM SPY straddle, exercising
the SP-5.1c on-fill ledger write + the SP-5.1b-ii EOD hedge-target producer.

Flow (H1..H9):
  H1  baseline   — position list snapshot (count+symbols) + open option orders.
  H2  fill       — _route_option_order(long ATM straddle, hedge='delta', N contracts).
                   --dry-run: resolve expiry+legs+net-quote ONLY (no submit).
                   live: poll order_id to terminal; FAIL if not filled in budget.
  H3  ledger     — option_hedge_ledger row for underlying: status='active',
                   contracts==N, structure_legs JSON has 2 legs w/ occ/right/strike/expiry.
                   (committed by the executor's on-fill write — real persistence.)
  H4  compute    — compute_option_hedge_targets(cur, today) in ONE rolled-back txn;
                   live: assert APPROVED is_hedge ES row w/ nonzero hedge_shares +
                   36-char workspace_id + '__hedge__...' registry FK; rollback.
                   --dry-run: run against CURRENT (likely empty) ledger; assert the
                   call completes + report row counts (no hedge row required).
  H5  OCC 5b     — held legs for underlying must match regime_blended_sizer._OCC_RE.
  H6  close      — _route_option_order(close_only straddle) -> _route_mleg_close;
                   per-leg flat-verify via _options_current_qty + ONE re-flatten retry.
  H7  deactivate — ledger row now status='closed' (G4c fired during close).
  H8  gate-off   — del OPENCLAW_OPTION_EXEC -> _route_option_order returns a dict with
                   status='skipped' and 'gate is OFF' in reason (NIT-1 fail-closed).
  H9  post       — positions == H1 baseline, zero held legs, zero open option orders.

SAFETY:
  - NEVER submits a real order in --dry-run. Market CLOSED tonight ⇒ run --dry-run only.
  - Process-scoped gates (OPENCLAW_OPTION_EXEC=1, OPENCLAW_OPTION_DELTA_HEDGE=1) set
    via os.environ at the top of main; .env on disk is NEVER touched.
  - POSTGRES_URI must already be in env (runner passes it) — the on-fill ledger write
    needs it; a run without it would 'pass' the fill but silently skip the ledger row.
  - Never trust the ack: poll mleg to terminal, per-leg flat-verify, re-flatten once,
    exit non-zero on any orphan with loud manual-flatten instructions.

INVOCATION:
  POSTGRES_URI="$PG" PYTHONPATH=src nice -n 19 python3 scripts/sp5_t9_hedge_smoke.py --dry-run
  (live, RTH only, operator-supervised: drop --dry-run)
"""
from __future__ import annotations
import os, sys, time, json, argparse, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, 'src'))


def log(msg: str):
    ts = dt.datetime.now(dt.UTC).isoformat()
    print(f'[sp5-t9-smoke {ts}] {msg}', flush=True)


def _load_alpaca_env():
    """Load ONLY ALPACA_* auth from .env (so H1/H2 resolution can hit the broker).

    DELIBERATELY excludes POSTGRES_URI: the runner is required to pass POSTGRES_URI
    in the process env, and main() guards on it BEFORE this loader runs. If we let
    .env repopulate POSTGRES_URI, that guard could never fire — defeating the point
    (the on-fill ledger write silently no-ops without it). Never sources .env into
    bash (unquoted parens break it); a line-grep is safe."""
    keys = ('ALPACA_API_KEY', 'ALPACA_SECRET_KEY', 'ALPACA_BASE_URL')
    try:
        with open(os.path.join(REPO, '.env')) as f:
            for line in f:
                line = line.strip()
                for k in keys:
                    if line.startswith(f'{k}='):
                        os.environ[k] = line.split('=', 1)[1].strip().strip('"\'')
                        break
    except FileNotFoundError:
        log('WARN: .env not found — relying on inherited ALPACA_* env')


CONTRACTS = 1
UNDERLYING = 'SPY'
POLL_BUDGET = 45.0   # seconds to wait for the mleg open to reach a terminal state (live)


def _straddle_open_order(underlying: str, contracts: int) -> dict:
    """Long ATM delta-hedged straddle. NO strategy_id on purpose — the executor falls
    back to option_strategy_id = spec.underlying, so the ledger / hedge rows key on
    the underlying (H3 queries by underlying; H4's hedge row is '__hedge__SPY')."""
    from strategies.base import OptionSpec
    spec = OptionSpec(
        underlying=underlying, structure='straddle', hedge='delta',
        strike_rule='atm', dte_target=22,
    )
    return {
        'ticker': underlying, 'instrument_class': 'option', 'direction': 'long',
        'contracts': contracts, 'option_spec': spec,
    }


def _straddle_close_order(underlying: str) -> dict:
    from strategies.base import OptionSpec
    spec = OptionSpec(underlying=underlying, structure='straddle', hedge='delta')
    return {
        'ticker': underlying, 'instrument_class': 'option', 'direction': 'long',
        'close_only': True, 'option_spec': spec,
    }


# ── shared poll/verify helpers (mirror sp5_2 / sp5_2b) ──────────────────────────
def _poll_order(order_id: str, timeout: float = POLL_BUDGET, poll: float = 1.5):
    from execution.alpaca_executor import _run_alpaca_cli
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
    from execution.alpaca_executor import _options_current_qty
    deadline = time.time() + timeout
    residual = {occ: _options_current_qty(occ) for occ in legs}
    while any(q != 0 for q in residual.values()) and time.time() < deadline:
        time.sleep(poll)
        residual = {occ: _options_current_qty(occ) for occ in legs}
    return (all(q == 0 for q in residual.values()), residual)


def _position_snapshot():
    """(count, sorted_symbols) of ALL broker positions."""
    from execution.alpaca_executor import _run_alpaca_cli
    ok, payload, _ = _run_alpaca_cli(['position', 'list'])
    if not ok or not payload:
        return 0, []
    syms = sorted(str(p.get('symbol') or '') for p in payload)
    return len(syms), syms


def _open_option_orders(underlying: str):
    """OCC-symbol open orders for `underlying` (option legs only)."""
    from execution.alpaca_executor import _run_alpaca_cli
    ok, payload, _ = _run_alpaca_cli(['order', 'list', '--status', 'open'])
    if not ok or not payload:
        return []
    out = []
    for o in payload:
        sym = str(o.get('symbol') or '')
        root = ''
        for c in sym:
            if c.isalpha():
                root += c
            else:
                break
        if root == underlying and len(sym) > len(root):
            out.append(sym)
    return out


def _confirm_rth():
    from execution.alpaca_executor import _run_alpaca_cli
    ok, payload, _ = _run_alpaca_cli(['clock'])
    if not ok or not payload:
        log('ABORT: cannot fetch alpaca clock'); sys.exit(2)
    if not payload.get('is_open'):
        log(f"ABORT: market closed (next_open={payload.get('next_open')})"); sys.exit(1)


# ── H1..H9 ──────────────────────────────────────────────────────────────────────
def h1_baseline(state):
    log('H1: baseline snapshot (position list + open option orders)')
    count, syms = _position_snapshot()
    open_opts = _open_option_orders(UNDERLYING)
    state['baseline_count'] = count
    state['baseline_syms'] = syms
    state['baseline_open_opts'] = open_opts
    log(f'H1: {count} positions; {len(open_opts)} open {UNDERLYING} option orders')
    return {'status': 'ok', 'positions': count, 'open_option_orders': len(open_opts)}


def h2_fill(state, dry_run, contracts):
    from execution.alpaca_executor import (
        _route_option_order, _resolve_expiry, _resolve_structure_legs,
        _structure_net_quote,
    )
    log('H2: fill long ATM delta-hedged straddle (dte=22)')
    order = _straddle_open_order(UNDERLYING, contracts)
    spec = order['option_spec']
    coid = f'sp5-t9-{int(time.time())}'
    equity = 100_000.0

    if dry_run:
        # Resolution-only: resolve expiry -> legs -> net quote WITHOUT submitting.
        today = dt.date.today()
        expiry = _resolve_expiry(spec, today)
        if expiry is None:
            log('H2[dry-run] expiry unresolved (after-hours chain unavailable) — SKIP')
            return {'status': 'skipped', 'reason': 'expiry unresolved (market closed)'}
        log(f'H2[dry-run] resolved expiry={expiry}')
        legs = _resolve_structure_legs(spec, today, expiry)
        if not legs:
            log('H2[dry-run] structure legs unresolved (after-hours chain) — SKIP')
            return {'status': 'skipped', 'reason': 'legs unresolved (market closed)',
                    'expiry': str(expiry)}
        log(f'H2[dry-run] resolved legs={legs}')
        nq = _structure_net_quote(spec, legs, expiry)
        if nq is None:
            log('H2[dry-run] no quote for a structure leg (after-hours) — SKIP')
            return {'status': 'skipped', 'reason': 'no leg quote (market closed)',
                    'expiry': str(expiry), 'legs': legs}
        net, leg_q = nq
        log(f'H2[dry-run] net_debit={net:.2f} legs={[lq[0] for lq in leg_q]} (NO submit)')
        return {'status': 'dry-run', 'expiry': str(expiry),
                'net_debit': round(net, 2), 'legs': [lq[0] for lq in leg_q]}

    # ── live ──
    res = _route_option_order(order, equity=equity, coid=coid)
    if res is None:
        return {'status': 'fail', 'reason': 'helper returned None (gate off?)'}
    log(f'H2 submit: {json.dumps({k: res.get(k) for k in ("ticker","structure","status","order_id","qty","entry","legs","reason")})}')
    if res.get('status') == 'skipped':
        return {'status': 'fail', 'reason': f"unexpected SKIP: {res.get('reason')}", 'result': res}
    order_id, legs = res.get('order_id'), res.get('legs') or []
    if not order_id or len(legs) != 2:
        return {'status': 'fail', 'reason': f'no order_id or != 2 legs (legs={legs})', 'result': res}
    state['legs'] = legs
    state['submit'] = res
    log(f'H2 polling mleg {order_id} to terminal (budget {POLL_BUDGET}s)...')
    term = _poll_order(order_id) or {}
    st = (term.get('status') or '').lower()
    log(f'H2 terminal: status={st} filled_qty={term.get("filled_qty")}')
    if st != 'filled':
        log('H2 FAIL: not filled within budget — cancelling + flattening any partial')
        from execution.alpaca_executor import _run_alpaca_cli
        _run_alpaca_cli(['order', 'cancel', '--order-id', order_id])
        _verify_legs_flat(legs, timeout=20.0)
        return {'status': 'fail', 'reason': f'terminal status={st!r} (not filled)', 'terminal': term}
    return {'status': 'ok', 'order_id': order_id, 'legs': legs, 'terminal': term}


def h3_ledger_verify(state, dry_run):
    if dry_run:
        log('H3: ledger verify — SKIP in --dry-run (no live fill ⇒ no on-fill write)')
        return {'status': 'skipped', 'reason': 'no live fill in dry-run'}
    import psycopg2
    log('H3: option_hedge_ledger row written on fill (the C3 seam)')
    uri = os.environ['POSTGRES_URI']
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT option_strategy_id, underlying, contracts, status, structure_legs
                   FROM option_hedge_ledger
                   WHERE underlying=%s AND status='active'
                   ORDER BY updated_at DESC LIMIT 1""",
                (UNDERLYING,))
            row = cur.fetchone()
    if row is None:
        return {'status': 'fail', 'reason': 'no active option_hedge_ledger row for underlying'}
    osid, und, contracts, status, legs = row
    if status != 'active':
        return {'status': 'fail', 'reason': f"ledger status={status!r}, expected active"}
    if int(contracts) != int(state.get('expected_contracts', CONTRACTS)):
        return {'status': 'fail', 'reason': f'ledger contracts={contracts}, expected {state.get("expected_contracts")}'}
    legs = legs if isinstance(legs, list) else json.loads(legs)
    if len(legs) != 2:
        return {'status': 'fail', 'reason': f'ledger has {len(legs)} legs, expected 2'}
    for lg in legs:
        for fld in ('occ', 'right', 'strike', 'expiry'):
            if fld not in lg:
                return {'status': 'fail', 'reason': f'ledger leg missing {fld!r}: {lg}'}
    log(f'H3 PASS: active ledger row option_strategy_id={osid} contracts={contracts}, 2 legs w/ occ/right/strike/expiry')
    return {'status': 'ok', 'option_strategy_id': osid, 'contracts': int(contracts), 'legs': legs}


def h4_compute_verify(state, dry_run):
    """compute_option_hedge_targets in ONE rolled-back transaction. NOTHING persists.

    live: assert an APPROVED is_hedge ES row for the underlying exists in-txn with
          nonzero hedge_shares + 36-char workspace_id + '__hedge__...' registry FK.
    dry-run: run against the CURRENT (likely empty) ledger; assert only that the call
             completes + report the active-ledger / hedge-row counts (no hedge row
             required — see ATM-near-neutral note in the module docstring)."""
    import psycopg2
    import execution.option_hedge as oh
    today = dt.date.today()
    uri = os.environ['POSTGRES_URI']
    conn = psycopg2.connect(uri)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM option_hedge_ledger WHERE status='active'")
        active_before = cur.fetchone()[0]
        log(f'H4: {active_before} active ledger row(s) before compute (txn, will rollback)')

        # Real compute — NO stub. Empty/near-neutral ledger ⇒ trivially safe in dry-run.
        oh.compute_option_hedge_targets(cur, today)

        cur.execute(
            """SELECT strategy_id, ticker, direction, lifecycle_state, status,
                      workspace_id,
                      signal_params->>'is_hedge',
                      (signal_params->>'hedge_shares')::float
               FROM execution_signals
               WHERE signal_params->>'is_hedge'='true' AND signal_date=%s""",
            (today,))
        hedge_rows = cur.fetchall()
        log(f'H4: compute produced {len(hedge_rows)} is_hedge ES row(s) in-txn')

        if dry_run:
            # Only assert the call completed and report counts.
            conn.rollback()
            log('H4[dry-run] PASS: compute_option_hedge_targets completed; rolled back '
                f'(active_ledger={active_before}, hedge_rows={len(hedge_rows)})')
            return {'status': 'dry-run', 'active_ledger_before': active_before,
                    'hedge_rows': len(hedge_rows)}

        # ── live assertions ──
        und_rows = [r for r in hedge_rows if str(r[1]) == UNDERLYING]
        if not und_rows:
            conn.rollback()
            return {'status': 'fail', 'reason': f'no APPROVED is_hedge ES row for {UNDERLYING} after compute'}
        r = und_rows[0]
        strategy_id, ticker, direction, lifecycle_state, _status, ws, is_hedge, hedge_shares = r
        problems = []
        if lifecycle_state != 'APPROVED':
            problems.append(f'lifecycle_state={lifecycle_state!r} != APPROVED')
        if not (hedge_shares and float(hedge_shares) != 0.0):
            problems.append(f'hedge_shares={hedge_shares} not nonzero')
        if len(str(ws)) != 36:
            problems.append(f'workspace_id not 36-char UUID: {ws!r}')
        if is_hedge != 'true':
            problems.append(f"is_hedge={is_hedge!r} != 'true'")
        # FK registry row present
        cur.execute("SELECT id, status FROM strategy_registry WHERE id=%s", (str(strategy_id),))
        reg = cur.fetchone()
        if reg is None or not str(strategy_id).startswith('__hedge__'):
            problems.append(f'registry FK row missing or strategy_id not __hedge__*: {strategy_id!r} reg={reg}')
        log(f'H4 row: strategy_id={strategy_id} dir={direction} hedge_shares={hedge_shares} ws={ws}')
        conn.rollback()
        if problems:
            return {'status': 'fail', 'reason': '; '.join(problems)}
        log('H4 PASS: APPROVED is_hedge row + nonzero hedge_shares + 36-char ws + __hedge__ FK; rolled back')
        return {'status': 'ok', 'strategy_id': str(strategy_id), 'direction': direction,
                'hedge_shares': float(hedge_shares), 'workspace_id': str(ws)}
    finally:
        conn.rollback()   # NEVER commit — canonical tables
        conn.close()


def h5_occ_format(state, dry_run):
    """Checklist 5b: held legs for underlying must match regime_blended_sizer._OCC_RE."""
    if dry_run:
        log('H5: OCC-format check — SKIP in --dry-run (no held legs)')
        return {'status': 'skipped', 'reason': 'no held legs in dry-run'}
    from execution.alpaca_executor import _held_option_legs
    from execution.regime_blended_sizer import _OCC_RE
    held = _held_option_legs(UNDERLYING)
    if not held:
        return {'status': 'fail', 'reason': 'no held legs to OCC-check (expected 2 after H2 fill)'}
    results = {}
    all_match = True
    for sym in held:
        matched = bool(_OCC_RE.match(str(sym).strip().upper()))
        results[sym] = matched
        log(f'H5: {sym} -> _OCC_RE match={matched}')
        all_match = all_match and matched
    if not all_match:
        return {'status': 'fail', 'reason': f'OCC mismatch (orphan-close exposure): {results}'}
    log(f'H5 PASS: all {len(held)} held legs match _OCC_RE')
    return {'status': 'ok', 'legs': results}


def h6_close(state, dry_run):
    if dry_run:
        log('H6: close — SKIP in --dry-run (nothing was opened)')
        return {'status': 'skipped', 'reason': 'no live position in dry-run'}
    from execution.alpaca_executor import _route_option_order
    legs = state.get('legs') or []
    coid = f'sp5-t9-close-{int(time.time())}'
    equity = 100_000.0
    close_order = _straddle_close_order(UNDERLYING)
    log('H6: closing straddle via close_only -> _route_mleg_close (per-leg)')
    close_res = _route_option_order(close_order, equity=equity, coid=coid)
    log(f'H6 close: {json.dumps({k: (close_res or {}).get(k) for k in ("status","legs","reason")})}')
    is_flat, residual = _verify_legs_flat(legs, timeout=20.0)
    if not is_flat:
        log(f'H6 WARN: residual {residual} — re-flattening once (long-leg-retry pattern)')
        _route_option_order(close_order, equity=equity, coid=f'{coid}-2')
        is_flat, residual = _verify_legs_flat(legs, timeout=25.0)
    if not is_flat:
        log(f'H6 FAIL: ORPHAN legs {residual} after close+retry. '
            f'MANUAL FLATTEN REQUIRED: for each orphan run '
            f'`alpaca position close --symbol-or-asset-id <OCC>`, then re-run flat-verify, '
            f'then ensure gates are OFF.')
        return {'status': 'fail', 'reason': f'orphan {residual}', 'close': close_res}
    log(f'H6 PASS: confirmed FLAT on all legs {legs}')
    return {'status': 'ok', 'flat': True, 'close': close_res}


def h7_deactivation_verify(state, dry_run):
    if dry_run:
        log('H7: ledger deactivation — SKIP in --dry-run (no active row created)')
        return {'status': 'skipped', 'reason': 'no ledger row in dry-run'}
    import psycopg2
    log('H7: option_hedge_ledger row now status=closed (G4c fired during close)')
    uri = os.environ['POSTGRES_URI']
    with psycopg2.connect(uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM option_hedge_ledger WHERE underlying=%s AND status='active'",
                (UNDERLYING,))
            still_active = cur.fetchone()[0]
    if still_active:
        log(f'H7 FAIL: {still_active} ledger row(s) STILL active for {UNDERLYING}. '
            f'Manual deactivation SQL (DO NOT auto-run; operator review):\n'
            f"  UPDATE option_hedge_ledger SET status='closed', target_hedge_qty=0, "
            f"updated_at=NOW() WHERE underlying='{UNDERLYING}' AND status='active';")
        return {'status': 'fail', 'reason': f'{still_active} active ledger row(s) after close'}
    log('H7 PASS: 0 active ledger rows for underlying (G4c deactivated)')
    return {'status': 'ok'}


def h8_gate_off(state, dry_run):
    """NIT-1 fail-closed: option order + gate OFF -> SKIP dict (not None, not equity).
    Submits NOTHING, so it runs in --dry-run too."""
    from execution.alpaca_executor import _route_option_order
    log('H8: gate-OFF sub-check (del OPENCLAW_OPTION_EXEC -> skip dict w/ "gate is OFF")')
    prev = os.environ.pop('OPENCLAW_OPTION_EXEC', None)
    try:
        order = _straddle_open_order(UNDERLYING, CONTRACTS)
        res = _route_option_order(order, equity=100_000.0, coid='sp5-t9-gate-off')
    finally:
        if prev is not None:
            os.environ['OPENCLAW_OPTION_EXEC'] = prev
    if res is None:
        return {'status': 'fail', 'reason': 'returned None (stale pre-NIT-1 contract); expected skip dict'}
    if res.get('status') != 'skipped':
        return {'status': 'fail', 'reason': f"status={res.get('status')!r} != 'skipped'", 'result': res}
    if 'gate is OFF' not in (res.get('reason') or ''):
        return {'status': 'fail', 'reason': f"reason missing 'gate is OFF': {res.get('reason')!r}"}
    log('H8 PASS: gate-OFF returns skip dict with "gate is OFF" reason')
    return {'status': 'ok', 'reason': res.get('reason')}


def h9_post_verify(state, dry_run):
    log('H9: independent post-verify (positions == H1 baseline, 0 held legs, 0 open option orders)')
    if dry_run:
        # In dry-run we opened nothing; still assert the broker book is unchanged
        # and there are no stray held legs / open orders for the underlying.
        count, syms = _position_snapshot()
        open_opts = _open_option_orders(UNDERLYING)
        from execution.alpaca_executor import _held_option_legs
        held = _held_option_legs(UNDERLYING)
        problems = []
        if syms != state.get('baseline_syms'):
            problems.append(f'positions changed: baseline={state.get("baseline_count")} now={count}')
        if held:
            problems.append(f'held {UNDERLYING} legs present: {held}')
        if problems:
            return {'status': 'fail', 'reason': '; '.join(problems)}
        log(f'H9[dry-run] PASS: book unchanged ({count} positions), 0 held {UNDERLYING} legs, '
            f'{len(open_opts)} open option orders (pre-existing baseline carried)')
        return {'status': 'dry-run', 'positions': count, 'held_legs': 0,
                'open_option_orders': len(open_opts)}

    count, syms = _position_snapshot()
    open_opts = _open_option_orders(UNDERLYING)
    from execution.alpaca_executor import _held_option_legs
    held = _held_option_legs(UNDERLYING)
    problems = []
    if syms != state.get('baseline_syms'):
        problems.append(f'positions != baseline (baseline_count={state.get("baseline_count")}, now={count})')
    if held:
        problems.append(f'held {UNDERLYING} legs remain: {held}')
    if open_opts != state.get('baseline_open_opts'):
        problems.append(f'open option orders changed: now={open_opts}')
    if problems:
        return {'status': 'fail', 'reason': '; '.join(problems)}
    log('H9 PASS: equity book untouched, 0 held legs, open option orders == baseline')
    return {'status': 'ok', 'positions': count}


def main():
    global UNDERLYING, CONTRACTS
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='validate wiring; no orders, no DB writes')
    ap.add_argument('--contracts', type=int, default=CONTRACTS)
    ap.add_argument('--underlying', default=UNDERLYING)
    args = ap.parse_args()

    UNDERLYING = args.underlying
    CONTRACTS = args.contracts

    # ── PROCESS-SCOPED GATES ────────────────────────────────────────────────────
    print('=' * 78, flush=True)
    print('  SP-5 T9 HEDGE SMOKE — process-scoped gates ON; .env on disk is UNTOUCHED', flush=True)
    print('  OPENCLAW_OPTION_EXEC=1, OPENCLAW_OPTION_DELTA_HEDGE=1 (this process only)', flush=True)
    print(f"  mode={'DRY-RUN (no orders, no DB writes)' if args.dry_run else 'LIVE'}  "
          f"underlying={UNDERLYING}  contracts={CONTRACTS}", flush=True)
    print('=' * 78, flush=True)
    os.environ['OPENCLAW_OPTION_EXEC'] = '1'
    os.environ['OPENCLAW_OPTION_DELTA_HEDGE'] = '1'

    # ── POSTGRES_URI must be in env BEFORE loading any .env (guard is the point) ──
    if not os.environ.get('POSTGRES_URI'):
        log('ABORT: POSTGRES_URI not in env. The on-fill ledger write + H4 compute need it; '
            'a run without it would "pass" the fill but silently skip the ledger row. '
            'Re-run with: POSTGRES_URI="$PG" PYTHONPATH=src python3 scripts/sp5_t9_hedge_smoke.py ...')
        sys.exit(2)

    _load_alpaca_env()   # ALPACA_* only; does NOT touch POSTGRES_URI

    if not args.dry_run:
        _confirm_rth()
        if os.environ.get('OPENCLAW_OPTION_EXEC') != '1':
            log('ABORT: OPENCLAW_OPTION_EXEC != 1'); sys.exit(2)
        if os.environ.get('OPENCLAW_OPTION_DELTA_HEDGE') != '1':
            log('ABORT: OPENCLAW_OPTION_DELTA_HEDGE != 1'); sys.exit(2)

    state = {'expected_contracts': CONTRACTS}
    results = {}

    # H1 baseline (broker reachable?). If not, the live path can't proceed.
    results['H1'] = h1_baseline(state)
    results['H2'] = h2_fill(state, args.dry_run, CONTRACTS)
    # If H2 live failed, do not open H3/H5/H6 against a half-state.
    h2_ok_for_chain = results['H2'].get('status') in ('ok', 'dry-run', 'skipped')
    results['H3'] = h3_ledger_verify(state, args.dry_run) if h2_ok_for_chain else {'status': 'skipped', 'reason': 'H2 failed'}
    results['H4'] = h4_compute_verify(state, args.dry_run)
    results['H5'] = h5_occ_format(state, args.dry_run) if h2_ok_for_chain else {'status': 'skipped', 'reason': 'H2 failed'}
    results['H6'] = h6_close(state, args.dry_run) if h2_ok_for_chain else {'status': 'skipped', 'reason': 'H2 failed'}
    results['H7'] = h7_deactivation_verify(state, args.dry_run) if h2_ok_for_chain else {'status': 'skipped', 'reason': 'H2 failed'}
    results['H8'] = h8_gate_off(state, args.dry_run)
    results['H9'] = h9_post_verify(state, args.dry_run)

    # ── PASS/FAIL table ──────────────────────────────────────────────────────────
    PASS_STATUSES = ('ok', 'dry-run', 'skipped')
    order = ['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'H7', 'H8', 'H9']
    print('\n' + '=' * 78, flush=True)
    print('  SP-5 T9 HEDGE SMOKE — PASS/FAIL TABLE'
          + ('  (DRY-RUN)' if args.dry_run else '  (LIVE)'), flush=True)
    print('=' * 78, flush=True)
    for k in order:
        r = results[k]
        st = r.get('status', '?')
        verdict = 'PASS' if st in PASS_STATUSES else 'FAIL'
        reason = r.get('reason')
        line = f'  {k}  {verdict:<4}  status={st}'
        if reason:
            line += f'  ({reason})'
        print(line, flush=True)
    print('=' * 78, flush=True)

    # H8 must specifically be 'ok' (it runs and submits nothing in any mode).
    h8_ok = results['H8'].get('status') == 'ok'
    all_ok = all(results[k].get('status') in PASS_STATUSES for k in order) and h8_ok
    if not h8_ok:
        log('OVERALL FAIL: H8 gate-off sub-check did not PASS (must be ok, not skipped)')
    summary = {k: results[k].get('status') for k in order}
    print(f'\nSUMMARY: {summary}', flush=True)
    print(f'OVERALL: {"PASS" if all_ok else "FAIL"}', flush=True)
    sys.exit(0 if all_ok else 1)


if __name__ == '__main__':
    main()
