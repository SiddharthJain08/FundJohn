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


def _parse_ts(value) -> str | None:
    """Best-effort ISO-8601 validation ahead of a `::timestamptz` cast.

    Broker activity/order timestamps arrive as spec-compliant ISO-8601
    strings (`2026-09-11T13:32:00Z`) in the happy path, but a malformed or
    missing value must degrade to NULL here rather than reach Postgres and
    abort the transaction on an unparseable-timestamp error. Returns the
    ORIGINAL string unchanged when it parses — the `%s::timestamptz` casts
    want the string form, not a Python datetime — else None. Used before
    both timestamp casts in this module (the broker_fills INSERT and the
    separate filled_at UPDATE in _apply_fill)."""
    if not value or not isinstance(value, str):
        return None
    try:
        # datetime.fromisoformat doesn't accept a trailing 'Z' before 3.11;
        # normalize defensively for whatever runtime this executes under.
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return value


def _fetch_fill_pages(window_args, *, page_size: int = 100, max_pages: int = 50):
    """Shared FILL-activity pager: `window_args` is the CLI window selector
    (`['--date', d]` or `['--after', ts, '--direction', 'asc']`). Returns the
    list of activity dicts; raises RuntimeError on a CLI/parse failure."""
    fills = []
    page_token = None
    for _ in range(max_pages):
        args = [ALPACA_CLI, 'account', 'activity', 'list',
                '--activity-types', 'FILL',
                *window_args,
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


def fetch_fills_for_date(run_date: str, *, page_size: int = 100, max_pages: int = 50):
    """Return the list of FILL activity dicts for `run_date`.

    Pages through `alpaca account activity list` until the broker returns
    fewer rows than the page size (or `max_pages` is hit, as a safety
    sentinel). The CLI caps page-size at 100, so a 200-fill day takes 2
    page calls. Activities span 'fill' and 'partial_fill' types — both
    are kept; collapsing into per-order summaries happens downstream.
    """
    return _fetch_fill_pages(['--date', run_date], page_size=page_size,
                             max_pages=max_pages)


def fetch_fills_since(after: str, *, page_size: int = 100, max_pages: int = 200):
    """FILL activities created after `after` (YYYY-MM-DDTHH:MM:SSZ), oldest
    first, fully paginated. Used by the account breaker's alpha-P&L ledger
    (C1 amendment 2a) so its realized leg does not depend on the reconcile
    step having run for the day. Same client/parsing as fetch_fills_for_date."""
    return _fetch_fill_pages(['--after', after, '--direction', 'asc'],
                             page_size=page_size, max_pages=max_pages)


def collapse_fills(fills):
    """Reduce a list of FILL activities to a per-order_id summary.

    For each order_id, take the max cum_qty seen, and the qty-weighted
    average price across all fills. Returns a dict keyed by order_id with
    {qty, avg_price, status, filled_at} where status is 'filled' if the
    latest fill has order_status='filled', else 'partial', and filled_at is
    the latest transaction_time seen for that order (or None).
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
            # B2: the broker's own fill timestamp — latency vs submitted_at, and
            # the ordering key for the broker_fills ledger.
            'filled_at': rec['last_seen'] or None,
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
        return {'qty': filled_qty, 'avg_price': avg_price, 'status': 'filled',
                'filled_at': o.get('filled_at')}
    if status == 'partially_filled':
        return {'qty': filled_qty, 'avg_price': avg_price, 'status': 'partial',
                'filled_at': o.get('filled_at')}
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


# B2: set True after the first UndefinedColumn from the filled_at UPDATE
# below, so a run with many un-reconciled rows logs the warning once, not
# once per row. Reset at the top of reconcile() / sweep_stale() so each
# script invocation gets its own "once".
_FILLED_AT_COLUMN_MISSING_LOGGED = False


def _reset_filled_at_missing_log() -> None:
    global _FILLED_AT_COLUMN_MISSING_LOGGED
    _FILLED_AT_COLUMN_MISSING_LOGGED = False


def _apply_fill(cur, sub_id, ticker, rec, *, dry_run: bool) -> None:
    """Write a terminal fill/partial record onto an alpaca_submissions row.

    Shared by reconcile()'s first/poll passes and the stale-sweep so the
    UPDATE SQL lives in exactly one place. Caller owns the commit.

    This UPDATE is BYTE-IDENTICAL to the pre-B2 statement: broker_status /
    filled_qty / filled_avg_price / reconciled_at are the critical path and
    must never depend on a column that might not exist yet.
    alpaca_submissions.filled_at (migration 155) is written by a SEPARATE,
    savepoint-isolated UPDATE right after, because this code can land on
    main and run before the johnbot restart that applies that migration —
    a plain UndefinedColumn on a combined UPDATE would abort the whole
    transaction, including the write above.
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

    ts = _parse_ts(rec.get('filled_at'))
    if ts is None:
        return
    global _FILLED_AT_COLUMN_MISSING_LOGGED
    cur.execute('SAVEPOINT sp_filled_at')
    try:
        cur.execute("""
            UPDATE alpaca_submissions
            SET filled_at=COALESCE(filled_at,%s::timestamptz)
            WHERE id=%s
        """, (ts, sub_id))
        cur.execute('RELEASE SAVEPOINT sp_filled_at')
    except psycopg2.errors.UndefinedColumn:
        cur.execute('ROLLBACK TO SAVEPOINT sp_filled_at')
        cur.execute('RELEASE SAVEPOINT sp_filled_at')
        if not _FILLED_AT_COLUMN_MISSING_LOGGED:
            log('alpaca_submissions.filled_at column missing (migration 155 not '
                'applied yet) — skipping filled_at backfill this run')
            _FILLED_AT_COLUMN_MISSING_LOGGED = True


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


# ── B2: broker_fills fact table (spec item 14) ──────────────────────────────
# Single source of truth for column order; ingested_at is a DB default and is
# NOT here. `ticker` stores the broker's OWN symbol form (e.g. `BTC/USD` for
# crypto, not a normalized `BTCUSD`) by design — broker_fills is a fact table
# over what the broker actually said, not a normalized join key.
_BROKER_FILL_COLUMNS = (
    'activity_id', 'order_id', 'parent_order_id', 'client_order_id',
    'ticker', 'side', 'order_type', 'order_class', 'qty', 'price', 'filled_at',
)

# Built FROM _BROKER_FILL_COLUMNS so the two can't desynchronize on a reorder;
# only filled_at needs the ::timestamptz cast (via _parse_ts before it lands
# in the params tuple).
_BROKER_FILL_INSERT = (
    'INSERT INTO broker_fills (' + ', '.join(_BROKER_FILL_COLUMNS) + ') '
    'VALUES (' + ', '.join(
        '%s::timestamptz' if col == 'filled_at' else '%s'
        for col in _BROKER_FILL_COLUMNS
    ) + ') '
    'ON CONFLICT (activity_id) DO NOTHING'
)


def build_order_meta(orders) -> dict:
    """{order_id: {parent_order_id, client_order_id, order_type, order_class}}
    from a --nested order list.

    Alpaca FILL ACTIVITY records carry none of these four fields, and the REST
    order model has no parent pointer at all — a leg's parent is only knowable
    by walking `legs` (the same walk classify_exit_fills and
    alpaca_replace_stop.find_stop_loss_leg do). A leg inherits its enclosing
    order's class when it declares none; a top-level order's parent is None."""
    meta: dict = {}
    for top in (orders or []):
        stack = [(top, None)]
        while stack:
            o, parent = stack.pop()
            if not isinstance(o, dict):
                continue
            for leg in (o.get('legs') or []):
                stack.append((leg, o))
            oid = o.get('id') or o.get('order_id')
            if not oid:
                continue
            meta[oid] = {
                'parent_order_id': (parent or {}).get('id'),
                'client_order_id': o.get('client_order_id'),
                'order_type': (o.get('type') or o.get('order_type') or None),
                'order_class': (o.get('order_class')
                                or (parent or {}).get('order_class') or None),
            }
    return meta


def ingest_broker_fills(cur, fills, order_meta=None, *, dry_run: bool = False) -> int:
    """Append every FILL activity to broker_fills (migration 155).

    Keyed by the broker's own activity id with ON CONFLICT DO NOTHING, so
    re-running the reconcile step never duplicates and never rewrites a row
    (append-only invariant). Returns rows offered. NOTE: --sweep-stale does
    NOT call this — it re-derives terminal status per order via
    fetch_order_status, not the FILL activity feed, so stale rows swept on a
    later day are not (and cannot be) added to broker_fills retroactively.

    The counted NULL-parent log line is deliberate: the exit-leg slippage join
    in backfill_exit_slippage keys on parent_order_id, so a closed-order window
    that stopped covering our orders would otherwise show up only as a silent
    `exit n=0` in the daily digest."""
    order_meta = order_meta or {}
    n = 0
    n_no_parent = 0
    for f in (fills or []):
        aid = f.get('id')
        if not aid:
            continue
        try:
            qty = float(f.get('qty') or 0)
            price = float(f.get('price') or 0)
        except (TypeError, ValueError):
            continue
        oid = f.get('order_id')
        m = order_meta.get(oid) or {}
        if not m.get('parent_order_id'):
            n_no_parent += 1
        n += 1
        if dry_run:
            continue
        cur.execute(_BROKER_FILL_INSERT, (
            aid, oid, m.get('parent_order_id'), m.get('client_order_id'),
            f.get('symbol'), (f.get('side') or '').lower() or None,
            m.get('order_type'), m.get('order_class'),
            qty, price, _parse_ts(f.get('transaction_time')),
        ))
    log(f'broker_fills: {n} fill activity row(s) offered '
        f'({n_no_parent} without a parent_order_id)'
        f'{" (DRY-RUN)" if dry_run else ""}')
    return n


# ── B2: exit-leg slippage join (spec item 14) ────────────────────────────────
# Two candidate branches, UNION ALL:
#   1. bracket exit legs — bf.parent_order_id points at the entry submission's
#      alpaca_order_id.
#   2. after-hours emulated-stop/target exits — TOP-LEVEL orders
#      (afterhours_tp.py submits them order_class='simple'), so
#      parent_order_id is NULL; matched instead by ticker + opposite side to
#      the latest submission in the lookback window.
# Both branches:
#   - require bf.ticker = the submission's own ticker (a bracket's OCC-symbol
#     option leg, e.g. an `mleg` structure, has bf.ticker != the underlying
#     ticker on alpaca_submissions and must never be scored as an equity
#     exit against an equity level);
#   - require bf.side be the OPPOSITE of the signal's direction (long exits
#     sell, short exits buy) — a same-side add-on fill on the same bracket
#     is not an exit and must never be scored as one. Both branches use the
#     SAME normalized UPPER(direction) IN ('LONG','BUY','BUY_VOL') form (fix
#     round 2, item 3) — branch 2 originally hardcoded a raw `s2.direction =
#     'long'`/`'short'` literal comparison that diverged from branch 1's
#     normalized form and would silently miss any non-canonical direction
#     spelling;
#   - are per-SIGNAL idempotent via NOT EXISTS, not just "the latest
#     signal_pnl row is NULL": a fill that already stamped an OLDER pnl row
#     for this signal must not be re-applied to a newer row upserted on a
#     later run_date (that would corrupt any mean/count over signal_pnl).
#     The pnl_date written is still always the signal's LATEST row.
# Branch 2 pre-filters broker_fills (parent_order_id IS NULL, ah prefix,
# filled_at bound) in a subquery BEFORE the LATERAL, not in the outer WHERE
# after it — broker_fills is append-only and grows forever (CLAUDE.md), and
# broker_fills_parent_idx is a partial index that only covers
# parent_order_id IS NOT NULL, so an unbounded LATERAL here would drive a
# submission-scan per historical fill instead of per fill in the lookback
# window. filled_at is bounded before the join in both branches.
_EXIT_CANDIDATE_SQL = """
    SELECT bf.activity_id, bf.order_type, bf.client_order_id, bf.price,
           es.id, es.direction, es.stop_loss, es.target_1, sp.pnl_date
      FROM broker_fills bf
      JOIN alpaca_submissions s ON s.alpaca_order_id = bf.parent_order_id
      JOIN execution_signals es ON es.target_date = s.run_date
                               AND es.ticker = s.ticker
                               AND es.strategy_id = s.strategy_id
      JOIN LATERAL (
            SELECT pnl_date
              FROM signal_pnl
             WHERE signal_id = es.id
             ORDER BY pnl_date DESC
             LIMIT 1
           ) sp ON TRUE
     WHERE bf.parent_order_id IS NOT NULL
       AND bf.ticker = s.ticker
       AND bf.filled_at >= %s::date - %s
       AND ((UPPER(es.direction) IN ('LONG','BUY','BUY_VOL') AND bf.side = 'sell')
            OR (UPPER(es.direction) NOT IN ('LONG','BUY','BUY_VOL') AND bf.side = 'buy'))
       AND NOT EXISTS (
             SELECT 1 FROM signal_pnl p
              WHERE p.signal_id = es.id AND p.exit_slippage_bps IS NOT NULL
           )

    UNION ALL

    SELECT bf.activity_id, bf.order_type, bf.client_order_id, bf.price,
           es.id, es.direction, es.stop_loss, es.target_1, sp.pnl_date
      FROM (
            SELECT activity_id, order_type, client_order_id, price,
                   ticker, side, filled_at
              FROM broker_fills
             WHERE parent_order_id IS NULL
               AND (LEFT(COALESCE(client_order_id, ''), 5) = 'ahsx_'
                    OR LEFT(COALESCE(client_order_id, ''), 5) = 'ahtp_')
               AND filled_at >= %s::date - %s
           ) bf
      JOIN LATERAL (
            SELECT s2.run_date, s2.ticker, s2.strategy_id
              FROM alpaca_submissions s2
             WHERE s2.ticker = bf.ticker
               AND s2.run_date >= %s::date - %s
               AND ((UPPER(s2.direction) IN ('LONG','BUY','BUY_VOL') AND bf.side = 'sell')
                    OR (UPPER(s2.direction) NOT IN ('LONG','BUY','BUY_VOL') AND bf.side = 'buy'))
             ORDER BY s2.run_date DESC
             LIMIT 1
           ) s ON TRUE
      JOIN execution_signals es ON es.target_date = s.run_date
                               AND es.ticker = s.ticker
                               AND es.strategy_id = s.strategy_id
      JOIN LATERAL (
            SELECT pnl_date
              FROM signal_pnl
             WHERE signal_id = es.id
             ORDER BY pnl_date DESC
             LIMIT 1
           ) sp ON TRUE
     WHERE NOT EXISTS (
             SELECT 1 FROM signal_pnl p
              WHERE p.signal_id = es.id AND p.exit_slippage_bps IS NOT NULL
           )
"""

# Diagnostic-only count: broker fills that neither candidate branch above can
# attribute — no parent_order_id to walk to a bracket, no ahsx_/ahtp_ prefix
# to match the after-hours branch, AND no alpaca_submissions row shares its
# client_order_id (that column is TEXT NOT NULL UNIQUE since migration 043).
# That last clause matters: a bracket ENTRY fill also has parent_order_id
# IS NULL (it IS the parent order, not a leg), so without it every entry fill
# in the lookback window inflated this count and made the log line read a
# steady non-zero number even on a perfectly healthy day (fix round 2, item
# 2) — an entry's own client_order_id always matches its own submission row,
# so the NOT EXISTS below correctly excludes it. Never gates the UPDATE;
# purely surfaced in the log line so an operator notices when exits stop
# being explainable (e.g. a closed-order window that no longer covers our
# orders, or a third exit path this join doesn't know about yet) instead of
# silently landing nothing.
_EXIT_ORPHAN_COUNT_SQL = """
    SELECT COUNT(*)
      FROM broker_fills bf
     WHERE bf.parent_order_id IS NULL
       AND LEFT(COALESCE(bf.client_order_id, ''), 5) <> 'ahsx_'
       AND LEFT(COALESCE(bf.client_order_id, ''), 5) <> 'ahtp_'
       AND bf.filled_at >= %s::date - %s
       AND NOT EXISTS (
             SELECT 1 FROM alpaca_submissions a
              WHERE a.client_order_id = bf.client_order_id
           )
"""

# Diagnostic-only count (fix round 2, item 2): how many ah-prefixed
# (ahsx_/ahtp_), parentless fills exist in the lookback window at all —
# independent of whether branch 2 above actually managed to attribute them
# to a submission/signal and land a plan row. backfill_exit_slippage
# subtracts the number of branch-2 rows that made it into the plan from this
# count to report `ah_unmatched=`, catching ah exits that COUNT sees but the
# LATERAL-joined candidate query couldn't resolve (submission out of the
# lookback window, no matching signal, or no usable stop/target level).
_EXIT_AH_COUNT_SQL = """
    SELECT COUNT(*)
      FROM broker_fills bf
     WHERE bf.parent_order_id IS NULL
       AND (LEFT(COALESCE(bf.client_order_id, ''), 5) = 'ahsx_'
            OR LEFT(COALESCE(bf.client_order_id, ''), 5) = 'ahtp_')
       AND bf.filled_at >= %s::date - %s
"""


def exit_level_kind(order_type, client_order_id) -> str:
    """'stop' or 'target' — which bracket level this exit fill was aiming at.

    client_order_id WINS over order_type for ahtp_: the after-hours monitor's
    resting take-profit exits are marketable LIMITS that emulate a stop-style
    fire-and-forget order, and afterhours_tp.classify_exit_fills tags them
    'ah_exit', so order_type typing alone would misscore them.

    ahsx_ is deliberately NOT special-cased here (fix round 2, item 1):
    afterhours_tp.py:349/:352 submits BOTH its stop_breach and tp_reach exits
    through the SAME ahsx_{sym}_{ts} client_order_id, so a prefix match alone
    cannot tell which level a given ahsx_ fill was aiming at — scoring every
    ahsx_ fill against stop_loss put a tp_reach fill near target_1 thousands
    of bps "off". That disambiguation now happens upstream, in
    plan_exit_slippage, via pick_ah_level() choosing whichever of
    stop_loss/target_1 the fill price actually landed near. An ahsx_ coid
    reaching this function (nothing in this module still calls it that way)
    falls through to the order_type default below like any other order."""
    coid = str(client_order_id or '')
    if coid.startswith('ahtp_'):
        return 'target'
    if str(order_type or '').lower() in ('stop', 'stop_limit', 'trailing_stop'):
        return 'stop'
    return 'target'


def pick_ah_level(price, stop_loss, target_1):
    """For an ahsx_ top-level after-hours exit, choose stop vs target by
    PROXIMITY to the fill price (fix round 2, item 1).

    afterhours_tp.py:349 (`reason, level = 'stop_breach', stop`) and :352
    (`'tp_reach', tp`) both submit through the same ahsx_{sym}_{ts}
    client_order_id — client_order_id alone can't disambiguate which level a
    given ahsx_ fill was aiming at.

    Candidates are the signal's non-null stop_loss and target_1; the smaller
    |fill_price - level| wins. A single non-null candidate is used outright.
    No usable candidate returns None (caller drops the row, same as any
    other missing-level case). An exact tie favors 'stop'.

    Returns (kind, level) where kind is 'stop' or 'target', or None."""
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    sl = None
    tg = None
    if stop_loss is not None:
        try:
            sl = float(stop_loss)
        except (TypeError, ValueError):
            sl = None
    if target_1 is not None:
        try:
            tg = float(target_1)
        except (TypeError, ValueError):
            tg = None
    if sl is None and tg is None:
        return None
    if sl is None:
        return ('target', tg)
    if tg is None:
        return ('stop', sl)
    return ('stop', sl) if abs(px - sl) <= abs(px - tg) else ('target', tg)


def exit_slippage_bps(direction, level, price):
    """Signed adverse-positive slippage of an exit fill vs its intended level.

    LONG exits (sell): filling BELOW the level is adverse -> +bp.
    SHORT exits (buy): filling ABOVE the level is adverse -> +bp.
    Both collapse to dir_sign * (level - price) / level * 10000 with
    dir_sign = +1 for LONG, -1 for SHORT — mirrored numerator, same
    adverse-positive semantics as entry fill_slippage_bps (migration 145,
    parity_mark.backfill_broker_fill_truth:317-322).

    None when the level or the price is missing or non-positive."""
    try:
        lvl = float(level)
        px = float(price)
    except (TypeError, ValueError):
        return None
    if lvl <= 0 or px <= 0:
        return None
    sign = 1.0 if str(direction or '').upper() in ('LONG', 'BUY', 'BUY_VOL') else -1.0
    return sign * (lvl - px) / lvl * 10000.0


def plan_exit_slippage(rows) -> list:
    """Pure: candidate rows from _EXIT_CANDIDATE_SQL -> [(signal_id, pnl_date, bps)].
    Row shape: (activity_id, order_type, client_order_id, price, signal_id,
    direction, stop_loss, target_1, pnl_date).

    ahsx_ rows route through pick_ah_level (fix round 2, item 1) instead of
    exit_level_kind: a single ahsx_ coid covers both stop_breach and
    tp_reach exits, so the level is chosen by proximity to the fill price,
    not by prefix."""
    out = []
    for r in (rows or []):
        (_aid, otype, coid, price, sig_id, direction, stop_loss, target_1, pnl_date) = r
        if str(coid or '').startswith('ahsx_'):
            picked = pick_ah_level(price, stop_loss, target_1)
            if picked is None:
                continue
            level = picked[1]
        else:
            level = stop_loss if exit_level_kind(otype, coid) == 'stop' else target_1
        bps = exit_slippage_bps(direction, level, price)
        if bps is None:
            continue
        out.append((sig_id, pnl_date, round(bps, 4)))
    return out


def backfill_exit_slippage(cur, run_date, *, lookback_days: int = 5,
                            dry_run: bool = False) -> int:
    """Attribute broker exit-leg fills to signals and persist exit_slippage_bps
    on each signal's LATEST signal_pnl row (migration 155).

    Attribution: broker_fills.parent_order_id = alpaca_submissions.alpaca_order_id
    identifies the submission whose bracket produced this exit leg (bracket
    branch), OR — for after-hours emulated-stop/target exits, which are
    TOP-LEVEL orders with no parent — a client_order_id ahsx_/ahtp_ prefix
    matched by ticker + opposite side to the latest submission in the
    lookback window (ah branch). Either way the submission maps to its
    signal by (run_date -> target_date, ticker, strategy_id) — the same key
    parity_mark.backfill_broker_fill_truth:323-332 uses for the entry twin.
    Both branches also require bf.ticker to match the submission's own
    ticker (an option leg's OCC symbol must never score against an equity
    level) and bf.side to be the OPPOSITE of the signal's direction (a
    same-side add-on fill is not an exit).

    Idempotent per SIGNAL, not just per signal_pnl row: NOT EXISTS(...)
    checks whether ANY signal_pnl row for this signal already carries a
    value, so a fill already attributed to an older pnl_date row is never
    re-applied to a newer row upserted on a later run_date (the write still
    always targets the signal's LATEST row). Savepoint-isolated; returns
    rows planned (0 on any failure).

    Also logs, every run (including a 0-row plan), two diagnostic counts so
    an unexplained gap in exit attribution is never silent (fix round 2,
    item 2):
      - `orphans=` — broker fills that neither branch could attribute at
        all (no parent_order_id, no ah prefix, and no alpaca_submissions row
        shares their client_order_id — that last check is what keeps a
        bracket ENTRY fill, which also has parent_order_id IS NULL, from
        permanently inflating this count).
      - `ah_unmatched=` — ah-prefixed (ahsx_/ahtp_), parentless fills that
        exist in the lookback window but did NOT end up as a branch-2 row in
        the plan (submission out of the lookback window, no matching
        signal, or no usable stop/target level) — distinct from `orphans=`,
        which only counts fills with NO ah prefix at all.

    dry_run reads and reports but issues no UPDATE — reconcile()'s docstring
    promises dry-run "exits cleanly without touching the DB", and
    PIPELINE_DRY_RUN=1 appends --dry-run to every pipeline step
    (pipeline_orchestrator._resolve_script:491-496), so that path is reachable."""
    try:
        cur.execute('SAVEPOINT sp_exit_slip')
        cur.execute(_EXIT_CANDIDATE_SQL, (
            run_date, int(lookback_days),
            run_date, int(lookback_days),
            run_date, int(lookback_days),
        ))
        rows = cur.fetchall() or []
        plan = plan_exit_slippage(rows)
        if not dry_run:
            for sig_id, pnl_date, bps in plan:
                cur.execute(
                    'UPDATE signal_pnl SET exit_slippage_bps = %s '
                    'WHERE signal_id = %s AND pnl_date = %s AND exit_slippage_bps IS NULL',
                    (bps, sig_id, pnl_date))
        cur.execute(_EXIT_ORPHAN_COUNT_SQL, (run_date, int(lookback_days)))
        orphan_row = cur.fetchone()
        n_orphan = int(orphan_row[0]) if orphan_row and orphan_row[0] is not None else 0
        cur.execute(_EXIT_AH_COUNT_SQL, (run_date, int(lookback_days)))
        ah_row = cur.fetchone()
        n_ah_total = int(ah_row[0]) if ah_row and ah_row[0] is not None else 0
        n_branch2_in_plan = len(plan_exit_slippage(
            [r for r in rows if str(r[2] or '').startswith(('ahsx_', 'ahtp_'))]))
        n_ah_unmatched = max(0, n_ah_total - n_branch2_in_plan)
        cur.execute('RELEASE SAVEPOINT sp_exit_slip')
        log(f'exit slippage: n={len(plan)} exit-leg bp value(s)'
            f'{" (DRY-RUN, not written)" if dry_run else " persisted"}, '
            f'orphans={n_orphan} ah_unmatched={n_ah_unmatched}')
        return len(plan)
    except Exception as exc:  # noqa: BLE001
        log(f'exit slippage backfill failed ({type(exc).__name__}: {exc}) — skipped')
        try:
            cur.execute('ROLLBACK TO SAVEPOINT sp_exit_slip')
            cur.execute('RELEASE SAVEPOINT sp_exit_slip')
        except Exception:  # noqa: BLE001
            pass
        return 0


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
    _reset_filled_at_missing_log()
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

    # ── B2: append the raw fill activities to the broker_fills ledger ───────
    # The enrichment read is symbol-scoped to today's fill symbols: the four
    # order-shape columns are not on activity records, and a --nested closed
    # order list is the only place a leg's parent is visible. It's a pure
    # broker CLI read (no DB writes), so it's done ABOVE the savepoint and
    # OUTSIDE the ingest try below — it cannot dirty the transaction, and an
    # unexpected failure here (beyond the ok=False the read already reports
    # on a CLI error) must still let fills land with NULL order-shape columns
    # rather than lose the day's fills entirely.
    try:
        from execution.stop_reattach import fetch_recent_closed_orders
        _syms = sorted({f.get('symbol') for f in fills if f.get('symbol')})
        _ok_meta, _orders = fetch_recent_closed_orders(_syms, include_unscoped=False)
    except Exception as exc:  # noqa: BLE001
        log(f'closed-order enrichment read failed ({type(exc).__name__}: {exc}) — '
            f'ingesting fills with NULL order-shape columns')
        _ok_meta, _orders = False, []

    # Savepoint-isolated so a missing migration or a broker hiccup can never
    # poison the submission reconcile above — that is this step's critical path.
    try:
        cur.execute('SAVEPOINT sp_broker_fills')
        ingest_broker_fills(cur, fills, build_order_meta(_orders) if _ok_meta else {},
                            dry_run=dry_run)
        backfill_exit_slippage(cur, run_date, dry_run=dry_run)
        cur.execute('RELEASE SAVEPOINT sp_broker_fills')
    except Exception as exc:  # noqa: BLE001
        log(f'broker_fills ingest skipped ({type(exc).__name__}: {exc})')
        try:
            cur.execute('ROLLBACK TO SAVEPOINT sp_broker_fills')
            cur.execute('RELEASE SAVEPOINT sp_broker_fills')
        except Exception:  # noqa: BLE001
            pass

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
    _reset_filled_at_missing_log()
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

        # Stream B (2026-09-12) item 15: per-ticker ownership ledger. REPORT-ONLY
        # here — OPENCLAW_OWNERSHIP_BLOCK=1 is what makes the sizer act on it.
        # run_ownership_pass swallows its own failures and returns {'skipped': …},
        # and does its DB work (cursor, SAVEPOINT sp_ownership, commit) inside a
        # try that rolls back to the savepoint on any failure, so an unapplied
        # migration 156 rolls back cleanly instead of poisoning this connection
        # for whatever runs after it. The try/except here is defense in depth
        # (fix round 1, item 1) — even the import itself must not be able to
        # fail this step, which is main()'s critical path (only RuntimeError is
        # caught below).
        try:
            from execution.position_ownership import run_ownership_pass
            log(f'ownership: {run_ownership_pass(conn, args.date, dry_run=args.dry_run, log_fn=log)}')
        except Exception as exc:  # noqa: BLE001
            log(f'[ownership] skipped: {type(exc).__name__}: {str(exc)[:120]}')
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
