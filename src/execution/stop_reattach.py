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
import requests

ROOT = Path(__file__).resolve().parents[2]
ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def log(msg: str) -> None:
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'{ts} [STOP_REATTACH] {msg}')


def _gate_on() -> bool:
    return os.environ.get('OPENCLAW_STOP_REATTACH', '1') != '0'


def _reattach_from_broker_on() -> bool:
    return os.environ.get('OPENCLAW_REATTACH_FROM_BROKER') == '1'


def _resolve_intended_bracket(conn, ticker: str, side: str):
    """Return a submission-shaped dict {'entry_price','stop_price','target_price'}
    representing the latest run's intended bracket. Prefers the broker's
    last-placed bracket legs (W2); falls back to the DB submission row.
    entry_price for the broker path is taken from the DB row (the broker leg
    carries no entry) so the existing pct re-anchor math is unchanged."""
    if _reattach_from_broker_on():
        b = latest_broker_bracket(ticker, side)
        if b:
            db = latest_stop_submission(conn, ticker, side) or {}
            entry = b.get('entry') or db.get('entry_price')
            if entry:
                return {'entry_price': float(entry),
                        'stop_price': b['stop'], 'target_price': b['target']}
    return latest_stop_submission(conn, ticker, side)


_RATE_LIMIT_BACKOFF = (2, 5, 10)


def _is_rate_limited(err: dict | None) -> bool:
    if not err:
        return False
    return err.get('status') == 429 or 'rate limit' in str(err.get('error', '')).lower()


def _run_cli(args, timeout=15):
    import time
    for attempt, backoff in enumerate((*_RATE_LIMIT_BACKOFF, None)):
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
        if backoff is None or not _is_rate_limited(err):
            return False, None, err
        log(f'  rate-limited ({args[0]} {args[1] if len(args) > 1 else ""}) — retry in {backoff}s')
        time.sleep(backoff)
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


def latest_broker_bracket(ticker: str, position_side: str) -> dict | None:
    """Most-recent terminal bracket order for `ticker` on the side that OPENED
    `position_side`, with its real take-profit + stop leg prices read back from
    Alpaca order history. This is the levels the LAST run actually placed —
    independent of the alpaca_submissions audit row.

    Returns {'entry': None, 'stop': float, 'target': float, 'order_id': str}
    or None when no bracket with both legs is found."""
    open_side = 'buy' if position_side == 'long' else 'sell'
    ok, payload, err = _run_cli(['order', 'list', '--status', 'all',
                                 '--nested', '--limit', '500'])
    if not ok:
        log(f'order history fetch failed ({(err or {}).get("error","unknown")})')
        return None
    cands = []
    for o in (payload or []):
        if o.get('symbol') != ticker or o.get('side') != open_side:
            continue
        if (o.get('order_class') or '') != 'bracket':
            continue
        tp = stp = None
        for leg in (o.get('legs') or []):
            ltype = (leg.get('type') or leg.get('order_type') or '').lower()
            if ltype == 'limit' and leg.get('limit_price'):
                tp = float(leg['limit_price'])
            elif ltype in ('stop', 'stop_limit') and leg.get('stop_price'):
                stp = float(leg['stop_price'])
        if tp and stp:
            cands.append((o.get('submitted_at') or '', tp, stp, o.get('id') or ''))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])           # ascending submitted_at
    _, tp, stp, oid = cands[-1]              # most recent
    return {'entry': None, 'stop': stp, 'target': tp, 'order_id': oid}


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


# ──────────────────────────────────────────────────────────────────────────
# Protective OCO (take-profit + stop, linked). The Alpaca CLI cannot emit
# order_class=oco with a stop leg (it coerces to bracket), so OCO exits go
# via REST. An OCO is placed ONLY when both legs straddle the current price
# (stop on the loss side, target on the profit side); otherwise we leave the
# position to the bare-stop floor reattach below.
# ──────────────────────────────────────────────────────────────────────────

