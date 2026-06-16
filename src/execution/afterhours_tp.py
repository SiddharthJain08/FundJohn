#!/usr/bin/env python3
"""afterhours_tp.py — W3: resting extended-hours take-profits.

Alpaca extended-hours orders must be limit + day TIF + extended_hours=true.
A sell-limit above market (buy-limit below, for shorts) is a clean ext-hours
take-profit. Stops cannot be represented in ext-hours, so this covers UPSIDE
only; the RTH GTC stop still covers downside.

Placed at each ext-hours session open; a session-boundary reconcile cancels the
prior session's TP and resizes the (unlinked) GTC stop after an ext-hours fill.

Gate: OPENCLAW_AFTERHOURS_TP (default OFF).
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone


def afterhours_tp_on() -> bool:
    return os.environ.get('OPENCLAW_AFTERHOURS_TP') == '1'


def desired_tps(positions, bracket_lookup, tp_covered=None):
    """Pure: map open positions to the ext-hours TP orders to place.

    positions: [{'symbol','side','qty'}]
    bracket_lookup(symbol, side) -> {'target': float} | None  (latest placed TP)
    tp_covered: {symbol: qty already resting on a limit} (skip covered qty)
    Returns [{'ticker','side','qty','tp'}] — side is the EXIT side.
    """
    tp_covered = tp_covered or {}
    out = []
    for p in positions:
        sym = p.get('symbol')
        side = (p.get('side') or '').lower()
        try:
            qty = abs(float(p.get('qty') or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if not sym or qty <= 0 or side not in ('long', 'short'):
            continue
        if tp_covered.get(sym, 0.0) >= qty - 0.01:
            continue
        b = bracket_lookup(sym, side)
        tp = (b or {}).get('target')
        if not tp or float(tp) <= 0:
            continue
        exit_side = 'sell' if side == 'long' else 'buy'
        out.append({'ticker': sym, 'side': exit_side, 'qty': int(qty),
                    'tp': float(tp)})
    return out


ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} [AFTERHOURS_TP] {msg}")


def _submit_limit(*, ticker, side, qty, limit_price, tif, order_class,
                  order_type, extended_hours, coid):
    """Thin wrapper over the executor's CLI submit (kept patchable in tests)."""
    from execution.alpaca_executor import _submit_order_via_cli
    return _submit_order_via_cli(
        ticker=ticker, side=side, qty=qty, tif=tif, order_class=order_class,
        target=None, stop=None, coid=coid, order_type=order_type,
        extended_hours=extended_hours, limit_price=limit_price)


def _place_plan(plan, dry_run: bool) -> int:
    if not afterhours_tp_on():
        log('OPENCLAW_AFTERHOURS_TP!=1 — skipping')
        return 0
    n = 0
    for o in plan:
        coid = f"ahtp_{o['ticker']}_{int(datetime.now(timezone.utc).timestamp())}"
        if dry_run:
            log(f"  DRY-RUN {o['ticker']} {o['side'].upper()} LIMIT x{o['qty']} "
                f"@ {o['tp']:.2f} ext_hours=True")
            n += 1
            continue
        ok, _pay, err = _submit_limit(
            ticker=o['ticker'], side=o['side'], qty=o['qty'],
            limit_price=o['tp'], tif='day', order_class='simple',
            order_type='limit', extended_hours=True, coid=coid)
        if ok:
            log(f"  ✔ {o['ticker']} ext-hours TP x{o['qty']} @ {o['tp']:.2f}")
            n += 1
        else:
            log(f"  ✘ {o['ticker']} ext-hours TP failed: {(err or {}).get('error','?')}")
    return n


def _cli(args, timeout=15):
    from execution.stop_reattach import _run_cli
    return _run_cli(args, timeout=timeout)


