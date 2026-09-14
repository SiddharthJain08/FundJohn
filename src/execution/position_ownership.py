"""position_ownership.py — per-ticker broker-vs-ledger ownership.

Stream B item 15 (docs/specs/2026-09-12-quantdinger-adoptions-spec.md:167-181).

Nightly, in the pipeline's `reconcile` step: for each ticker compare the
broker's signed share count against what the open execution_signals rows claim,
classify ok / unallocated / shortfall, append one row per (cycle_date, ticker),
and log ONLY on a status transition.

Enforcement is opt-in. With OPENCLAW_OWNERSHIP_BLOCK=1 the sizer sheds OPENS and
ADDS on tickers whose latest status is not ok. Unset (the default) is report-only
and byte-identical to today's behaviour. Exits and flattens are NEVER blocked —
the sizer applies the blocklist through the same `_shed` helper the stop-out
cooldown uses (regime_blended_sizer._apply_entry_hygiene_gate:2398-2404).

signal_qty: execution_signals carries no share column, so the count comes from
alpaca_submissions.filled_qty joined by (run_date -> target_date, ticker,
strategy_id) — the key parity_mark.backfill_broker_fill_truth:323-332 uses.
filled_qty is the ENTRY quantity and never decreases, so it is netted by the
exit fills in broker_fills (migration 155): any fill on that ticker after the
earliest entry submission whose order_id is NOT itself a recorded entry
submission is an exit, applied ONCE per ticker. Before broker_fills has a day of
history a partially-exited position reads `shortfall` — which is why the default
is report-only.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

EXTRA_TOL_FRAC = 0.001      # broker may hold 0.1% more than the ledger claims
SHORTFALL_TOL_FRAC = 0.005  # ledger may claim 0.5% more than the broker holds
DUST_SHARES = 1.0           # absolute floor: 1 share is never a finding

STATUS_OK = 'ok'
STATUS_UNALLOCATED = 'unallocated'
STATUS_SHORTFALL = 'shortfall'


def classify(account_qty, signal_qty, *,
             extra_tol: float = EXTRA_TOL_FRAC,
             shortfall_tol: float = SHORTFALL_TOL_FRAC,
             dust: float = DUST_SHARES):
    """(unknown_qty, status) for one ticker.

    unknown = account - signal on SIGNED quantities, so a short book reads the
    same way a long one does. unknown > 0 means the broker holds exposure no
    open signal claims (unallocated); unknown < 0 means open signals claim
    exposure the broker does not hold (shortfall) — the dangerous direction,
    which is why it gets the looser band before we cry wolf. Both are floored at
    `dust` shares so rounding residue is never a finding."""
    a = float(account_qty)
    s = float(signal_qty)
    unknown = a - s
    scale = max(abs(a), abs(s))
    if unknown > 0:
        tol = max(dust, extra_tol * scale)
        return unknown, (STATUS_OK if unknown <= tol else STATUS_UNALLOCATED)
    if unknown < 0:
        tol = max(dust, shortfall_tol * scale)
        return unknown, (STATUS_OK if -unknown <= tol else STATUS_SHORTFALL)
    return 0.0, STATUS_OK


def compute_ownership(account_qty: dict, signal_qty: dict, **kw) -> list:
    """[{ticker, account_qty, signal_qty, unknown_qty, status}] over the UNION of
    both key sets, sorted by ticker. A ticker present on only one side gets 0.0
    on the other — exactly the case this ledger exists to catch."""
    rows = []
    for tkr in sorted(set(account_qty or {}) | set(signal_qty or {})):
        a = float((account_qty or {}).get(tkr) or 0.0)
        s = float((signal_qty or {}).get(tkr) or 0.0)
        unknown, status = classify(a, s, **kw)
        rows.append({'ticker': tkr, 'account_qty': a, 'signal_qty': s,
                     'unknown_qty': round(unknown, 6), 'status': status})
    return rows


def transitions(rows, previous: dict) -> list:
    """One line per ticker whose status CHANGED vs `previous` ({ticker: status}
    from the last cycle). A ticker with no prior row counts as a transition only
    when its status is not ok — the first sighting of a healthy name is not news
    and would flood the log on the very first pass."""
    previous = previous or {}
    out = []
    for r in rows:
        prev = previous.get(r['ticker'])
        if prev == r['status']:
            continue
        if prev is None and r['status'] == STATUS_OK:
            continue
        out.append(f"[ownership] {r['ticker']}: {prev or 'new'} -> {r['status']} "
                   f"(account={r['account_qty']:g} signal={r['signal_qty']:g} "
                   f"unknown={r['unknown_qty']:+g})")
    return out


def load_account_qty(fetch_positions=None):
    """{ticker: signed share qty} from the broker, or None when the CLI call
    FAILED. stop_reattach.fetch_positions returns None for "couldn't ask" vs []
    for a genuinely flat book (:234-236); the ownership pass MUST skip on None —
    classifying an unreadable book would mark every ticker shortfall."""
    if fetch_positions is None:
        from execution.stop_reattach import fetch_positions as _fp
        fetch_positions = _fp
    positions = fetch_positions()
    if positions is None:
        return None
    out: dict = {}
    for p in positions:
        sym = p.get('symbol')
        if not sym:
            continue
        try:
            out[sym] = out.get(sym, 0.0) + float(p.get('qty') or 0.0)
        except (TypeError, ValueError):
            continue
    return out


_SIGNAL_QTY_SQL = """
    WITH entries AS (
        SELECT es.ticker,
               SUM(CASE WHEN UPPER(es.direction) IN ('LONG','BUY','BUY_VOL')
                        THEN 1 ELSE -1 END * COALESCE(s.filled_qty, 0)) AS entry_qty,
               MIN(s.submitted_at) AS first_submitted_at
          FROM execution_signals es
          JOIN alpaca_submissions s ON s.run_date = es.target_date
                                   AND s.ticker = es.ticker
                                   AND s.strategy_id = es.strategy_id
         WHERE es.status = 'open'
           AND (es.lifecycle_state IS NULL OR es.lifecycle_state = 'FILLED')
           AND es.ticker IS NOT NULL
         GROUP BY es.ticker
    )
    SELECT e.ticker,
           e.entry_qty + COALESCE((
               SELECT SUM(CASE WHEN LOWER(bf.side) = 'sell' THEN -bf.qty ELSE bf.qty END)
                 FROM broker_fills bf
                WHERE bf.ticker = e.ticker
                  AND bf.filled_at >= e.first_submitted_at
                  AND NOT EXISTS (SELECT 1 FROM alpaca_submissions s2
                                   WHERE s2.alpaca_order_id = bf.order_id)
           ), 0) AS signal_qty
      FROM entries e
