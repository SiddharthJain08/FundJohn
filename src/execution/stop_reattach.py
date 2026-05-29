#!/usr/bin/env python3
"""
stop_reattach.py — pipeline step that ensures every open equity position
has an active GTC protective stop.

Why this exists
---------------
Alpaca bracket orders submit with `tif=day`. The take-profit and stop-loss
children inherit that TIF and are canceled/expired by Alpaca at EOD (the
OCO group dies when either leg terminates). The next morning the position
is naked. Compounding sources of naked positions:
  - Shorts open as `order_class='simple'` (Alpaca rejects bracket-on-short)
    — never had a broker stop in the first place.
  - Close/reverse orders fall back to `simple` for the same reason.
  - Extended-hours orders are always `simple` + `limit` — no bracket.
  - Bracket parents that fill before EOD: children die at EOD.

Production audit 2026-05-29: 26 open positions, 0 active stops; of the last
500 orders, 62 stop legs (status=canceled, day-TIF) vs 6 stop legs (filled).
91% of intended stop protection evaporated nightly.

Behavior
--------
1. Fetch broker positions (`alpaca position list`) — equity only; crypto
   has its own resting stop path in alpaca_executor._submit_crypto_stop.
2. Fetch open orders (`alpaca order list --status open`) and index any
   stop / stop_limit orders by symbol.
3. For each position not fully covered by an existing stop, look up the
   most recent `alpaca_submissions` row with `stop_price IS NOT NULL` and
   compute a fresh stop level: preserves the strategy's original stop_pct
   (|entry - stop| / entry) applied against the position's current
   `avg_entry_price`, so risk-per-position stays consistent even if the
   position averaged in over multiple fills.
4. Submit a standalone STOP order with `tif=gtc`, opposite side of the
   position, qty = position_qty - already_covered_qty.

Gate: OPENCLAW_STOP_REATTACH (default ON). Set to 0 to disable.
Dry-run: --dry-run prints the planned submissions without firing.

Usage:
    python3 src/execution/stop_reattach.py [--date YYYY-MM-DD] [--dry-run]

Exit codes:
    0 — success (including no-op when there's nothing to reattach)
    1 — POSTGRES_URI missing, broker query failed catastrophically
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def log(msg: str) -> None:
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'{ts} [STOP_REATTACH] {msg}')


def _gate_on() -> bool:
    return os.environ.get('OPENCLAW_STOP_REATTACH', '1') != '0'


def _run_cli(args, timeout=15):
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
           'error': proc.stderr.strip()}
    try:
        ej = json.loads(proc.stderr)
        err.update({'status': ej.get('status'), 'code': ej.get('code'),
                    'error': ej.get('error') or err['error']})
    except json.JSONDecodeError:
        pass
    return False, None, err


def fetch_positions() -> list[dict]:
    """Equity-only open positions. Crypto has a parallel stop path in
    alpaca_executor — skipping here keeps that contract clean."""
    ok, payload, err = _run_cli(['position', 'list'])
    if not ok:
        log(f'position list failed: {(err or {}).get("error","unknown")}')
        return []
    positions = payload or []
    return [p for p in positions if p.get('asset_class') == 'us_equity']


def fetch_active_stops() -> dict[str, float]:
    """Map ticker -> total qty already covered by an active stop / stop_limit
    order. Sums across multiple stops on the same symbol so a position with
    2 partial stops is counted correctly."""
    ok, payload, err = _run_cli(['order', 'list', '--status', 'open'])
    if not ok:
        log(f'order list failed: {(err or {}).get("error","unknown")} — treating as no active stops')
        return {}
    out: dict[str, float] = {}
    for o in payload or []:
        t = (o.get('type') or o.get('order_type') or '').lower()
        if t in ('stop', 'stop_limit') and o.get('stop_price'):
            sym = o.get('symbol')
            try:
                q = float(o.get('qty') or 0)
            except (TypeError, ValueError):
                q = 0.0
            if sym and q > 0:
                out[sym] = out.get(sym, 0.0) + q
    return out


def latest_stop_submission(conn, ticker: str, position_side: str) -> dict | None:
    """Most recent alpaca_submissions row for this ticker whose direction
    matches the position's side AND records a non-zero stop_price.

    Direction matching matters because the same ticker often cycles
    long → short → long via close-then-open submissions; without it we'd
    pick the latest row regardless of side (e.g. a stop=0 simple short
    close-out following a still-open long bracket).

    `broker_status` is NOT filtered: recent brackets often carry
    broker_status=NULL when reconcile hasn't propagated yet, and excluding
    them would skip the very rows that carry the live stop_price.
    Filtering on `stop_price > 0` is the durable signal — simple closes
    write stop_price=0, brackets and reverse-opens write the real stop."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute('''
        SELECT strategy_id, ticker, direction, entry_price, stop_price,
               target_price, filled_avg_price, order_class, broker_status,
               submitted_at
          FROM alpaca_submissions
         WHERE ticker = %s
           AND direction = %s
           AND stop_price > 0
         ORDER BY submitted_at DESC
         LIMIT 1
    ''', (ticker, position_side))
    row = cur.fetchone()
    cur.close()
    return dict(row) if row else None


def _compute_new_stop(position: dict, submission: dict) -> tuple[float | None, str]:
    """Apply the submission's stop_pct against the position's current
    avg_entry_price so the risk envelope tracks the actual cost basis even
    if multiple fills averaged in at different prices.

    Returns (stop_price, status). status is one of:
      'ok'          — stop is valid + on the correct side of current price
      'degenerate'  — math couldn't produce a positive stop
      'breached'    — the computed stop is already on the WRONG side of
                      current price (i.e. it would either be rejected by
                      Alpaca or trigger immediately). Position is past its
                      intended stop and needs operator attention; we DO NOT
                      submit a stop that fires on first tick — that's the
                      pipeline's job to detect and exit, not the
                      stop-reattach helper's job to mask."""
    try:
        sub_entry = float(submission['entry_price'])
        sub_stop  = float(submission['stop_price'])
        avg       = float(position.get('avg_entry_price') or 0)
        current   = float(position.get('current_price') or 0)
        if sub_entry <= 0 or avg <= 0:
            return None, 'degenerate'
    except (TypeError, ValueError, KeyError):
        return None, 'degenerate'

    side = (position.get('side') or '').lower()
    if side == 'long':
        stop_pct = (sub_entry - sub_stop) / sub_entry   # positive for longs
        if stop_pct <= 0:
            return None, 'degenerate'
        new_stop = avg * (1 - stop_pct)
        # Long sell-stop must be STRICTLY BELOW current; otherwise Alpaca
        # rejects with "stop_price must be < base_price" or triggers on
        # first tick. Position is past its strategy's risk envelope.
        if current > 0 and new_stop >= current:
            return round(new_stop, 2), 'breached'
    elif side == 'short':
        stop_pct = (sub_stop - sub_entry) / sub_entry   # positive for shorts
        if stop_pct <= 0:
            return None, 'degenerate'
        new_stop = avg * (1 + stop_pct)
        # Short buy-stop must be STRICTLY ABOVE current.
        if current > 0 and new_stop <= current:
            return round(new_stop, 2), 'breached'
    else:
        return None, 'degenerate'
    if new_stop <= 0:
        return None, 'degenerate'
    return round(new_stop, 2), 'ok'


def submit_protective_stop(*, ticker: str, position_side: str, qty: float,
                           stop_price: float, dry_run: bool) -> dict:
    """Stand-alone STOP order with tif=gtc, side opposite the position."""
    side = 'sell' if position_side == 'long' else 'buy'
    coid = f'sr_{ticker}_{int(datetime.utcnow().timestamp())}'
    args = [
        'order', 'submit',
        '--symbol',          ticker,
        '--side',            side,
        '--qty',             str(int(qty)),
        '--type',            'stop',
        '--time-in-force',   'gtc',
        '--stop-price',      f'{stop_price:.2f}',
        '--client-order-id', coid,
    ]
    if dry_run:
        log(f'  DRY-RUN {ticker} {side.upper()} STOP qty={int(qty)} @ {stop_price:.2f}  coid={coid}')
        return {'ticker': ticker, 'status': 'dry_run', 'side': side, 'qty': qty,
                'stop_price': stop_price, 'coid': coid}
    ok, payload, err = _run_cli(args, timeout=15)
    if ok:
        oid = (payload or {}).get('id', '?') if isinstance(payload, dict) else '?'
        log(f'  ✔ {ticker} {side.upper()} STOP qty={int(qty)} @ {stop_price:.2f}  order={oid}')
        return {'ticker': ticker, 'status': 'submitted', 'side': side, 'qty': qty,
                'stop_price': stop_price, 'order_id': oid, 'coid': coid}
    err_msg = (err or {}).get('error', 'unknown')
    log(f'  ✗ {ticker} {side.upper()} STOP submit failed: {err_msg}')
    return {'ticker': ticker, 'status': 'rejected', 'side': side, 'qty': qty,
            'stop_price': stop_price, 'error': err_msg, 'coid': coid}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not _gate_on():
        log('OPENCLAW_STOP_REATTACH=0 — skipping')
        return 0

    pg_uri = os.environ.get('POSTGRES_URI')
    if not pg_uri:
        log('POSTGRES_URI not set')
        return 1

    positions = fetch_positions()
    log(f'fetched {len(positions)} equity positions')
    if not positions:
        return 0

    covered = fetch_active_stops()
    log(f'active stops cover {len(covered)} symbols (total {sum(covered.values()):.0f} shares)')

    plans = []
    breached: list[dict] = []
    skips: dict[str, int] = {'already_covered': 0, 'no_submission_row': 0,
                              'degenerate_stop': 0, 'unknown_side': 0,
                              'past_stop_breached': 0}
    conn = psycopg2.connect(pg_uri)
    try:
        for pos in positions:
            sym = pos.get('symbol')
            try:
                pos_qty = abs(float(pos.get('qty') or 0))
            except (TypeError, ValueError):
                pos_qty = 0.0
            if pos_qty <= 0 or not sym:
                continue
            already = covered.get(sym, 0.0)
            need = pos_qty - already
            if need <= 0.01:
                skips['already_covered'] += 1
                continue

            side = (pos.get('side') or '').lower()
            sub = latest_stop_submission(conn, sym, side)
            if not sub:
                skips['no_submission_row'] += 1
                log(f'  {sym}: no submission row with stop_price — cannot reattach')
                continue
            new_stop, status = _compute_new_stop(pos, sub)
            if status == 'degenerate':
                skips['degenerate_stop'] += 1
                log(f'  {sym}: stop calc degenerate (side={side} avg={pos.get("avg_entry_price")} '
                    f'sub_entry={sub["entry_price"]} sub_stop={sub["stop_price"]})')
                continue
            if status == 'breached':
                skips['past_stop_breached'] += 1
                breached.append({'ticker': sym, 'side': side,
                                 'current': float(pos.get('current_price') or 0),
                                 'breached_stop': new_stop,
                                 'unrealized_pl': float(pos.get('unrealized_pl') or 0),
                                 'qty': need})
                log(f'  ⚠ {sym}: PAST STOP (side={side} current={pos.get("current_price")} '
                    f'computed_stop={new_stop}) — position breached risk envelope, NOT submitting reactive stop')
                continue
            if side not in ('long', 'short'):
                skips['unknown_side'] += 1
                continue
            plans.append({'ticker': sym, 'position_side': side, 'qty': need,
                          'stop_price': new_stop})

        log(f'plan: {len(plans)} stops to attach; skips: {skips}')
        results = []
        for p in plans:
            r = submit_protective_stop(
                ticker=p['ticker'], position_side=p['position_side'],
                qty=p['qty'], stop_price=p['stop_price'], dry_run=args.dry_run,
            )
            results.append(r)

        submitted = sum(1 for r in results if r['status'] == 'submitted')
        rejected  = sum(1 for r in results if r['status'] == 'rejected')
        dry       = sum(1 for r in results if r['status'] == 'dry_run')
        log(f'done: {submitted} submitted, {rejected} rejected, {dry} dry-run, '
            f'{skips["already_covered"]} already_covered, {skips["no_submission_row"]} no_submission, '
            f'{skips["past_stop_breached"]} past_stop_breached')
        if breached:
            log(f'⚠ {len(breached)} positions PAST their strategy stop — operator review needed (manual close or accept):')
            for b in breached:
                log(f'    {b["ticker"]:6} {b["side"]:5} cur={b["current"]:.2f} breached_stop={b["breached_stop"]:.2f} '
                    f'unrealized=${b["unrealized_pl"]:.2f} qty={b["qty"]:.0f}')
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