def reconcile_afterhours(dry_run: bool) -> dict:
    """Cancel any resting ext-hours TP we placed (coid prefix 'ahtp_'); if a
    position's TP filled (the GTC stop now over-covers the held qty), cancel the
    oversized stop so the next stop_reattach pass re-sizes it. Idempotent.

    After an ext-hours TP fill the oversized stop is canceled here and the
    position is left unprotected until the next stop_reattach pass (next RTH
    open). This is a deliberate naked-position window: the ext-hours session is
    over, the stop would be unexecutable anyway, and stop_reattach re-attaches
    at RTH open.

    Safety guard: the oversized-stop pass only cancels when we have POSITIVE
    evidence that the held qty is below the stop qty. If fetch_positions()
    returns empty (CLI glitch), the entire resize pass is skipped and a warning
    is logged. Orphan cleanup after a real close is stop_reattach's job.
    """
    if not afterhours_tp_on():
        log('OPENCLAW_AFTERHOURS_TP!=1 — skipping')
        return {'tp_canceled': 0, 'stops_resized': 0}
    from execution.stop_reattach import fetch_positions
    stats = {'tp_canceled': 0, 'stops_resized': 0}
    ok, orders, _ = _cli(['order', 'list', '--status', 'open'])
    if not ok:
        return stats
    pos_qty = {p['symbol']: abs(float(p.get('qty') or 0))
               for p in fetch_positions()}
    for o in (orders or []):
        if (o.get('client_order_id') or '').startswith('ahtp_'):
            if dry_run:
                log(f"  DRY-RUN cancel ext-hours TP {o.get('symbol')} {o.get('id')}")
                stats['tp_canceled'] += 1
            else:
                ok_cancel, _, err = _cli(['order', 'cancel', '--order-id', o.get('id')])
                if ok_cancel:
                    stats['tp_canceled'] += 1
                else:
                    log(f"  ✘ cancel ext-hours TP {o.get('id')} failed: "
                        f"{(err or {}).get('error', '?')}")
    # Oversized-stop resize pass: only run when we have positive position evidence.
    # If pos_qty is empty but there are open stops, the CLI likely glitched —
    # log a warning and skip the entire resize pass (never cancel on unknown state).
    open_stops = [o for o in (orders or [])
                  if (o.get('type') or o.get('order_type')) in ('stop', 'stop_limit')]
    if not pos_qty and open_stops:
        log(f"  ⚠ fetch_positions returned empty with {len(open_stops)} open stop(s) "
            f"present — resize pass SKIPPED (missing position data)")
        return stats
    for o in open_stops:
        sym = o.get('symbol')
        try:
            stop_qty = abs(float(o.get('qty') or 0))
        except (TypeError, ValueError):
            stop_qty = 0.0
        held = pos_qty.get(sym)
        if held is None:
            # No position data for this symbol — cannot confirm it shrank; skip.
            continue
        if stop_qty > held + 0.01:
            log(f"  ⚠ {sym}: stop qty {stop_qty:.0f} > held {held:.0f} "
                f"(ext-hours TP filled) — cancel+resize")
            if dry_run:
                stats['stops_resized'] += 1
            else:
                ok_cancel, _, err = _cli(['order', 'cancel', '--order-id', o.get('id')])
                if ok_cancel:
                    stats['stops_resized'] += 1
                else:
                    log(f"  ✘ cancel oversized stop {o.get('id')} failed: "
                        f"{(err or {}).get('error', '?')}")
    return stats


def main(argv=None) -> int:
    import argparse
    from execution.stop_reattach import (fetch_positions, fetch_tp_covered,
                                         latest_broker_bracket)
    ap = argparse.ArgumentParser()
    ap.add_argument('--reconcile', action='store_true',
                    help='session-boundary reconcile instead of placement')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    if not afterhours_tp_on():
        log('OPENCLAW_AFTERHOURS_TP!=1 — skipping')
        return 0
    if args.reconcile:
        log(f'reconcile: {reconcile_afterhours(args.dry_run)}')
        return 0
    positions = list(fetch_positions())
    plan = desired_tps(positions, latest_broker_bracket,
                       tp_covered=fetch_tp_covered())
    log(f'placing {len(plan)} ext-hours TP(s)')
    _place_plan(plan, args.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
