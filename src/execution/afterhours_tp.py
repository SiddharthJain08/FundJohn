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
import json
import os
import sys
import time
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
    # `order list` returns 50 rows by DEFAULT (cap 500). An unbounded read on a
    # 200+-order breadth book silently dropped the ahtp_* TPs and oversized
    # stops past row 50 — they were never cancelled/resized. Keyset-paginate
    # (--after-order-id, --direction asc) until a short page, as
    # stop_reattach._fetch_open_orders does.
    orders, cursor = [], None
    for _page in range(40):
        args = ['order', 'list', '--status', 'open', '--limit', '500', '--direction', 'asc']
        if cursor:
            args += ['--after-order-id', cursor]
        ok, page, _ = _cli(args, timeout=30)
        if not ok:
            return stats
        page = page or []
        orders.extend(page)
        if len(page) < 500:
            break
        cursor = page[-1].get('id')
        if not cursor:
            break
    pos_qty = {p['symbol']: abs(float(p.get('qty') or 0))
               for p in (fetch_positions() or [])}
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


def _max_spread_pct() -> float:
    try:
        return float(os.environ.get('OPENCLAW_AH_MAX_SPREAD_PCT', '5')) / 100.0
    except ValueError:
        return 0.05


def book_too_wide(reason: str, bid, ask) -> bool:
    """Defer a stop_breach exit when the book is too wide to cross.

    A stop exit prices off the far side, so the spread IS the exit cost:
    NVNO 2026-07-23 08:10Z covered into a 9.85/12.5 premarket book and paid
    18% over its stop. Past the cap the exit waits for a tighter book — the
    journal keeps the debt visible every tick, spreads tighten toward the
    open, and the 09:35 RTH reattach pass is the unconditional backstop.
    tp_reach is never deferred (its limit is floored at the level, so a wide
    book can't degrade it), nor is a one-sided/empty book (reprice falls
    back to last trade, already slip-bounded)."""
    if reason != 'stop_breach' or not bid or not ask:
        return False
    mid = (bid + ask) / 2
    return mid > 0 and (ask - bid) / mid > _max_spread_pct()


_INTENTS_TTL_S = 24 * 3600


def _intents_path() -> _Path:
    return _Path(os.environ.get(
        'OPENCLAW_AH_INTENTS_PATH',
        '/root/openclaw/logs/afterhours_pending_exits.json'))


def _load_intents() -> dict:
    """{symbol: {'stop','tp','ts'}} — protection WE cancelled but never
    replaced (exit unsubmitted and stop-restore failed). Without this journal
    a slow broker cancel makes the position invisible to every later tick:
    the orders are gone, so protection_map has no levels to act on
    (2026-07-21: 8 TP-passed positions orphaned exactly this way)."""
    try:
        raw = json.loads(_intents_path().read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in raw.items()
            if isinstance(v, dict) and now - float(v.get('ts', 0)) < _INTENTS_TTL_S}


def _save_intents(intents: dict) -> None:
    p = _intents_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(intents))
        os.replace(tmp, p)
    except OSError as e:
        log(f'⚠ pending-exit journal write failed: {e}')


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


def _latest_quote(sym: str):
    """(bid, ask) from the live book, (None, None) when unavailable. Only
    called for symbols the monitor is about to exit — not per-position."""
    ok, payload, _ = _cli(['data', 'latest-quote', '--symbol', sym])
    if not ok or not isinstance(payload, dict):
        return None, None
    q = payload.get('quote') or {}
    def _f(v):
        try:
            v = float(v)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    return _f(q.get('bp')), _f(q.get('ap'))


def reprice_exit(action: dict, bid, ask, slip_pct: float) -> float:
    """Pure: final exit limit from the live book instead of the last trade.

    The last trade can be hours stale on thin names (CLYM 2026-07-21: last
    12.90 was Monday's close, live book 12.25/12.80 — the last-based
    'marketable' limit sat above the entire book and could never fill).

    Sells price off the BID, buys off the ASK (the side an exit actually
    crosses), falling back to the last trade when the book side is empty.
    tp_reach limits are floored (long) / capped (short) at the TP level —
    never exit worse than target through this path; if nobody trades at
    target, the order rests until someone does. stop_breach limits stay
    unfloored: the position has already broken its risk envelope and the
    slip-bounded marketable price is the point.
    """
    side, reason, level = action['side'], action['reason'], action['level']
    if side == 'sell':
        ref = bid if bid else action['current']
        raw = ref * (1 - slip_pct)
        limit = max(level, raw) if reason == 'tp_reach' else raw
    else:
        ref = ask if ask else action['current']
        raw = ref * (1 + slip_pct)
        limit = min(level, raw) if reason == 'tp_reach' else raw
    return round(limit, 2)


