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


def reconcile(run_date: str, conn, dry_run: bool = False):
    """Update alpaca_submissions rows for `run_date` with broker fill state.

    With dry_run=True: prints the would-be UPDATE statements and exits
    cleanly without touching the DB. Useful for development iteration
    without polluting alpaca_submissions reconciled_at timestamps."""
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
    for sub_id, alpaca_order_id, ticker, sub_qty in submissions:
        rec = by_oid.get(alpaca_order_id)
        if rec is None:
            if dry_run:
                log(f'  DRY: would mark sub={sub_id} ({ticker}) → rejected_by_broker')
            else:
                cur.execute("""
                    UPDATE alpaca_submissions
                    SET broker_status='rejected_by_broker',
                        reconciled_at=NOW()
                    WHERE id=%s
                """, (sub_id,))
            n_rejected += 1
            continue
        if dry_run:
            log(f'  DRY: would mark sub={sub_id} ({ticker}) → {rec["status"]} '
                f'qty={rec["qty"]} avg=${rec["avg_price"]:.2f}')
        else:
            cur.execute("""
                UPDATE alpaca_submissions
                SET broker_status=%s,
                    filled_qty=%s,
                    filled_avg_price=%s,
                    reconciled_at=NOW()
                WHERE id=%s
            """, (rec['status'], rec['qty'], rec['avg_price'], sub_id))
        if rec['status'] == 'filled':
            n_filled += 1
        else:
            n_partial += 1

    if not dry_run:
        conn.commit()
    cur.close()
    prefix = 'DRY-RUN' if dry_run else 'Reconciled'
    log(f'{prefix} {len(submissions)}: filled={n_filled} partial={n_partial} rejected={n_rejected}')
    return len(submissions)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=str(date.today()))
    ap.add_argument('--dry-run', action='store_true',
                    help='Show what UPDATE statements would run without executing them.')
    args = ap.parse_args()

    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        log('POSTGRES_URI not set — aborting')
        sys.exit(2)   # auth/config error per Tier 3 exit-code discipline

    log(f'Reconciling {args.date}{" (DRY-RUN)" if args.dry_run else ""}')
    conn = psycopg2.connect(uri)
    try:
        reconcile(args.date, conn, dry_run=args.dry_run)
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