"""


def load_signal_qty(cur) -> dict:
    """{ticker: signed share qty the open signals claim}. Entry fills netted by
    every non-entry fill on that ticker since the earliest entry submission —
    applied ONCE per ticker, never once per signal (two strategies on the same
    name would otherwise double-count the exit)."""
    cur.execute(_SIGNAL_QTY_SQL)
    return {r[0]: float(r[1] or 0.0) for r in (cur.fetchall() or []) if r and r[0]}


def load_previous_statuses(cur, cycle_date) -> dict:
    """{ticker: status} at the most recent cycle_date STRICTLY BEFORE this one —
    the baseline `transitions` diffs against."""
    cur.execute(
        'SELECT ticker, status FROM position_ownership WHERE cycle_date = '
        '(SELECT MAX(cycle_date) FROM position_ownership WHERE cycle_date < %s)',
        (cycle_date,))
    return {r[0]: r[1] for r in (cur.fetchall() or []) if r and r[0]}


def persist_ownership(cur, cycle_date, rows) -> int:
    """Append one row per ticker. ON CONFLICT DO NOTHING: a second reconcile run
    on the same cycle_date is a no-op, never a rewrite (append-only)."""
    n = 0
    for r in rows:
        cur.execute(
            'INSERT INTO position_ownership '
            '(cycle_date, ticker, account_qty, signal_qty, unknown_qty, status) '
            'VALUES (%s,%s,%s,%s,%s,%s) '
            'ON CONFLICT (cycle_date, ticker) DO NOTHING',
            (cycle_date, r['ticker'], r['account_qty'], r['signal_qty'],
             r['unknown_qty'], r['status']))
        n += 1
    return n


def latest_status_map(cur) -> dict:
    """{ticker: status} at the newest cycle_date — what the sizer blocklist and
    the position_ownership_clean system check read."""
    cur.execute(
        'SELECT ticker, status FROM position_ownership WHERE cycle_date = '
        '(SELECT MAX(cycle_date) FROM position_ownership)')
    return {r[0]: r[1] for r in (cur.fetchall() or []) if r and r[0]}


def run_ownership_pass(conn, cycle_date, *, dry_run: bool = False,
                       account_qty=None, log_fn=None) -> dict:
    """The reconcile-step hook. Returns
    {'rows', 'unallocated', 'shortfall', 'transitions'} or {'skipped': reason}.

    NEVER raises: an ownership finding must not fail the reconcile step, whose
    critical path is the submission reconcile. `conn` may already have other
    work committed on it (reconcile() / cleanup_phantom_signals each own their
    own commit) — this pass wraps its DB work in its own SAVEPOINT, mirroring
    sp_broker_fills / sp_filled_at / sp_exit_slip, so an UNAPPLIED migration
    156 (position_ownership missing) rolls back cleanly instead of leaving the
    connection InFailedSqlTransaction for whatever runs after it."""
    emit = log_fn or logger.info
    try:
        if account_qty is None:
            account_qty = load_account_qty()
    except Exception as e:  # noqa: BLE001
        emit(f'[ownership] pass failed ({type(e).__name__}: {e}) — skipped')
        return {'skipped': f'{type(e).__name__}: {e}'}
    if account_qty is None:
        emit('[ownership] broker position list unavailable — pass SKIPPED')
        return {'skipped': 'broker_unavailable'}

    cur = conn.cursor()
    cur.execute('SAVEPOINT sp_ownership')
    try:
        signal_qty = load_signal_qty(cur)
        rows = compute_ownership(account_qty, signal_qty)
        lines = transitions(rows, load_previous_statuses(cur, cycle_date))
        if not dry_run:
            persist_ownership(cur, cycle_date, rows)
        cur.execute('RELEASE SAVEPOINT sp_ownership')
    except Exception as e:  # noqa: BLE001
        try:
            cur.execute('ROLLBACK TO SAVEPOINT sp_ownership')
            cur.execute('RELEASE SAVEPOINT sp_ownership')
        except Exception:  # noqa: BLE001
            pass
        emit(f'[ownership] pass failed ({type(e).__name__}: {e}) — skipped')
        return {'skipped': f'{type(e).__name__}: {e}'}

    if not dry_run:
        conn.commit()
    for line in lines:
        emit(line)
    stats = {
        'rows': len(rows),
        'unallocated': sum(1 for r in rows if r['status'] == STATUS_UNALLOCATED),
        'shortfall': sum(1 for r in rows if r['status'] == STATUS_SHORTFALL),
        'transitions': lines,
    }
    emit(f"[ownership] {stats['rows']} ticker(s): {stats['unallocated']} unallocated, "
         f"{stats['shortfall']} shortfall, {len(lines)} transition(s)")
    return stats