def _compute_new_target(position: dict, submission: dict) -> tuple[float | None, str]:
    """Mirror of _compute_new_stop for the take-profit leg. Applies the
    submission's target_pct against the position's avg entry.

    Returns (target_price, status):
      'ok'         — target is on the correct (profit) side of current price
      'degenerate' — no usable target in the submission
      'reached'    — current price is already at/past the target; the position
                     should be CLOSED (handled by the daily cycle), not given a
                     resting take-profit that would fill instantly."""
    try:
        sub_entry  = float(submission['entry_price'])
        sub_target = float(submission.get('target_price') or 0)
        avg        = float(position.get('avg_entry_price') or 0)
        current    = float(position.get('current_price') or 0)
        if sub_entry <= 0 or avg <= 0 or sub_target <= 0:
            return None, 'degenerate'
    except (TypeError, ValueError, KeyError):
        return None, 'degenerate'

    side = (position.get('side') or '').lower()
    if side == 'long':
        target_pct = (sub_target - sub_entry) / sub_entry   # positive for longs
        if target_pct <= 0:
            return None, 'degenerate'
        new_target = avg * (1 + target_pct)
        if current > 0 and new_target <= current:
            return round(new_target, 2), 'reached'
    elif side == 'short':
        target_pct = (sub_entry - sub_target) / sub_entry   # positive for shorts
        if target_pct <= 0:
            return None, 'degenerate'
        new_target = avg * (1 - target_pct)
        if current > 0 and new_target >= current:
            return round(new_target, 2), 'reached'
    else:
        return None, 'degenerate'
    if new_target <= 0:
        return None, 'degenerate'
    return round(new_target, 2), 'ok'


def _oco_order_body(ticker: str, exit_side: str, qty: float,
                    target_price: float, stop_price: float, coid: str) -> dict:
    """Alpaca REST OCO exit body (take-profit limit + stop, one-cancels-other).
    exit_side = 'sell' to close a long, 'buy' to close a short. No top-level
    limit_price — that conflicting field is what made the CLI coerce to
    bracket and invert the leg relationship."""
    return {
        'symbol':         ticker,
        'qty':            str(int(qty)),
        'side':           exit_side,
        'type':           'limit',
        'time_in_force':  'gtc',
        'order_class':    'oco',
        'take_profit':    {'limit_price': f'{target_price:.2f}'},
        'stop_loss':      {'stop_price':  f'{stop_price:.2f}'},
        'client_order_id': coid,
    }


def _alpaca_rest_post(path: str, body: dict, timeout: int = 15):
    """POST to the Alpaca trading REST API. Returns (ok, payload, err)."""
    base = os.environ.get('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets').rstrip('/')
    key = os.environ.get('ALPACA_API_KEY') or os.environ.get('APCA_API_KEY_ID')
    sec = (os.environ.get('ALPACA_SECRET_KEY') or os.environ.get('ALPACA_API_SECRET')
           or os.environ.get('APCA_API_SECRET_KEY'))
    headers = {'APCA-API-KEY-ID': key or '', 'APCA-API-SECRET-KEY': sec or '',
               'Content-Type': 'application/json'}
    import time
    err = {'status': None, 'error': 'not attempted'}
    for backoff in (*_RATE_LIMIT_BACKOFF, None):
        try:
            r = requests.post(f'{base}{path}', json=body, headers=headers, timeout=timeout)
        except Exception as e:
            return False, None, {'status': None, 'error': str(e)}
        if r.status_code == 200:
            try:
                return True, r.json(), None
            except Exception:
                return True, {}, None
        try:
            ej = r.json()
        except Exception:
            ej = {'message': r.text[:300]}
        err = {'status': r.status_code,
               'error': ej.get('message') or ej.get('error') or r.text[:300],
               'code': ej.get('code')}
        if backoff is None or not _is_rate_limited(err):
            return False, None, err
        log(f'  rate-limited (REST {path}) — retry in {backoff}s')
        time.sleep(backoff)
    return False, None, err


