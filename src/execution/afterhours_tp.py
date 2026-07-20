#!/usr/bin/env python3
"""afterhours_tp.py — W3: resting extended-hours take-profits.

Alpaca extended-hours orders must be limit + day TIF + extended_hours=true.
A sell-limit above market (buy-limit below, for shorts) is a clean ext-hours
take-profit. Stops cannot be represented in ext-hours; downside is covered by
the --monitor mode (2026-07-20): a timer-driven pass that emulates the stop —
on a level breach it cancels the symbol's resting exit orders and submits an
ext-hours marketable limit exit, restoring a bare GTC stop on any failure.

TP placement runs at each ext-hours session open; a session-boundary reconcile
cancels the prior session's TP and resizes the (unlinked) GTC stop after an
ext-hours fill.

Gates: OPENCLAW_AFTERHOURS_TP (placement, default OFF),
OPENCLAW_AFTERHOURS_STOP_MONITOR (monitor, default OFF),
OPENCLAW_AH_EXIT_SLIP_PCT (marketable-limit buffer, default 0.5).
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone
from pathlib import Path as _Path

# Run standalone via systemd (ExecStart=python3 .../afterhours_tp.py): src/ is not
# on sys.path, so the lazy `from execution.X import` calls below would raise
# ModuleNotFoundError. Mirror the repo idiom (alpaca_reconcile.py / ic_gate_runner.py):
# put the repo root and src/ on the path so the `execution` package resolves.
_ROOT = _Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / 'src')):
    if _p not in sys.path:
        sys.path.insert(0, _p)


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


# ── Ext-hours stop/TP monitor (downside protection outside RTH) ─────────────
# Plain stop orders cannot trigger in extended sessions, so the resting GTC
# OCO protects RTH only. The monitor emulates the stop: on a level breach it
# cancels the symbol's resting exit orders (frees the reserved shares) and
# submits an extended-hours marketable DAY limit exit. If the ext session
# closes before it fills, the same limit stays working into the RTH open.


def stop_monitor_on() -> bool:
    return os.environ.get('OPENCLAW_AFTERHOURS_STOP_MONITOR') == '1'


def _slip_pct() -> float:
    """Marketable-limit buffer past last trade. Wide enough to cross thin
    ext-hours spreads, tight enough to bound a bad print."""
    try:
        return float(os.environ.get('OPENCLAW_AH_EXIT_SLIP_PCT', '0.5')) / 100.0
    except ValueError:
        return 0.005


def protection_map(orders) -> dict:
    """{symbol: {'stop': float|None, 'tp': float|None}} from resting exit
    orders. MUST be fed a --nested order list: an OCO's stop lives on a held
    child leg that a flat listing hides entirely."""
    out: dict = {}
    def _lvl(sym, key, val):
        if not sym or not val:
            return
        try:
            v = float(val)
        except (TypeError, ValueError):
            return
        if v > 0:
            out.setdefault(sym, {'stop': None, 'tp': None})
            # Keep the tightest level (max stop for shorts is unknowable here
            # without side; first-seen is fine — one exit set per symbol).
            if out[sym][key] is None:
                out[sym][key] = v
    for o in (orders or []):
        sym = o.get('symbol')
        otype = (o.get('type') or o.get('order_type') or '').lower()
        oclass = (o.get('order_class') or 'simple').lower()
        if otype in ('stop', 'stop_limit'):
            _lvl(sym, 'stop', o.get('stop_price'))
        elif otype == 'limit' and oclass in ('oco', 'bracket'):
            _lvl(sym, 'tp', o.get('limit_price'))
        for leg in (o.get('legs') or []):
            ltype = (leg.get('type') or leg.get('order_type') or '').lower()
            if ltype in ('stop', 'stop_limit'):
                _lvl(leg.get('symbol') or sym, 'stop', leg.get('stop_price'))
            elif ltype == 'limit':
                _lvl(leg.get('symbol') or sym, 'tp', leg.get('limit_price'))
    return out


def monitor_plan(positions, protection, slip_pct, resting_exit_syms=frozenset()):
    """Pure: decide which positions breached their stop/TP level after hours.

    positions: [{'symbol','side','qty','current_price'}]
    protection: {symbol: {'stop','tp'}} (from protection_map)
    resting_exit_syms: symbols that already carry an ahsx_ exit (idempotency)
    Returns [{'ticker','side','qty','limit','reason','level','current','stop'}]
    — side is the EXIT side; 'stop' carries the protective level for restore.
    """
    out = []
    for p in positions:
        sym = p.get('symbol')
        side = (p.get('side') or '').lower()
        try:
            qty = abs(float(p.get('qty') or 0))
            current = float(p.get('current_price') or 0)
        except (TypeError, ValueError):
            continue
        if not sym or qty <= 0 or current <= 0 or side not in ('long', 'short'):
            continue
        if sym in resting_exit_syms:
            continue
        levels = protection.get(sym) or {}
        stop, tp = levels.get('stop'), levels.get('tp')
        reason = None
        if stop and ((side == 'long' and current <= stop) or
                     (side == 'short' and current >= stop)):
            reason, level = 'stop_breach', stop
        elif tp and ((side == 'long' and current >= tp) or
                     (side == 'short' and current <= tp)):
            reason, level = 'tp_reach', tp
        if not reason:
            continue
        exit_side = 'sell' if side == 'long' else 'buy'
        limit = current * (1 - slip_pct) if exit_side == 'sell' else current * (1 + slip_pct)
        out.append({'ticker': sym, 'side': exit_side, 'qty': int(qty),
                    'limit': round(limit, 2), 'reason': reason,
                    'level': level, 'current': current, 'stop': stop})
    return out


def run_stop_monitor(dry_run: bool) -> dict:
    """Effectful monitor pass. Skips during RTH (the GTC OCO owns RTH exits)."""
    from execution.stop_reattach import (fetch_positions, cancel_stops_for,
                                         _wait_qty_freed, submit_protective_stop,
                                         _post_alert)
    stats = {'checked': 0, 'exits': 0, 'restored': 0, 'failed': 0}
    if not stop_monitor_on():
        log('OPENCLAW_AFTERHOURS_STOP_MONITOR!=1 — skipping')
        return stats
    ok, clk, _ = _cli(['clock'])
    if not ok or not isinstance(clk, dict):
        log('clock unavailable — skipping (never act on unknown session state)')
        return stats
    if clk.get('is_open'):
        log('market open — RTH exits belong to the resting OCO; skipping')
        return stats
    ok, orders, _ = _cli(['order', 'list', '--status', 'open', '--nested',
                          '--limit', '500'])
    if not ok:
        log('order list failed — skipping')
        return stats
    resting_exits = {o.get('symbol') for o in (orders or [])
                     if (o.get('client_order_id') or '').startswith('ahsx_')}
    positions = list(fetch_positions())
    plan = monitor_plan(positions, protection_map(orders), _slip_pct(),
                        resting_exit_syms=resting_exits)
    stats['checked'] = len(positions)
    for a in plan:
        sym = a['ticker']
        log(f"  {sym}: {a['reason']} (cur={a['current']:.2f} level={a['level']:.2f}) "
            f"→ ext-hours {a['side'].upper()} LIMIT x{a['qty']} @ {a['limit']:.2f}")
        if dry_run:
            stats['exits'] += 1
            continue
        pos_side = 'long' if a['side'] == 'sell' else 'short'
        cancel_stops_for(sym, False, include_reserving=True, exit_side=a['side'])
        if not _wait_qty_freed(sym, a['qty']):
            # Cancels may not have landed — protection likely still resting.
            # Re-assert a bare GTC stop so the RTH open is covered regardless.
            log(f'  ⚠ {sym}: shares not freed — restoring stop, no ext exit')
            if a.get('stop'):
                submit_protective_stop(ticker=sym, position_side=pos_side,
                                       qty=a['qty'], stop_price=a['stop'],
                                       dry_run=False)
            stats['failed'] += 1
            continue
        coid = f"ahsx_{sym}_{int(datetime.now(timezone.utc).timestamp())}"
        ok_s, _pay, err = _submit_limit(
            ticker=sym, side=a['side'], qty=a['qty'], limit_price=a['limit'],
            tif='day', order_class='simple', order_type='limit',
            extended_hours=True, coid=coid)
        if ok_s:
            stats['exits'] += 1
            _post_alert(f"🌙 **after-hours exit** {sym}: {a['reason']} "
                        f"cur={a['current']:.2f} level={a['level']:.2f} — "
                        f"{a['side']} limit x{a['qty']} @ {a['limit']:.2f} placed")
        else:
            stats['failed'] += 1
            log(f"  ✘ {sym} ext-hours exit failed: {(err or {}).get('error','?')} "
                f"— restoring protective stop")
            if a.get('stop'):
                r = submit_protective_stop(ticker=sym, position_side=pos_side,
                                           qty=a['qty'], stop_price=a['stop'],
                                           dry_run=False)
                if r.get('status') == 'submitted':
                    stats['restored'] += 1
            _post_alert(f"⚠️ after-hours exit FAILED for {sym} "
                        f"({(err or {}).get('error','?')}) — bare stop restored")
    return stats


def main(argv=None) -> int:
    import argparse
    from execution.stop_reattach import (fetch_positions, fetch_tp_covered,
                                         latest_broker_bracket)
    ap = argparse.ArgumentParser()
    ap.add_argument('--reconcile', action='store_true',
                    help='session-boundary reconcile instead of placement')
    ap.add_argument('--monitor', action='store_true',
                    help='ext-hours stop/TP breach monitor (downside protection)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(argv)
    if args.monitor:
        log(f'stop-monitor: {run_stop_monitor(args.dry_run)}')
        return 0
    if not afterhours_tp_on():
        log('OPENCLAW_AFTERHOURS_TP!=1 — skipping')
        return 0
    if args.reconcile:
        log(f'reconcile: {reconcile_afterhours(args.dry_run)}')
        return 0
    # Session-boundary reconcile FIRST (clear the prior session's ext-hours TPs
    # and resize any stop a filled TP left oversized), THEN place fresh TPs.
    log(f'reconcile: {reconcile_afterhours(args.dry_run)}')
    positions = list(fetch_positions())
    plan = desired_tps(positions, latest_broker_bracket,
                       tp_covered=fetch_tp_covered())
    log(f'placing {len(plan)} ext-hours TP(s)')
    _place_plan(plan, args.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
