"""src/execution/open_reconcile.py — SP-6 Phase A: open-position reconciliation helpers.

Two module-level helpers for Task 8's run_reconcile():

  drop_signal_close(cur, signal_id, ticker, closed_price, reason='signal_dropped')
  flatten_signal_close(cur, signal_id, ticker, closed_price)

Each helper closes a dropped/flattened signal by:
  1. Upserting a signal_pnl row (status='closed') mirroring engine.update_pnl's upsert.
  2. Updating execution_signals (status='closed', lifecycle_state='CLOSED_AT_OPEN',
     filled_at=NOW(), fill_price=closed_price) to prevent phantom-row re-marking by
     subsequent engine.update_pnl() runs.

Phantom-row fix (SP-6 Task 9):
  Today a dropped/orphan-closed position keeps accruing P&L in update_pnl because
  execution_signals.status remains 'open'. Setting lifecycle_state='CLOSED_AT_OPEN'
  + status='closed' causes engine.update_pnl (post-Task-6) to skip the row via its
  WHERE ... AND (lifecycle_state IS NULL OR lifecycle_state='FILLED') filter, and
  additionally by the explicit CLOSED_AT_OPEN guard at engine.py:1128.

NOTE: Task 8 will add run_reconcile() to this file; these helpers are the only
      contents for now.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# Mirror the module-level constant from engine.py.
WORKSPACE = os.environ.get('WORKSPACE_ID', 'default')


def drop_signal_close(
    cur,
    signal_id: str,
    ticker: str,
    closed_price: float,
    reason: str = 'signal_dropped',
) -> None:
    """Close a signal that was dropped (e.g. position flattened at the open).

    Mirrors engine.py:1196–1225 signal_pnl UPSERT + execution_signals UPDATE.
    Computes realized_pnl_pct from mark_entry_price (or fallback entry_price)
    and direction.  days_held follows the same logic as update_pnl: prefer
    target_date when present, else signal_date.

    Args:
        cur:          psycopg2 cursor (RealDictCursor recommended; bare cursor ok)
        signal_id:    UUID of the execution_signal to close
        ticker:       ticker symbol (context only; signal_id is the key)
        closed_price: market price at which the signal is being closed
        reason:       close_reason written to signal_pnl (default 'signal_dropped')

    Returns:
        None. Raises on DB error.

    Postconditions:
        - signal_pnl row upserted: status='closed', close_reason=reason,
          closed_price=closed_price, realized_pnl_pct computed off mark_entry_price
          (or entry_price fallback when mark is NULL/NaN).
        - execution_signals row updated: status='closed',
          lifecycle_state='CLOSED_AT_OPEN', filled_at=NOW(), fill_price=closed_price.
    """
    # ── 1. Fetch signal metadata ──────────────────────────────────────────
    cur.execute(
        """
        SELECT id, strategy_id, workspace_id, signal_date, direction,
               entry_price, mark_entry_price, target_date
        FROM execution_signals
        WHERE id = %s
        """,
        (signal_id,),
    )
    row = cur.fetchone()
    if row is None:
        logger.warning("drop_signal_close: signal_id %s not found", signal_id)
        return

    # Support both RealDictRow and plain tuple access.
    def _get(key, pos):
        try:
            return row[key]
        except (TypeError, KeyError):
            return row[pos]

    sig_id    = _get('id', 0)
    strat_id  = _get('strategy_id', 1)
    ws_id     = _get('workspace_id', 2)
    sig_date  = _get('signal_date', 3)
    direction = _get('direction', 4)
    _raw_entry = _get('entry_price', 5)
    _raw_mark  = _get('mark_entry_price', 6)
    target_dt  = _get('target_date', 7)

    # ── 2. Resolve effective entry price (mirrors engine.py:1133–1142) ──
    entry: float | None = None
    if _raw_mark is not None:
        try:
            _mark_f = float(_raw_mark)
            entry = _mark_f if math.isfinite(_mark_f) else None
        except (ValueError, TypeError):
            entry = None
    if entry is None and _raw_entry is not None:
        try:
            entry = float(_raw_entry)
            if not math.isfinite(entry):
                entry = None
        except (ValueError, TypeError):
            entry = None

    # ── 3. Compute realized_pnl_pct (guard entry <= 0 / None) ────────────
    direction_upper = (direction or '').upper()
    if entry and entry > 0:
        if direction_upper == 'LONG':
            realized_pct = (closed_price - entry) / entry
        elif direction_upper == 'SHORT':
            realized_pct = (entry - closed_price) / entry
        else:
            realized_pct = 0.0
        if not math.isfinite(realized_pct):
            realized_pct = 0.0
    else:
        realized_pct = 0.0
        logger.warning(
            "drop_signal_close %s: no valid entry price (entry=%s); realized_pnl_pct=0.0",
            sig_id, entry,
        )

    # ── 4. Compute days_held (mirrors engine.py:1154–1158) ───────────────
    run_date = date.today()
    if target_dt is not None and isinstance(target_dt, date):
        days_held = (run_date - target_dt).days
    elif sig_date is not None and isinstance(sig_date, date):
        days_held = (run_date - sig_date).days
    else:
        days_held = 0

    now_utc = datetime.now(timezone.utc)

    # ── 5. Upsert signal_pnl (mirrors engine.py:1196–1219) ───────────────
    cur.execute(
        """
        INSERT INTO signal_pnl
            (signal_id, strategy_id, workspace_id, pnl_date,
             close_price, unrealized_pnl_pct, days_held, status,
             closed_price, closed_at, close_reason, realized_pnl_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (signal_id, pnl_date) DO UPDATE SET
            close_price        = EXCLUDED.close_price,
            unrealized_pnl_pct = EXCLUDED.unrealized_pnl_pct,
            days_held          = EXCLUDED.days_held,
            status             = EXCLUDED.status,
            closed_price       = EXCLUDED.closed_price,
            closed_at          = EXCLUDED.closed_at,
            close_reason       = EXCLUDED.close_reason,
            realized_pnl_pct   = EXCLUDED.realized_pnl_pct
        """,
        (
            sig_id, strat_id, ws_id, run_date,
            closed_price, round(realized_pct, 6), days_held,
            'closed',
            closed_price,
            run_date,          # closed_at is DATE in signal_pnl schema
            reason,
            round(realized_pct, 6),
        ),
    )
    logger.info(
        "drop_signal_close: upserted signal_pnl %s reason=%s realized_pct=%.6f",
        sig_id, reason, realized_pct,
    )

    # ── 6. Update execution_signals → prevent phantom-row re-marking ─────
    cur.execute(
        """
        UPDATE execution_signals
        SET
            status          = 'closed',
            lifecycle_state = 'CLOSED_AT_OPEN',
            filled_at       = %s,
            fill_price      = %s
        WHERE id = %s
        """,
        (now_utc, closed_price, sig_id),
    )
    logger.info(
        "drop_signal_close: updated execution_signals %s → CLOSED_AT_OPEN fill_price=%s",
        sig_id, closed_price,
    )


def flatten_signal_close(
    cur,
    signal_id: str,
    ticker: str,
    closed_price: float,
) -> None:
    """Close a signal that is being flattened (position reduced to zero).

    Identical to drop_signal_close but with close_reason='flattened'.

    Args:
        cur:          psycopg2 cursor
        signal_id:    UUID of the execution_signal to close
        ticker:       ticker symbol (context only)
        closed_price: market price at which the position is being flattened

    Returns:
        None. Raises on DB error.
    """
    drop_signal_close(
        cur=cur,
        signal_id=signal_id,
        ticker=ticker,
        closed_price=closed_price,
        reason='flattened',
    )
