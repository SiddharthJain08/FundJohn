"""tests/test_sp6_ledger_close.py — SP-6 Task 9: open_reconcile ledger-close helpers.

Integration tests for drop_signal_close() + flatten_signal_close() and the
phantom-row fix they provide.

Invariants verified:
  (a) drop_signal_close() upserts signal_pnl with status='closed',
      close_reason='signal_dropped', correct mark-based realized_pnl_pct, and
      updates execution_signals to status='closed', lifecycle_state='CLOSED_AT_OPEN',
      fill_price=closed_price.
  (b) phantom-row fix: after drop_signal_close(), a subsequent engine.update_pnl()
      run does NOT re-open/re-mark the signal (signal_pnl unchanged, execution_signals
      stays 'closed').
  (c) flatten_signal_close() produces close_reason='flattened'.
  (d) mark_entry_price fallback: when mark is NULL, entry_price is used for
      realized_pnl_pct.
  (e) SHORT direction P&L sign: realized_pnl_pct = (entry - closed) / entry.

Run:
    source ./worktree-env.sh
    python3 -m pytest tests/test_sp6_ledger_close.py -v

Requirements:
    - POSTGRES_URI set (worktree-env.sh loads it)
    - Migration 126 applied (lifecycle_state, mark_entry_price, fill_price,
      filled_at, target_date columns exist on execution_signals)
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.open_reconcile import drop_signal_close, flatten_signal_close  # noqa: E402
from execution import engine  # noqa: E402

pytestmark = pytest.mark.integration

DSN = os.environ.get("POSTGRES_URI")


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures — cloned from test_sp6_update_pnl_mark.py for consistent cursor type
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """Auto-rollback RealDictCursor connection for test isolation."""
    assert DSN, "POSTGRES_URI required"
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    yield cur
    conn.rollback()
    cur.close()
    conn.close()


@pytest.fixture
def _db_meta(db_conn):
    """Resolve real workspace_id + an approved strategy_id from the live DB.
    Patches engine.WORKSPACE to the real UUID so update_pnl's WHERE clause
    matches our test signals (mirroring test_sp6_update_pnl_mark.py).
    """
    db_conn.execute("SELECT id FROM workspaces WHERE name='default' LIMIT 1")
    ws_row = db_conn.fetchone()
    assert ws_row is not None, "No 'default' workspace"
    ws_id = ws_row['id']

    db_conn.execute("SELECT id FROM strategy_registry WHERE status='approved' LIMIT 1")
    st_row = db_conn.fetchone()
    assert st_row is not None, "No approved strategy in strategy_registry"
    strategy_id = st_row['id']

    original_ws = engine.WORKSPACE
    engine.WORKSPACE = str(ws_id)
    yield {'ws_id': ws_id, 'strategy_id': strategy_id}
    engine.WORKSPACE = original_ws


def _insert_signal(
    cur, *, ws_id, strategy_id,
    entry_price, mark_entry_price,
    target_date, lifecycle_state, signal_date,
    stop_loss=95.0, target_1=110.0,
    ticker='ZZRECLTEST', direction='LONG',
) -> str:
    """Insert a minimal execution_signals row; return its id (UUID string)."""
    cur.execute(
        """
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, mark_entry_price, target_date, lifecycle_state,
             stop_loss, target_1, target_2, target_3,
             position_size_pct, regime_state, signal_params, status)
        VALUES
            (%s, %s, %s, %s, %s,
             %s, %s, %s, %s,
             %s, %s, %s, %s,
             %s, %s, %s::jsonb, %s)
        RETURNING id
        """,
        (
            strategy_id, ws_id, signal_date, ticker, direction,
            entry_price, mark_entry_price, target_date, lifecycle_state,
            stop_loss, target_1, None, None,
            0.05, 'NORMAL', '{}', 'open',
        ),
    )
    row = cur.fetchone()
    return row['id'] if hasattr(row, 'keys') else row[0]


# ──────────────────────────────────────────────────────────────────────────────
# (a) drop_signal_close basic write test
# ──────────────────────────────────────────────────────────────────────────────

class TestDropSignalClose:

    def test_creates_closed_pnl_row_long(self, db_conn, _db_meta):
        """LONG FILLED signal: mark-based realized_pnl_pct; signal_pnl written."""
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZRC001',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=100.0,   # mark == entry for simplicity
            target_date=date(2026, 6, 20),
            lifecycle_state='FILLED',
            stop_loss=95.0,
            target_1=115.0,
        )

        closed_price = 101.0
        drop_signal_close(db_conn, sig_id, 'ZZRC001', closed_price, reason='signal_dropped')

        db_conn.execute(
            """
            SELECT status, closed_price, close_reason, realized_pnl_pct
            FROM signal_pnl WHERE signal_id = %s
            """,
            (sig_id,),
        )
        pnl = db_conn.fetchone()
        assert pnl is not None, "signal_pnl row must exist"
        assert pnl['status'] == 'closed', f"status={pnl['status']}"
        assert float(pnl['closed_price']) == closed_price, f"closed_price={pnl['closed_price']}"
        assert pnl['close_reason'] == 'signal_dropped', f"close_reason={pnl['close_reason']}"

        # LONG realized: (101 - 100) / 100 = 0.01
        expected_pct = (101.0 - 100.0) / 100.0
        assert abs(float(pnl['realized_pnl_pct']) - expected_pct) < 1e-5, (
            f"realized_pnl_pct expected ~{expected_pct:.6f}, got {pnl['realized_pnl_pct']}"
        )

    def test_updates_execution_signals_to_closed_at_open(self, db_conn, _db_meta):
        """execution_signals row set to status='closed', lifecycle_state='CLOSED_AT_OPEN',
        fill_price populated."""
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZRC002',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=100.0,
            target_date=None,
            lifecycle_state='FILLED',
            stop_loss=95.0,
            target_1=115.0,
        )

        closed_price = 99.0
        drop_signal_close(db_conn, sig_id, 'ZZRC002', closed_price)

        db_conn.execute(
            "SELECT status, lifecycle_state, filled_at, fill_price FROM execution_signals WHERE id = %s",
            (sig_id,),
        )
        sig = db_conn.fetchone()
        assert sig is not None
        assert sig['status'] == 'closed', f"status={sig['status']}"
        assert sig['lifecycle_state'] == 'CLOSED_AT_OPEN', f"lifecycle_state={sig['lifecycle_state']}"
        assert sig['filled_at'] is not None, "filled_at must be set"
        assert abs(float(sig['fill_price']) - closed_price) < 1e-6, f"fill_price={sig['fill_price']}"

    def test_uses_mark_entry_price_for_pct(self, db_conn, _db_meta):
        """mark_entry_price=105 (entry_price=100): realized_pnl_pct uses mark."""
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZRC003',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=105.0,
            target_date=date(2026, 6, 10),
            lifecycle_state='FILLED',
            stop_loss=95.0,
            target_1=115.0,
        )

        closed_price = 107.0
        drop_signal_close(db_conn, sig_id, 'ZZRC003', closed_price)

        db_conn.execute(
            "SELECT realized_pnl_pct FROM signal_pnl WHERE signal_id = %s",
            (sig_id,),
        )
        pnl = db_conn.fetchone()
        assert pnl is not None

        # Mark-based: (107 - 105) / 105
        expected_mark = (107.0 - 105.0) / 105.0
        expected_entry = (107.0 - 100.0) / 100.0
        actual = float(pnl['realized_pnl_pct'])

        assert abs(actual - expected_mark) < 1e-5, (
            f"Expected mark-based pct {expected_mark:.6f}, got {actual:.6f}"
        )
        assert abs(actual - expected_entry) > 1e-5, (
            f"pct must DIFFER from entry-price path {expected_entry:.6f}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# (b) phantom-row fix
# ──────────────────────────────────────────────────────────────────────────────

class TestPhantomRowFix:

    def test_update_pnl_does_not_remark_after_drop_close(self, db_conn, _db_meta):
        """Core phantom-row fix: after drop_signal_close(), engine.update_pnl()
        must NOT overwrite the signal_pnl row or re-open the signal.

        Without the fix the signal retains status='open' in execution_signals
        and update_pnl overwrites the 'closed' pnl row with 'open' + new prices.
        With the fix:
          - execution_signals.status='closed' → excluded by update_pnl's WHERE
          - lifecycle_state='CLOSED_AT_OPEN' → excluded by update_pnl's AND guard

        We use today's date for both the helper and update_pnl so the UPSERT
        key (signal_id, pnl_date) is identical — a regression would overwrite
        the 'closed' row in-place.
        """
        run_date = date.today()

        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=run_date,
            ticker='ZZRC004',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=100.0,
            target_date=run_date,
            lifecycle_state='FILLED',
            stop_loss=90.0,
            target_1=115.0,
        )

        # Step 1: close the signal via the helper
        closed_price = 101.0
        drop_signal_close(db_conn, sig_id, 'ZZRC004', closed_price, reason='signal_dropped')

        # Capture the pnl row state before update_pnl
        db_conn.execute(
            "SELECT status, close_reason, closed_price FROM signal_pnl WHERE signal_id = %s AND pnl_date = %s",
            (sig_id, run_date),
        )
        before = db_conn.fetchone()
        assert before is not None, "signal_pnl row must exist after drop_signal_close"
        assert before['status'] == 'closed'
        assert before['close_reason'] == 'signal_dropped'

        # Step 2: run engine.update_pnl with a different price (101.5)
        # If the phantom-row bug were present, this would overwrite the row.
        prices = pd.DataFrame({'ZZRC004': [101.5]})
        engine.update_pnl(db_conn, prices, run_date)

        # Step 3: assert the signal_pnl row is UNCHANGED
        db_conn.execute(
            "SELECT status, close_reason, closed_price FROM signal_pnl WHERE signal_id = %s AND pnl_date = %s",
            (sig_id, run_date),
        )
        after = db_conn.fetchone()
        assert after is not None, "signal_pnl row must still exist (not deleted)"
        assert after['status'] == 'closed', (
            f"status was re-opened by update_pnl: {after['status']} (phantom-row BUG!)"
        )
        assert after['close_reason'] == 'signal_dropped', (
            f"close_reason overwritten: {after['close_reason']}"
        )
        assert abs(float(after['closed_price']) - closed_price) < 1e-6, (
            f"closed_price overwritten from {closed_price} to {after['closed_price']}"
        )

        # Step 4: execution_signals must still be 'closed'
        db_conn.execute("SELECT status FROM execution_signals WHERE id = %s", (sig_id,))
        es = db_conn.fetchone()
        assert es['status'] == 'closed', f"execution_signals status re-opened: {es['status']}"


# ──────────────────────────────────────────────────────────────────────────────
# (c) flatten_signal_close — reason='flattened'
# ──────────────────────────────────────────────────────────────────────────────

class TestFlattenSignalClose:

    def test_flatten_writes_flattened_close_reason(self, db_conn, _db_meta):
        """flatten_signal_close() must produce close_reason='flattened'."""
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZRC005',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=100.0,
            target_date=date(2026, 6, 20),
            lifecycle_state='FILLED',
            stop_loss=95.0,
            target_1=115.0,
        )

        closed_price = 102.0
        flatten_signal_close(db_conn, sig_id, 'ZZRC005', closed_price)

        db_conn.execute(
            "SELECT status, close_reason FROM signal_pnl WHERE signal_id = %s",
            (sig_id,),
        )
        pnl = db_conn.fetchone()
        assert pnl is not None
        assert pnl['status'] == 'closed'
        assert pnl['close_reason'] == 'flattened', f"close_reason={pnl['close_reason']}"

    def test_flatten_also_sets_closed_at_open(self, db_conn, _db_meta):
        """flatten_signal_close() must also set lifecycle_state='CLOSED_AT_OPEN'."""
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZRC006',
            direction='SHORT',
            entry_price=100.0,
            mark_entry_price=100.0,
            target_date=None,
            lifecycle_state='FILLED',
            stop_loss=105.0,
            target_1=90.0,
        )

        closed_price = 99.0
        flatten_signal_close(db_conn, sig_id, 'ZZRC006', closed_price)

        db_conn.execute(
            "SELECT lifecycle_state, fill_price FROM execution_signals WHERE id = %s",
            (sig_id,),
        )
        sig = db_conn.fetchone()
        assert sig['lifecycle_state'] == 'CLOSED_AT_OPEN'
        assert abs(float(sig['fill_price']) - closed_price) < 1e-6


# ──────────────────────────────────────────────────────────────────────────────
# (d) mark_entry_price fallback: NULL mark → entry_price used
# ──────────────────────────────────────────────────────────────────────────────

class TestMarkFallback:

    def test_null_mark_falls_back_to_entry_price(self, db_conn, _db_meta):
        """mark_entry_price=NULL: realized_pnl_pct computed from entry_price."""
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZRC007',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=None,
            target_date=None,
            lifecycle_state=None,   # legacy NULL
            stop_loss=95.0,
            target_1=115.0,
        )

        closed_price = 103.0
        drop_signal_close(db_conn, sig_id, 'ZZRC007', closed_price)

        db_conn.execute(
            "SELECT realized_pnl_pct FROM signal_pnl WHERE signal_id = %s",
            (sig_id,),
        )
        pnl = db_conn.fetchone()
        assert pnl is not None

        expected = (103.0 - 100.0) / 100.0
        assert abs(float(pnl['realized_pnl_pct']) - expected) < 1e-5, (
            f"expected {expected:.6f}, got {pnl['realized_pnl_pct']}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# (e) SHORT direction P&L sign
# ──────────────────────────────────────────────────────────────────────────────

class TestShortDirection:

    def test_short_realized_pnl_pct_sign(self, db_conn, _db_meta):
        """SHORT signal: realized_pnl_pct = (entry - closed) / entry."""
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZRC008',
            direction='SHORT',
            entry_price=100.0,
            mark_entry_price=100.0,
            target_date=None,
            lifecycle_state='FILLED',
            stop_loss=105.0,
            target_1=90.0,
        )

        closed_price = 99.0   # profitable SHORT
        drop_signal_close(db_conn, sig_id, 'ZZRC008', closed_price)

        db_conn.execute(
            "SELECT realized_pnl_pct FROM signal_pnl WHERE signal_id = %s",
            (sig_id,),
        )
        pnl = db_conn.fetchone()
        assert pnl is not None

        # SHORT: (100 - 99) / 100 = 0.01
        expected = (100.0 - 99.0) / 100.0
        assert abs(float(pnl['realized_pnl_pct']) - expected) < 1e-5, (
            f"SHORT realized_pnl_pct expected {expected:.6f}, got {pnl['realized_pnl_pct']}"
        )