def run_stop_monitor(dry_run: bool) -> dict:
    """Effectful monitor pass. Skips during RTH (the GTC OCO owns RTH exits)."""
    from execution.stop_reattach import (fetch_positions, cancel_stops_for,
                                         _wait_qty_freed, submit_protective_stop,
                                         _post_alert)
    stats = {'checked': 0, 'exits': 0, 'restored': 0, 'failed': 0, 'deferred': 0}
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
    positions = fetch_positions()
    if positions is None:
        # Couldn't ask ≠ flat book. The intent-reconciliation below settles
        # journal debt when a symbol leaves the position list — running it on
        # a failed fetch would wipe every owed exit in one tick.
        log('position list failed — skipping (never settle debt on unknown state)')
        return stats
    protection = protection_map(orders)
    intents = _load_intents()
    pos_syms = {p.get('symbol') for p in positions}
    for sym in list(intents):
        it = intents[sym]
        if sym not in pos_syms:
            intents.pop(sym)             # position closed — debt settled
            continue
        lv = protection.get(sym)
        if lv is not None:
            # Clear only when the OWED leg is resting again. An intent that
            # carries a TP is settled by a resting TP, never by a bare stop:
            # clearing on any protection re-opens the stop-only blind spot
            # (a restored/floor stop hides the TP from every later tick).
            owes_tp = it.get('tp') is not None
            if (owes_tp and lv.get('tp') is not None) or \
               (not owes_tp and lv.get('stop') is not None):
                intents.pop(sym)
                continue
            # Stop-only but a TP is owed (reached-target handoff from the
            # reattach pass, or an OCO cancel that only restored the stop):
            # borrow the journaled TP; a live stop level wins over the
            # journaled one.
            log(f'  ↻ {sym}: journal owes a take-profit — borrowing level onto live protection')
            protection[sym] = {'stop': lv.get('stop') if lv.get('stop') is not None
                               else it.get('stop'),
                               'tp': it.get('tp')}
        else:
            # We cancelled this symbol's protection on an earlier tick and
            # never replaced it (broker cancel outlived _wait_qty_freed, then
            # the bare-stop restore hit insufficient-qty on the still-reserved
            # shares). Resurrect the levels so the plan retries — by now the
            # cancel has completed and the shares are free.
            log(f'  ↻ {sym}: pending-exit journal has unreplaced protection — retrying')
            protection[sym] = {'stop': it.get('stop'), 'tp': it.get('tp')}
    plan = monitor_plan(positions, protection, _slip_pct(),
                        resting_exit_syms=resting_exits)
    stats['checked'] = len(positions)
    for a in plan:
        sym = a['ticker']
        bid, ask = _latest_quote(sym)
        if book_too_wide(a['reason'], bid, ask):
            log(f"  ⏸ {sym}: book too wide to cross ({bid}/{ask}) — "
                f"deferring stop exit to a tighter book / RTH pass")
            stats['deferred'] += 1
            continue
        quoted = reprice_exit(a, bid, ask, _slip_pct())
        if quoted != a['limit']:
            log(f"  {sym}: repriced off live book (bid={bid} ask={ask}): "
                f"{a['limit']:.2f} → {quoted:.2f}")
            a['limit'] = quoted
        log(f"  {sym}: {a['reason']} (cur={a['current']:.2f} level={a['level']:.2f}) "
            f"→ ext-hours {a['side'].upper()} LIMIT x{a['qty']} @ {a['limit']:.2f}")
        if dry_run:
            stats['exits'] += 1
            continue
        pos_side = 'long' if a['side'] == 'sell' else 'short'
        # Journal BEFORE cancelling: if anything past this line fails (or the
        # process dies), the next tick still knows this symbol owes an exit.
        lv = protection.get(sym) or {}
        intents[sym] = {'stop': lv.get('stop'), 'tp': lv.get('tp'),
                        'ts': time.time()}
        _save_intents(intents)
        cancel_stops_for(sym, False, include_reserving=True, exit_side=a['side'])
        if not _wait_qty_freed(sym, a['qty']):
            # Cancels may not have landed — protection likely still resting.
            # Re-assert a bare GTC stop so the RTH open is covered regardless;
            # the journal entry keeps this symbol visible to the next tick
            # even if the restore is rejected on still-reserved shares.
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
            intents.pop(sym, None)
            _save_intents(intents)
            _post_alert(f"🌙 **after-hours exit** {sym}: {a['reason']} "
                        f"cur={a['current']:.2f} level={a['level']:.2f} — "
                        f"{a['side']} limit x{a['qty']} @ {a['limit']:.2f} placed",
                        channel='trade-reports')
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
    if not dry_run:
        _save_intents(intents)
    return stats


# ── exit-fill reporter (2026-07-21) ─────────────────────────────────────────
# Positions that exit via take-profit or stop leave no trace in Discord: the
# GTC OCO / bracket legs fill silently at the broker. Each monitor tick this
# pass scans recently CLOSED orders for filled exit orders (any session) and
# posts one #trade-reports line per fill, deduped via a seen-ids state file.