def submit_protective_oco(*, ticker: str, position_side: str, qty: float,
                          stop_price: float, target_price: float, dry_run: bool) -> dict:
    """Place a GTC OCO exit (take-profit + stop) via REST."""
    exit_side = 'sell' if position_side == 'long' else 'buy'
    coid = f'oco_{ticker}_{int(datetime.utcnow().timestamp())}'
    body = _oco_order_body(ticker, exit_side, qty, target_price, stop_price, coid)
    if dry_run:
        log(f'  DRY-RUN {ticker} OCO {exit_side.upper()} qty={int(qty)} '
            f'TP={target_price:.2f} STOP={stop_price:.2f}  body={json.dumps(body)}')
        return {'ticker': ticker, 'status': 'dry_run', 'side': exit_side, 'qty': qty,
                'target_price': target_price, 'stop_price': stop_price, 'coid': coid}
    ok, payload, err = _alpaca_rest_post('/v2/orders', body)
    if ok:
        oid = (payload or {}).get('id', '?') if isinstance(payload, dict) else '?'
        log(f'  ✔ {ticker} OCO {exit_side.upper()} qty={int(qty)} '
            f'TP={target_price:.2f} STOP={stop_price:.2f}  order={oid}')
        return {'ticker': ticker, 'status': 'submitted', 'side': exit_side, 'qty': qty,
                'target_price': target_price, 'stop_price': stop_price,
                'order_id': oid, 'coid': coid}
    em = (err or {}).get('error', 'unknown')
    log(f'  ✗ {ticker} OCO submit failed: {em}')
    return {'ticker': ticker, 'status': 'rejected', 'side': exit_side, 'qty': qty,
            'target_price': target_price, 'stop_price': stop_price,
            'error': em, 'coid': coid}


def fetch_tp_covered(linked_only: bool = False) -> dict[str, float]:
    """Map ticker -> qty covered by a resting LIMIT (take-profit) leg.

    Two callers, two meanings:
      - afterhours_tp uses the permissive DEFAULT (count every resting limit) so
        it doesn't double-place its own ext-hours TPs on re-runs (idempotency).
      - stop_reattach passes ``linked_only=True``: only the limit leg of an
        OCO/bracket (order_class in {'oco','bracket'}) counts as coverage,
        because only a LINKED limit carries a stop leg. A standalone
        order_class='simple' TP (e.g. afterhours_tp's `ahtp_*` day-TIF limit)
        gives ZERO downside protection and must NOT mask a missing stop — that
        conflation left 12 positions naked 2026-06-16→06-18 (the bare 16:05 TP
        made stop_reattach skip the GTC stop, then the day-TIF TP expired)."""
    ok, payload, err = _run_cli(['order', 'list', '--status', 'open'])
    cov: dict[str, float] = {}
    if not ok:
        log(f'order list failed (tp cover): {(err or {}).get("error","unknown")}')
        return cov
    for o in (payload or []):
        otype = o.get('order_type') or o.get('type')
        if otype != 'limit':
            continue
        if linked_only and (o.get('order_class') or 'simple') not in ('oco', 'bracket'):
            continue
        try:
            q = abs(float(o.get('qty') or 0))
        except (TypeError, ValueError):
            q = 0.0
        sym = o.get('symbol')
        if sym:
            cov[sym] = cov.get(sym, 0.0) + q
    return cov


