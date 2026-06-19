#!/usr/bin/env python3
"""
alpaca_reconcile.py — pipeline step that reconciles `alpaca_submissions`
against actual broker FILL activities.

Runs as the `reconcile` orchestrator step, immediately after `alpaca` and
before `report`. Closes the attribution hole where the engine's
"would-have-hit-target" arithmetic on parquet prices was credited even when
the broker rejected the order or partially filled it.

For each FILL activity returned by `alpaca account activity list
--activity-types FILL --date $TODAY`, find the matching alpaca_submissions
row by alpaca_order_id and update its broker_status / filled_qty /
filled_avg_price / reconciled_at. Submissions that exist but have no
matching FILL get `broker_status='rejected_by_broker'`.

Partial fills appear as multiple FILL activities per order_id with the
same `cum_qty` aggregating up; we collapse them to a single
broker-canonical record per order using the highest `cum_qty` row.

Usage:
    python3 src/execution/alpaca_reconcile.py [--date YYYY-MM-DD]

Exit codes:
    0 — success, or no submissions to reconcile
    1 — POSTGRES_URI missing, or unrecoverable CLI error
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')


def log(msg: str) -> None:
    ts = datetime.now().strftime('%H:%M:%S')
    print(f'{ts} [RECONCILE] {msg}')


def fetch_fills_for_date(run_date: str, *, page_size: int = 100, max_pages: int = 50):
    """Return the list of FILL activity dicts for `run_date`.

    Pages through `alpaca account activity list` until the broker returns
    fewer rows than the page size (or `max_pages` is hit, as a safety
    sentinel). The CLI caps page-size at 100, so a 200-fill day takes 2
    page calls. Activities span 'fill' and 'partial_fill' types — both
    are kept; collapsing into per-order summaries happens downstream.
    """
    fills = []
    page_token = None
    for _ in range(max_pages):
        args = [ALPACA_CLI, 'account', 'activity', 'list',
                '--activity-types', 'FILL',
                '--date', run_date,
                '--page-size', str(page_size)]
        if page_token:
            args += ['--page-token', page_token]
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            log(f'CLI rc={proc.returncode} stderr={proc.stderr[:300]}')
            raise RuntimeError(f'alpaca activity list failed: {proc.stderr[:200]}')
        if not proc.stdout.strip():
            break
        try:
            page = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f'CLI returned non-JSON stdout: {exc}; head={proc.stdout[:200]}')
        if not isinstance(page, list) or not page:
            break
        fills.extend(page)
        if len(page) < page_size:
            break
        # Pagination: pass the last activity's id as page-token next iter.
        page_token = page[-1].get('id')
        if not page_token:
            break
    return fills


def collapse_fills(fills):
    """Reduce a list of FILL activities to a per-order_id summary.

    For each order_id, take the max cum_qty seen, and the qty-weighted
    average price across all fills. Returns a dict keyed by order_id with
    {qty, avg_price, status} where status is 'filled' if the latest fill
    has order_status='filled', else 'partial'.
    """
    by_oid = {}
    for f in fills:
        oid = f.get('order_id')
        if not oid:
            continue
        try:
            qty   = float(f.get('qty')   or 0)
            price = float(f.get('price') or 0)
        except (TypeError, ValueError):
            continue
        rec = by_oid.setdefault(oid, {
            'cum_qty':       0.0,
            'notional':      0.0,
            'order_status':  f.get('order_status', ''),
            'last_seen':     f.get('transaction_time', ''),
        })
        rec['cum_qty']       += qty
        rec['notional']      += qty * price
        # Track the most recent transaction_time so the final status reflects
        # the broker's last word on this order, not just an early partial.
        ts = f.get('transaction_time', '')
        if ts >= rec['last_seen']:
            rec['order_status'] = f.get('order_status', rec['order_status'])
            rec['last_seen']    = ts

    out = {}
    for oid, rec in by_oid.items():
        cq = rec['cum_qty']
        avg_price = (rec['notional'] / cq) if cq > 0 else 0.0
        status = 'filled' if rec['order_status'] == 'filled' else 'partial'
        out[oid] = {
            'qty':       cq,
            'avg_price': avg_price,
            'status':    status,
        }
    return out


def fetch_order_status(order_id: str) -> dict | None:
    """Fetch a single order's definitive fill status directly from the broker.

    Used as a fallback when fill activities haven't propagated yet (activity
    API can lag 1-30s after a market-order fill) or when an activity record
    shows 'partial' but the order may have since completed.

    Returns {qty, avg_price, status} where status ∈ {'filled', 'partial', 'rejected'}.
    Returns None for orders still in-flight (new/accepted/held/pending_new) or
    when the CLI call fails — callers leave those rows untouched.
    """
    # The CLI's `order get` requires the id via `--order-id`; a bare
    # positional arg returns an error JSON with rc=0 and no `status` key,
    # which makes this function silently return None for every order. (That
    # latent bug is why the poll-to-terminal pass never upgraded anything and
    # ext-hours fills accreted at broker_status=NULL — see --sweep-stale.)
    proc = subprocess.run([ALPACA_CLI, 'order', 'get', '--order-id', order_id],
                          capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        return None
    try:
        o = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    # Defensive: `order get <id>` returns a single JSON object in production,
    # but a mis-routed CLI call (or a test mock reusing the activities-list
    # fixture) can return a list or other non-dict shape. Treat anything
    # we can't interpret as "still in-flight" → caller leaves the row alone
    # rather than aggressively marking rejected on ambiguous evidence.
    if not isinstance(o, dict):
        return None
    status = o.get('status', '')
    filled_qty = float(o.get('filled_qty') or 0)
    avg_price = float(o.get('filled_avg_price') or 0)
    if status == 'filled':
        return {'qty': filled_qty, 'avg_price': avg_price, 'status': 'filled'}
    if status == 'partially_filled':
        return {'qty': filled_qty, 'avg_price': avg_price, 'status': 'partial'}
    if status in ('canceled', 'rejected', 'expired'):
        return {'qty': 0.0, 'avg_price': 0.0, 'status': 'rejected'}
    return None  # new / accepted / held / pending_new — still in flight


def fetch_broker_tickers() -> set[str] | None:
    """Return the set of tickers the broker currently holds, or None
    if the CLI call fails (caller should skip phantom cleanup on None).
    """
    proc = subprocess.run([ALPACA_CLI, 'position', 'list'],
                          capture_output=True, text=True, timeout=30, check=False)
    if proc.returncode != 0:
        log(f'position list rc={proc.returncode}; skipping phantom cleanup')
        return None
    try:
        positions = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log('position list returned non-JSON; skipping phantom cleanup')
        return None
    if not isinstance(positions, list):
        return None
    return {p['symbol'] for p in positions if p.get('symbol')}


def cleanup_phantom_signals(conn, dry_run: bool, broker_tickers: set[str]) -> int:
    """Mark execution_signals.status='closed' for open rows whose ticker
    is no longer held by the broker.

    Spares today's signals (signal_date == CURRENT_DATE) — they may be
    fresh orders that haven't filled yet. Older rows where the broker
    isn't holding the ticker are unambiguous phantoms (closed externally,
    rejected, or expired before fill); they pollute the dashboard
    portfolio rollup and the sizer's active-window query if left open.

    Append-only invariant preserved: status flip only, no DELETE.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ticker, strategy_id, signal_date
          FROM execution_signals
         WHERE status='open'
           AND signal_date < CURRENT_DATE
           AND NOT (ticker = ANY(%s))
        """,
        (list(broker_tickers),))
    rows = cur.fetchall()
    if not rows:
        log('phantom cleanup: nothing to do (open rows match broker holdings)')
        cur.close()
        return 0
    if dry_run:
        log(f'phantom cleanup: would close {len(rows)} stale open signals (DRY-RUN)')
        for r in rows[:5]:
            log(f'  DRY: sig={r[0]} {r[2]}/{r[1]} signal_date={r[3]}')
        cur.close()
        return len(rows)
    # Per-row savepoint loop. The execution_signals table has pre-existing
    # legacy duplicate (sid, signal_date, ticker, direction) tuples that
    # predate the unique constraint; a bulk UPDATE on those rows trips
    # UniqueViolation. Per-row + savepoint lets the loop skip past those
    # rows without losing the closes on healthy rows.
    n_closed, n_skipped = 0, 0
    for r in rows:
        sp = f'sp_phantom_{r[0].hex if hasattr(r[0],"hex") else "x"}'.replace('-', '_')
        try:
            cur.execute(f'SAVEPOINT {sp}')
            cur.execute("UPDATE execution_signals SET status='closed' WHERE id=%s::uuid", (str(r[0]),))
            cur.execute(f'RELEASE SAVEPOINT {sp}')
            n_closed += 1
        except psycopg2.errors.UniqueViolation:
            cur.execute(f'ROLLBACK TO SAVEPOINT {sp}')
            n_skipped += 1
    conn.commit()
    cur.close()
    log(f'phantom cleanup: closed {n_closed} stale open signals '
        f'({n_skipped} skipped due to legacy dup-key))')
    return n_closed


def _apply_fill(cur, sub_id, ticker, rec, *, dry_run: bool) -> None:
    """Write a terminal fill/partial record onto an alpaca_submissions row.

    Shared by reconcile()'s first/poll passes and the stale-sweep so the
    UPDATE SQL lives in exactly one place. Caller owns the commit.
    """
    if dry_run:
        log(f'  DRY: would mark sub={sub_id} ({ticker}) → {rec["status"]} '
            f'qty={rec["qty"]} avg=${rec["avg_price"]:.2f}')
        return
    cur.execute("""
        UPDATE alpaca_submissions
        SET broker_status=%s,
            filled_qty=%s,
            filled_avg_price=%s,
            reconciled_at=NOW()
        WHERE id=%s
    """, (rec['status'], rec['qty'], rec['avg_price'], sub_id))


def _mark_rejected(cur, sub_id, ticker, rec, *, dry_run: bool) -> None:
    """Mark an alpaca_submissions row rejected_by_broker.

    Shared by reconcile()'s first/poll passes and the stale-sweep. Caller
    owns the commit.
    """
    if dry_run:
        log(f'  DRY: would mark sub={sub_id} ({ticker}) → rejected_by_broker')
        return
    cur.execute("""
        UPDATE alpaca_submissions
        SET broker_status='rejected_by_broker',
            reconciled_at=NOW()
        WHERE id=%s
    """, (sub_id,))


def reconcile(run_date: str, conn, dry_run: bool = False,
              poll_timeout_s: int = 30, poll_interval_s: int = 3):
    """Update alpaca_submissions rows for `run_date` with broker fill state.

    With dry_run=True: prints the would-be UPDATE statements and exits
    cleanly without touching the DB. Useful for development iteration
    without polluting alpaca_submissions reconciled_at timestamps.

    `poll_timeout_s` controls how long to wait for orders still showing
    as in-flight after the first pass. Ext-hours fills routinely lag the
    activities API by 5-30 seconds; without polling, those rows would
    stay broker_status=NULL until the NEXT reconcile run. Set to 0 to
    disable polling entirely (legacy behavior)."""
    cur = conn.cursor()

    cur.execute("""
        SELECT id, alpaca_order_id, ticker, qty
        FROM alpaca_submissions
        WHERE run_date = %s AND alpaca_order_id IS NOT NULL
    """, (run_date,))
    submissions = cur.fetchall()
    if not submissions:
        log(f'No submissions for {run_date} to reconcile — exiting clean')
        cur.close()
        return 0

    fills = fetch_fills_for_date(run_date)
    log(f'Pulled {len(fills)} FILL activities for {run_date}')
    by_oid = collapse_fills(fills)

    n_filled = 0
    n_partial = 0
    n_rejected = 0
    # Orders still in-flight after the first pass — we'll poll these
    # before declaring them un-reconciled and leaving the row as submitted.
    in_flight: list[tuple] = []

    def _apply(sub_id_, ticker_, rec_):
        nonlocal n_filled, n_partial
        _apply_fill(cur, sub_id_, ticker_, rec_, dry_run=dry_run)
        if rec_['status'] == 'filled':
            n_filled += 1
        else:
            n_partial += 1

    for sub_id, alpaca_order_id, ticker, sub_qty in submissions:
        rec = by_oid.get(alpaca_order_id)

        if rec is None:
            # No fill activity found yet — the activity API can lag 1-30s after a
            # market fill. Fetch the order directly to distinguish "truly rejected"
            # from "fill not propagated yet."
            order_rec = fetch_order_status(alpaca_order_id)
            if order_rec is None:
                # Still in-flight; defer to the poll pass below.
                in_flight.append((sub_id, alpaca_order_id, ticker))
                continue
            if order_rec['status'] == 'rejected':
                _mark_rejected(cur, sub_id, ticker, order_rec, dry_run=dry_run)
                n_rejected += 1
                continue
            rec = order_rec

        elif rec['status'] == 'partial':
            # Activity shows partial — confirm whether order has since fully filled
            # (common when reconcile runs before all fill chunks propagate).
            order_rec = fetch_order_status(alpaca_order_id)
            if order_rec and order_rec['status'] == 'filled':
                rec = order_rec  # upgrade to filled; don't downgrade

        _apply(sub_id, ticker, rec)

    # ── Poll-to-terminal pass for in-flight orders ──────────────────────────
    if in_flight and poll_timeout_s > 0:
        log(f'  polling {len(in_flight)} in-flight order(s) for up to {poll_timeout_s}s '
            f'(interval={poll_interval_s}s) — ext-hours fills can lag activity API')
        deadline = time.time() + poll_timeout_s
        remaining = list(in_flight)
        while remaining and time.time() < deadline:
            time.sleep(poll_interval_s)
            still_pending = []
            for sub_id, oid, ticker in remaining:
                order_rec = fetch_order_status(oid)
                if order_rec is None:
                    still_pending.append((sub_id, oid, ticker))
                    continue
                if order_rec['status'] == 'rejected':
                    _mark_rejected(cur, sub_id, ticker, order_rec, dry_run=dry_run)
                    n_rejected += 1
                else:
                    log(f'  {ticker}: order terminal after poll → {order_rec["status"]}')
                    _apply(sub_id, ticker, order_rec)
            remaining = still_pending
        for sub_id, oid, ticker in remaining:
            log(f'  {ticker}: still in-flight after {poll_timeout_s}s poll — leaving as submitted')
    elif in_flight:
        for sub_id, oid, ticker in in_flight:
            log(f'  {ticker}: no fill activity, order in-flight — leaving as submitted')

    if not dry_run:
        conn.commit()
    cur.close()
    prefix = 'DRY-RUN' if dry_run else 'Reconciled'
    log(f'{prefix} {len(submissions)}: filled={n_filled} partial={n_partial} rejected={n_rejected}')
    return len(submissions)


def sweep_stale(conn, days: int = 5, dry_run: bool = False) -> int:
    """Re-reconcile alpaca_submissions rows left broker_status=NULL on prior runs.

    The normal reconcile() only scans the current cycle's run_date and polls
    in-flight orders for ~30s. Ext-hours fills lag Alpaca's activity API by
    more than that, so those rows are stranded at broker_status=NULL and are
    never re-examined on a later day even though the broker eventually filled
    them (2026-06-17 17/19, 2026-06-18 15/17 stuck silently).

    This sweep selects the last `days` of NULL-status submitted orders and
    re-queries each order's definitive status directly via
    fetch_order_status() — the same helper reconcile()'s fallback/poll passes
    use. We deliberately do NOT reuse the date-windowed fetch_fills_for_date()
    nor the poll-to-terminal loop: both exist to absorb propagation lag on
    FRESH orders, whereas these rows are days old and the broker has long
    since reached a terminal state, so one order-get per row is the right,
    cheaper probe. Updates reuse the shared _apply_fill / _mark_rejected
    helpers so the write logic is not duplicated.

    Idempotent: the SELECT is scoped to `broker_status IS NULL`, so once a row
    is reconciled it falls out of the selection and a re-run is a no-op. Rows
    that are still genuinely in-flight (fetch_order_status → None) are left
    untouched — a later sweep re-examines them.

    Returns the number of rows updated (or that WOULD be updated in dry-run).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT id, alpaca_order_id, ticker, qty, run_date
          FROM alpaca_submissions
         WHERE broker_status IS NULL
           AND alpaca_order_id IS NOT NULL
           AND run_date >= CURRENT_DATE - (%s || ' days')::interval
         ORDER BY run_date ASC
    """, (days,))
    rows = cur.fetchall()
    if not rows:
        log(f'sweep-stale: no NULL-status submitted orders in last {days}d — nothing to do')
        cur.close()
        return 0

    log(f'sweep-stale: {len(rows)} NULL-status submitted order(s) in last {days}d'
        f'{" (DRY-RUN)" if dry_run else ""}')
    n_filled = n_partial = n_rejected = n_inflight = 0
    for sub_id, alpaca_order_id, ticker, _qty, run_date in rows:
        order_rec = fetch_order_status(alpaca_order_id)
        if order_rec is None:
            # Still in-flight (or transient CLI failure) — leave it for a later sweep.
            log(f'  {ticker} (run_date={run_date}): order not terminal/unfetchable — leaving NULL')
            n_inflight += 1
            continue
        if order_rec['status'] == 'rejected':
            _mark_rejected(cur, sub_id, ticker, order_rec, dry_run=dry_run)
            n_rejected += 1
        else:
            _apply_fill(cur, sub_id, ticker, order_rec, dry_run=dry_run)
            if order_rec['status'] == 'filled':
                n_filled += 1
            else:
                n_partial += 1

    if not dry_run:
        conn.commit()
    cur.close()
    n_updated = n_filled + n_partial + n_rejected
    prefix = 'sweep-stale DRY-RUN would update' if dry_run else 'sweep-stale updated'
    log(f'{prefix} {n_updated}/{len(rows)}: filled={n_filled} partial={n_partial} '
        f'rejected={n_rejected} still-in-flight={n_inflight}')
    return n_updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    ap.add_argument('--dry-run', action='store_true',
                    help='Show what UPDATE statements would run without executing them.')
    ap.add_argument('--poll-timeout-s', type=int, default=30,
                    help='Seconds to poll in-flight orders before declaring them un-reconciled. '
                         'Set to 0 to disable polling (legacy behavior).')
    ap.add_argument('--poll-interval-s', type=int, default=3,
                    help='Seconds between poll attempts for in-flight orders.')
    ap.add_argument('--sweep-stale', action='store_true',
                    help='Additive backfill mode: re-reconcile alpaca_submissions rows '
                         'left broker_status=NULL on PRIOR runs (ext-hours fills that '
                         'lagged the activity API past the poll window). Does NOT run '
                         'the normal current-date reconcile or phantom cleanup.')
    ap.add_argument('--days', type=int, default=5,
                    help='With --sweep-stale: how many days back to scan for stale '
                         'NULL-status submissions (default 5).')
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        log('POSTGRES_URI not set — aborting')
        sys.exit(2)   # auth/config error per Tier 3 exit-code discipline

    # ── Stale re-sweep mode (additive; does not run the normal reconcile) ──
    if args.sweep_stale:
        log(f'Sweeping stale NULL-status submissions (last {args.days}d)'
            f'{" (DRY-RUN)" if args.dry_run else ""}')
        conn = psycopg2.connect(uri)
        try:
            sweep_stale(conn, days=args.days, dry_run=args.dry_run)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return

    log(f'Reconciling {args.date}{" (DRY-RUN)" if args.dry_run else ""}')
    conn = psycopg2.connect(uri)
    try:
        reconcile(args.date, conn, dry_run=args.dry_run,
                  poll_timeout_s=args.poll_timeout_s,
                  poll_interval_s=args.poll_interval_s)
        # Phantom cleanup: stale 'open' execution_signals whose tickers the
        # broker no longer holds. Fail-open on broker fetch error (the
        # reconcile work above is the critical path).
        broker_tickers = fetch_broker_tickers()
        if broker_tickers is not None:
            cleanup_phantom_signals(conn, args.dry_run, broker_tickers)
    except RuntimeError as exc:
        log(f'aborted: {exc}')
        conn.close()
        # CLI auth failures (alpaca activity list returning 401) → exit 2
        if 'authentication' in str(exc).lower() or 'unauthor' in str(exc).lower():
            sys.exit(2)
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