_EXIT_FILL_LABELS = {
    'take_profit': '✅ **take-profit filled**',
    'stop': '🛑 **stop filled**',
    'ah_take_profit': '🌙 **after-hours take-profit filled**',
    'ah_exit': '🌙 **after-hours exit filled**',
}


def _fills_state_path() -> _Path:
    return _Path(os.environ.get(
        'OPENCLAW_EXIT_FILLS_STATE',
        '/root/openclaw/logs/exit_fills_reported.json'))


def classify_exit_fills(orders) -> list:
    """Pure: walk closed orders (+legs, so OCO stop children are seen) and
    return every FILLED exit order as {id,symbol,side,qty,price,level,kind}.

    Exit orders are: any filled stop/stop_limit (top or leg); the TP limit of
    an oco parent or a bracket/oco leg; and the ext-hours ahtp_/ahsx_ limits.
    Plain top-level limits/markets without an exit coid are ENTRIES — skipped.
    """
    out = []
    def _emit(o, kind, parent_class=None):
        try:
            qty = float(o.get('filled_qty') or 0)
            price = float(o.get('filled_avg_price') or 0)
        except (TypeError, ValueError):
            return
        if qty <= 0 or price <= 0:
            return
        level = o.get('limit_price') if 'take_profit' in kind or kind == 'ah_exit' \
            else o.get('stop_price')
        try:
            level = float(level) if level else None
        except (TypeError, ValueError):
            level = None
        out.append({'id': o.get('id'), 'symbol': o.get('symbol'),
                    'side': (o.get('side') or '').lower(), 'qty': qty,
                    'price': price, 'level': level, 'kind': kind,
                    'filled_at': o.get('filled_at')})

    for top in (orders or []):
        stack = [(top, None)]
        while stack:
            o, parent = stack.pop()
            for leg in (o.get('legs') or []):
                stack.append((leg, o))
            if (o.get('status') or '').lower() != 'filled':
                continue
            otype = (o.get('type') or o.get('order_type') or '').lower()
            oclass = (o.get('order_class') or 'simple').lower()
            coid = o.get('client_order_id') or ''
            if otype in ('stop', 'stop_limit'):
                _emit(o, 'stop')
            elif coid.startswith('ahtp_'):
                _emit(o, 'ah_take_profit')
            elif coid.startswith('ahsx_'):
                _emit(o, 'ah_exit')
            elif otype == 'limit' and parent is None and oclass == 'oco':
                _emit(o, 'take_profit')          # reattach OCO parent IS the TP
            elif otype == 'limit' and parent is not None:
                _emit(o, 'take_profit')          # bracket/oco child TP leg
    return out


def run_exit_fill_reporter(dry_run: bool) -> dict:
    """Report newly-filled exit orders to #trade-reports. First run seeds the
    seen-set silently so history doesn't flood the channel."""
    from execution.stop_reattach import _post_alert
    stats = {'fills_seen': 0, 'reported': 0}
    ok, orders, _ = _cli(['order', 'list', '--status', 'closed', '--nested',
                          '--limit', '200'])
    if not ok:
        log('fill-reporter: order list failed — skipping')
        return stats
    fills = classify_exit_fills(orders)
    stats['fills_seen'] = len(fills)
    state_p = _fills_state_path()
    first_run = not state_p.exists()
    try:
        seen_list = list(json.loads(state_p.read_text()).get('seen', []))
    except (OSError, ValueError):
        seen_list = []
    seen = set(seen_list)
    new = [f for f in fills if f['id'] and f['id'] not in seen]
    for f in new:
        seen.add(f['id'])
        seen_list.append(f['id'])
        if first_run:
            continue                     # seed silently, no history flood
        lvl = f" (level {f['level']:.2f})" if f['level'] else ''
        msg = (f"{_EXIT_FILL_LABELS[f['kind']]} {f['symbol']}: "
               f"{f['side'].upper()} {f['qty']:g} @ {f['price']:.2f}{lvl} — "
               f"${f['qty'] * f['price']:,.0f}")
        log(f'  {msg}')
        stats['reported'] += 1
        if not dry_run:
            _post_alert(msg, channel='trade-reports')
    if not dry_run:
        try:
            state_p.parent.mkdir(parents=True, exist_ok=True)
            tmp = state_p.with_suffix('.tmp')
            tmp.write_text(json.dumps({'seen': seen_list[-800:]}))
            os.replace(tmp, state_p)
        except OSError as e:
            log(f'⚠ fill-reporter state write failed: {e}')
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
        # Fill reporter first (session-agnostic — RTH OCO/bracket fills are
        # the common case); the stop monitor still skips during RTH itself.
        log(f'fill-reporter: {run_exit_fill_reporter(args.dry_run)}')
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
    positions = fetch_positions() or []
    plan = desired_tps(positions, latest_broker_bracket,
                       tp_covered=fetch_tp_covered())
    log(f'placing {len(plan)} ext-hours TP(s)')
    _place_plan(plan, args.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