def cancel_stops_for(symbol: str, dry_run: bool, include_reserving: bool = False,
                     exit_side: str | None = None) -> int:
    """Cancel resting orders on a symbol so its shares are free for an OCO.

    Default: only bare stop/stop_limit orders (the historical behavior).
    include_reserving=True additionally cancels EXIT-SIDE limit orders that
    RESERVE shares against a full-size OCO — bare simple-class limit TPs
    (afterhours ahtp_*) and partial OCO parents. Full-size OCO placement
    needs the whole position free; leaving these resting is what produced
    the chronic "insufficient qty available" naked positions. Only orders
    whose side == exit_side are touched so a resting ENTRY limit (position
    direction) survives. Safe because the caller's failure path re-submits
    a full-qty bare stop.
    Returns count canceled."""
    ok, payload, err = _run_cli(['order', 'list', '--status', 'open'])
    if not ok:
        return 0
    n = 0
    for o in (payload or []):
        if o.get('symbol') != symbol:
            continue
        otype = o.get('order_type') or o.get('type')
        if otype not in ('stop', 'stop_limit'):
            if not include_reserving or otype != 'limit':
                continue
            if exit_side and (o.get('side') or '').lower() != exit_side:
                continue
        oid = o.get('id')
        if not oid:
            continue
        if dry_run:
            log(f'  DRY-RUN cancel {otype} {symbol} {oid}')
            n += 1
            continue
        ok2, _, _ = _run_cli(['order', 'cancel', '--order-id', oid])
        if ok2:
            n += 1
        else:
            log(f'  ⚠ {symbol}: {otype} cancel failed for {oid}')
    return n


def _post_alert(msg: str, channel: str = 'data-alerts') -> None:
    """Best-effort Discord post via the persisted agent_registry webhook map
    (same channel map pipeline_orchestrator.notify uses). Ops alerts go to
    #data-alerts (default); trade lifecycle events go to #trade-reports. The
    naked-position audit is worthless if it only reaches a log file nobody
    tails — SNDK/XENE sat breached+naked for 3 days that way."""
    try:
        conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        cur = conn.cursor()
        cur.execute("SELECT webhook_urls FROM agent_registry WHERE webhook_urls IS NOT NULL")
        url = None
        for (hooks,) in cur.fetchall():
            if hooks and hooks.get(channel):
                url = hooks[channel]
                break
        conn.close()
        if not url:
            log(f'alert skipped: no {channel} webhook in agent_registry')
            return
        r = requests.post(url, json={'content': msg[:1900]},
                          headers={'User-Agent': 'Mozilla/5.0 (openclaw)'}, timeout=10)
        if r.status_code >= 300:
            log(f'alert post failed: {r.status_code}')
    except Exception as e:
        log(f'alert post error (non-fatal): {e}')


def audit_naked_positions(positions: list[dict], breached: list[dict],
                          dry_run: bool) -> list[str]:
    """End-of-pass truth check: re-fetch broker coverage and list every
    position whose downside is still uncovered. Posts a Discord alert when
    anything is naked or breached — this pass is the last line of defense,
    so its failures must be operator-visible, not just logged."""
    # Bare stops (flat order list shows no OCO stop legs) and OCO/bracket
    # parents reserve DISJOINT shares, so coverage is their SUM — max() here
    # would flag mixed protection (e.g. 28 OCO + 1 bare stop) as naked and
    # train the operator to ignore the alert.
    covered = fetch_active_stops()
    for _sym, _q in fetch_tp_covered(linked_only=True).items():
        covered[_sym] = covered.get(_sym, 0.0) + _q
    naked: list[str] = []
    for pos in positions:
        sym = pos.get('symbol')
        try:
            q = abs(float(pos.get('qty') or 0))
        except (TypeError, ValueError):
            q = 0.0
        if not sym or q <= 0:
            continue
        gap = q - covered.get(sym, 0.0)
        if gap > 0.01:
            naked.append(f'{sym} ({gap:.0f}/{q:.0f} sh uncovered)')
    if (naked or breached) and not dry_run:
        lines = ['⚠️ **stop_reattach: positions without full downside protection**']
        if naked:
            lines.append('NAKED after pass: ' + ', '.join(naked))
        if breached:
            lines.append('BREACHED (operator: close or accept): ' + ', '.join(
                f'{b["ticker"]} {b["side"]} cur={b["current"]:.2f} '
                f'stop={b["breached_stop"]:.2f} uPnL=${b["unrealized_pl"]:.0f}'
                for b in breached))
        _post_alert('\n'.join(lines))
    return naked


def _wait_qty_freed(symbol: str, need: float, timeout: int = 12) -> bool:
    """Poll the position's qty_available until >= need (the broker frees shares
    asynchronously after a stop cancel). Returns True once free, else False on
    timeout."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, payload, _ = _run_cli(['position', 'list'])
        if ok:
            for p in (payload or []):
                if p.get('symbol') == symbol:
                    try:
                        avail = abs(float(p.get('qty_available') or 0))
                    except (TypeError, ValueError):
                        avail = 0.0
                    if avail >= need - 0.01:
                        return True
                    break
        time.sleep(1.0)
    return False


def _position_still_open(symbol: str) -> bool:
    """Fresh broker truth for one symbol. Used before restoring a bare stop
    after an OCO rejection: the canonical rejection cause ("oco orders must
    be exit orders") is the position closing mid-pass — its TP filled between
    fetch_positions() and the submit. Restoring a stop then doesn't protect
    anything; it OPENS a new position when triggered (KLAC 2026-07-21: 4-sh
    buy_to_open stop restored onto a flat symbol). Fails open (True) when the
    broker can't be read — restoring a possibly-needed stop beats leaving a
    possibly-open position naked, and the orphan sweep catches the miss."""
    ok, payload, _ = _run_cli(['position', 'list'])
    if not ok:
        return True
    for p in (payload or []):
        if p.get('symbol') == symbol:
            try:
                return abs(float(p.get('qty') or 0)) > 0
            except (TypeError, ValueError):
                return True
    return False


def sweep_orphan_exits(dry_run: bool, only: str | None = None) -> list[str]:
    """Cancel EXIT orders whose symbol has no open position — a triggered
    orphan doesn't close anything, it opens a fresh unintended position.
    Exit orders are: bare stop/stop_limit/trailing_stop, OCO parents (pure
    exit pairs), and afterhours monitor limits (ahtp_/ahsx_ client ids).
    Plain limit/market entries and bracket/OTO parents (entry + attached
    exits) are legitimately positionless and are never touched; orders
    already pending_cancel are left to die. Both lists are fetched fresh so
    a same-pass race (TP fill after the pass's position snapshot) is caught.
    Returns labels of cancelled orders; posts a data-alerts note when any."""
    ok, positions, _ = _run_cli(['position', 'list'])
    if not ok:
        log('orphan sweep skipped: position list unavailable')
        return []
    pos_syms = set()
    for p in (positions or []):
        try:
            if abs(float(p.get('qty') or 0)) > 0:
                pos_syms.add(p.get('symbol'))
        except (TypeError, ValueError):
            pos_syms.add(p.get('symbol'))
    ok, orders, _ = _run_cli(['order', 'list', '--status', 'open'])
    if not ok:
        log('orphan sweep skipped: order list unavailable')
        return []
    cancelled: list[str] = []
    for o in (orders or []):
        sym = o.get('symbol')
        if not sym or sym in pos_syms or (only and sym != only):
            continue
        if (o.get('status') or '') == 'pending_cancel':
            continue
        otype = o.get('order_type') or o.get('type') or ''
        oclass = (o.get('order_class') or '').lower()
        coid = o.get('client_order_id') or ''
        is_exit = (otype in ('stop', 'stop_limit', 'trailing_stop')
                   or oclass == 'oco'
                   or coid.startswith(('ahtp_', 'ahsx_')))
        if not is_exit:
            continue
        oid = o.get('id')
        label = f'{sym} {oclass or otype} x{o.get("qty")}'
        if dry_run:
            log(f'  DRY-RUN orphan cancel: {label} ({oid})')
            cancelled.append(label)
            continue
        ok2, _, _ = _run_cli(['order', 'cancel', '--order-id', oid])
        if ok2:
            log(f'  🧹 orphan exit cancelled (no position): {label}')
            cancelled.append(label)
        else:
            log(f'  ⚠ {sym}: orphan cancel failed for {oid}')
    log(f'orphan sweep: {len(cancelled)} exit order(s) on flat symbols'
        + (f' — {", ".join(cancelled)}' if cancelled else ''))
    if cancelled and not dry_run:
        _post_alert('🧹 **stop_reattach: cancelled orphan exit order(s) on flat '
                    'symbols** — ' + ', '.join(cancelled))
    return cancelled


def run_oco_reattach(conn, positions: list[dict], dry_run: bool) -> dict:
    """Upgrade positions to a GTC OCO (take-profit + stop) where both legs are
    valid. Idempotent: positions already carrying a resting take-profit are
    skipped. Positions whose target is already reached, or whose stop is
    breached, are left to the bare-stop floor reattach (and operator review)."""
    tp_cov = fetch_tp_covered(linked_only=True)
    stats = {'oco': 0, 'already_tp': 0, 'no_sub': 0, 'degenerate': 0,
             'breached': 0, 'reached': 0, 'rejected': 0, 'restored': 0,
             'tp_missing': 0, 'closed_mid_pass': 0}
    for pos in positions:
        sym = pos.get('symbol')
        try:
            pos_qty = abs(float(pos.get('qty') or 0))
        except (TypeError, ValueError):
            pos_qty = 0.0
        if pos_qty <= 0 or not sym:
            continue
        if tp_cov.get(sym, 0.0) >= pos_qty - 0.01:
            stats['already_tp'] += 1
            continue
        side = (pos.get('side') or '').lower()
        sub = _resolve_intended_bracket(conn, sym, side)
        if not sub:
            stats['no_sub'] += 1
            continue
        new_stop, sstatus = _compute_new_stop(pos, sub)
        new_target, tstatus = _compute_new_target(pos, sub)
        if sstatus == 'breached':
            stats['breached'] += 1
            continue
        if sstatus != 'ok':
            stats['degenerate'] += 1
            continue
        if tstatus != 'ok':
            # Never silently drop the TP: leave the loss side to the bare-stop
            # floor, but surface a missing take-profit for operator review.
            if tstatus == 'reached':
                stats['reached'] += 1
                # A reached target can never rest as an OCO, and a stop-only
                # position gives the ext-hours monitor no TP level to act on
                # — CLYM rode 2.4% past target for 20h that way (2026-07-21).
                # Hand the levels to the monitor's pending-exit journal; its
                # next ext-hours tick captures the profit.
                if not dry_run:
                    try:
                        from execution.afterhours_tp import (_load_intents,
                                                             _save_intents)
                        import time as _t
                        intents = _load_intents()
                        intents[sym] = {'stop': new_stop, 'tp': new_target,
                                        'ts': _t.time()}
                        _save_intents(intents)
                        log(f'  → {sym}: target {new_target} already reached '
                            f'— handed to ext-hours monitor journal')
                    except Exception as e:
                        log(f'  ⚠ {sym}: monitor-journal handoff failed: {e}')
            else:
                stats['degenerate'] += 1
                stats['tp_missing'] += 1
                log(f'  ⚠ {sym}: no valid take-profit from broker history or DB '
                    f'(tstatus={tstatus}) — bare stop only, TP NOT re-established')
            continue
        # Both legs valid → cancel every reserving exit order (bare stops,
        # bare ahtp TPs, partial OCO parents), WAIT for the broker to free
        # the shares (cancel is async), then place the full-size OCO. If the
        # shares never free, restore the stop and skip rather than risk a
        # naked position.
        ncanc = cancel_stops_for(sym, dry_run, include_reserving=True,
                                 exit_side='sell' if side == 'long' else 'buy')
        if not dry_run and ncanc and not _wait_qty_freed(sym, pos_qty):
            if not _position_still_open(sym):
                stats['closed_mid_pass'] += 1
                log(f'  ○ {sym}: position closed mid-pass — no restore')
                continue
            log(f'  ⚠ {sym}: shares not freed after stop cancel — restoring stop, skipping OCO')
            submit_protective_stop(ticker=sym, position_side=side, qty=pos_qty,
                                   stop_price=new_stop, dry_run=False)
            stats['rejected'] += 1
            continue
        r = submit_protective_oco(ticker=sym, position_side=side, qty=pos_qty,
                                  stop_price=new_stop, target_price=new_target,
                                  dry_run=dry_run)
        if r['status'] in ('submitted', 'dry_run'):
            stats['oco'] += 1
        else:
            stats['rejected'] += 1
            if not dry_run:
                if not _position_still_open(sym):
                    stats['closed_mid_pass'] += 1
                    log(f'  ○ {sym}: position closed mid-pass (nothing left to '
                        f'exit) — no restore')
                    continue
                # OCO failed AFTER canceling the bare stop — restore loss-side
                # protection so the position is never left naked.
                log(f'  ↩ {sym}: OCO rejected, restoring bare stop')
                rr = submit_protective_stop(ticker=sym, position_side=side,
                                            qty=pos_qty, stop_price=new_stop,
                                            dry_run=False)
                if rr.get('status') == 'submitted':
                    stats['restored'] += 1
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--oco', action='store_true',
                    help='Upgrade positions to GTC OCO (take-profit + stop) '
                         'before the bare-stop floor pass.')
    ap.add_argument('--only', default=None,
                    help='Restrict to a single ticker (canary/diagnostic).')
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
    if args.only:
        positions = [p for p in positions if p.get('symbol') == args.only]
        log(f'--only {args.only}: {len(positions)} position(s) after filter')
    if not positions:
        # Nothing to protect, but exit orders may survive their positions
        # (orphans open unintended positions when triggered) — sweep anyway.
        sweep_orphan_exits(args.dry_run, only=args.only)
        return 0

    plans = []
    breached: list[dict] = []
    skips: dict[str, int] = {'already_covered': 0, 'no_submission_row': 0,
                              'degenerate_stop': 0, 'unknown_side': 0,
                              'past_stop_breached': 0}
    conn = psycopg2.connect(pg_uri)
    try:
        # OCO upgrade pass (take-profit + stop, REST) before the bare-stop floor.
        # Runs first so its stop legs count toward `covered` below — the floor
        # pass then only stops positions the OCO pass left uncovered.
        if args.oco:
            oco_stats = run_oco_reattach(conn, positions, args.dry_run)
            log(f'OCO pass: {oco_stats}')
            if oco_stats.get('tp_missing'):
                log(f'⚠ {oco_stats["tp_missing"]} position(s) re-stopped WITHOUT a '
                    f'take-profit (no recoverable TP) — operator review needed')

        # Bare-stop floor: ensure every still-uncovered position has a GTC stop.
        # An OCO surfaces as a limit order (its stop leg is linked, not a
        # top-level stop), so fold its LINKED take-profit coverage in — an OCO'd
        # position is already protected on both sides and must be skipped here.
        # linked_only=True is load-bearing: a bare order_class='simple' TP
        # (afterhours_tp's `ahtp_*`) gives NO downside protection and must NOT
        # count as covered, or the position is left naked when that TP expires.
        covered = fetch_active_stops()
        for _sym_tp, _q_tp in fetch_tp_covered(linked_only=True).items():
            covered[_sym_tp] = max(covered.get(_sym_tp, 0.0), _q_tp)
        log(f'active stops/oco cover {len(covered)} symbols (total {sum(covered.values()):.0f} shares)')
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
        naked = audit_naked_positions(positions, breached, args.dry_run)
        if naked:
            log(f'⚠ NAKED after pass: {", ".join(naked)}')
        # Inverse of the naked audit: exit orders without positions. Runs
        # LAST so a position that closed during this very pass (TP fill
        # racing the OCO upgrade — KLAC 2026-07-21) is caught same-pass.
        sweep_orphan_exits(args.dry_run, only=args.only)
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
