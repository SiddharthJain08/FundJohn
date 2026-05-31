"""tests/test_sp6_update_pnl_mark.py

Integration tests for engine.update_pnl P&L calculation with mark_entry_price
and lifecycle_state filtering (SP-6 Task 6).

Invariants:
  (a) mark_entry_price preferred: P&L computed off mark, NOT entry_price; asserts
      result DIFFERS from entry_price-based value and MATCHES mark-based math.
  (b) legacy NULL mark_entry_price: falls back to entry_price — byte-identical;
      also asserts days_held comes from signal_date when target_date is NULL.
  (c) CLOSED_AT_OPEN skip: no signal_pnl row created even when status='open'.

Run:
    source ./worktree-env.sh
    python3 -m pytest tests/test_sp6_update_pnl_mark.py -v
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402

pytestmark = pytest.mark.integration

DSN = os.environ.get("POSTGRES_URI")


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
    """Resolve real workspace_id and an approved strategy_id from the live DB.
    Also patches engine.WORKSPACE to the real UUID so update_pnl's WHERE clause
    matches the inserted test signal.
    """
    db_conn.execute("SELECT id FROM workspaces WHERE name='default' LIMIT 1")
    ws_row = db_conn.fetchone()
    assert ws_row is not None, "No 'default' workspace"
    ws_id = ws_row['id']

    db_conn.execute("SELECT id FROM strategy_registry WHERE status='approved' LIMIT 1")
    st_row = db_conn.fetchone()
    assert st_row is not None, "No approved strategy in strategy_registry"
    strategy_id = st_row['id']

    # engine.WORKSPACE defaults to 'default' (a string), but the DB stores
    # workspace_id as UUID. Patch it to the real UUID for the duration of the test.
    original_ws = engine.WORKSPACE
    engine.WORKSPACE = str(ws_id)
    yield {'ws_id': ws_id, 'strategy_id': strategy_id}
    engine.WORKSPACE = original_ws


def _insert_signal(cur, *, ws_id, strategy_id,
                   entry_price, mark_entry_price, target_date,
                   lifecycle_state, signal_date,
                   stop_loss=95.0, target_1=110.0,
                   ticker='ZZPNLTEST', direction='LONG') -> int:
    """Insert a minimal execution_signals row and return its id."""
    cur.execute("""
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
    """, (
        strategy_id, ws_id, signal_date, ticker, direction,
        entry_price, mark_entry_price, target_date, lifecycle_state,
        stop_loss, target_1, None, None,
        0.05, 'NORMAL', '{}', 'open',
    ))
    row = cur.fetchone()
    return row['id'] if hasattr(row, 'keys') else row[0]


class TestUpdatePnlMarkEntryPrice:
    # ─────────────────────────────────────────────────────────────────
    # (a) mark_entry_price preferred
    # ─────────────────────────────────────────────────────────────────
    def test_mark_entry_price_preferred_over_entry_price(self, db_conn, _db_meta):
        """Signal with mark_entry_price=105 (entry_price=100):
        P&L computed off 105, NOT 100; asserts both that it DIFFERS from the
        entry_price path and MATCHES the mark-based math.
        """
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZPNLMRK',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=105.0,
            target_date=date(2026, 6, 20),
            lifecycle_state='COMPUTED',
            stop_loss=95.0,
            target_1=115.0,
        )

        current_price = 107.0
        prices = pd.DataFrame({'ZZPNLMRK': [current_price]})
        run_date = date(2026, 5, 21)

        engine.update_pnl(db_conn, prices, run_date)

        db_conn.execute(
            "SELECT unrealized_pnl_pct FROM signal_pnl WHERE signal_id = %s",
            (sig_id,)
        )
        row = db_conn.fetchone()
        assert row is not None, "signal_pnl row must be created"

        actual_pct = float(row['unrealized_pnl_pct'])

        # Mark-based math: (107 - 105) / 105
        expected_mark_pct = (current_price - 105.0) / 105.0
        # Entry_price-based math: (107 - 100) / 100
        expected_entry_pct = (current_price - 100.0) / 100.0

        assert abs(actual_pct - expected_mark_pct) < 1e-5, (
            f"Expected mark-based pct ~{expected_mark_pct:.6f}, got {actual_pct:.6f}"
        )
        assert abs(actual_pct - expected_entry_pct) > 1e-5, (
            f"P&L should DIFFER from entry_price-based value {expected_entry_pct:.6f} "
            f"but got {actual_pct:.6f}"
        )

    # ─────────────────────────────────────────────────────────────────
    # (b) legacy NULL mark_entry_price — byte-identical fallback
    # ─────────────────────────────────────────────────────────────────
    def test_legacy_null_mark_uses_entry_price(self, db_conn, _db_meta):
        """Signal with mark_entry_price=NULL: falls back to entry_price=100
        and days_held is computed from signal_date (target_date also NULL).
        """
        signal_date = date(2026, 5, 15)
        run_date = date(2026, 5, 21)
        current_price = 103.0

        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=signal_date,
            ticker='ZZPNLLEG',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=None,
            target_date=None,
            lifecycle_state=None,
            stop_loss=95.0,
            target_1=115.0,
        )

        prices = pd.DataFrame({'ZZPNLLEG': [current_price]})
        engine.update_pnl(db_conn, prices, run_date)

        db_conn.execute(
            "SELECT unrealized_pnl_pct, days_held FROM signal_pnl WHERE signal_id = %s",
            (sig_id,)
        )
        row = db_conn.fetchone()
        assert row is not None, "signal_pnl row must be created for legacy signal"

        # Pct off entry_price=100
        expected_pct = (current_price - 100.0) / 100.0
        assert abs(float(row['unrealized_pnl_pct']) - expected_pct) < 1e-5, (
            f"Expected entry_price-based pct {expected_pct:.6f}, "
            f"got {float(row['unrealized_pnl_pct']):.6f}"
        )

        # days_held from signal_date (not target_date, which is NULL)
        expected_days = (run_date - signal_date).days
        assert row['days_held'] == expected_days, (
            f"Expected days_held={expected_days} (from signal_date), "
            f"got {row['days_held']}"
        )

    # ─────────────────────────────────────────────────────────────────
    # (c) CLOSED_AT_OPEN skip
    # ─────────────────────────────────────────────────────────────────
    def test_closed_at_open_is_skipped(self, db_conn, _db_meta):
        """Signal with lifecycle_state='CLOSED_AT_OPEN' and status='open' is
        skipped entirely — no signal_pnl row created.

        This is belt-and-suspenders: such rows normally have status='closed'
        already, but a dropped/flattened position that retained status='open'
        must not be re-marked.
        """
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZPNLCAO',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=100.0,
            target_date=date(2026, 6, 10),
            lifecycle_state='CLOSED_AT_OPEN',
            stop_loss=95.0,
            target_1=115.0,
        )

        prices = pd.DataFrame({'ZZPNLCAO': [101.0]})
        engine.update_pnl(db_conn, prices, date(2026, 5, 21))

        db_conn.execute(
            "SELECT id FROM signal_pnl WHERE signal_id = %s",
            (sig_id,)
        )
        assert db_conn.fetchone() is None, (
            "CLOSED_AT_OPEN signal must not produce a signal_pnl row"
        )

    # ─────────────────────────────────────────────────────────────────
    # non-finite mark_entry_price falls back to entry_price
    # ─────────────────────────────────────────────────────────────────
    def test_non_finite_mark_falls_back_to_entry_price(self, db_conn, _db_meta):
        """Signal with mark_entry_price=NaN (Decimal) falls back to entry_price.

        This matches the documented Decimal('NaN') case from the sharpe_cadence
        drift incident — the isfinite guard must fire and use entry_price.
        """
        import decimal
        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=date(2026, 5, 20),
            ticker='ZZPNLNAN',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=decimal.Decimal('NaN'),  # NUMERIC NaN
            target_date=date(2026, 6, 20),
            lifecycle_state='COMPUTED',
            stop_loss=95.0,
            target_1=115.0,
        )

        current_price = 103.0
        prices = pd.DataFrame({'ZZPNLNAN': [current_price]})
        engine.update_pnl(db_conn, prices, date(2026, 5, 21))

        db_conn.execute(
            "SELECT unrealized_pnl_pct FROM signal_pnl WHERE signal_id = %s",
            (sig_id,)
        )
        row = db_conn.fetchone()
        assert row is not None, "signal_pnl row must be created"

        # Should fall back to entry_price=100, not NaN
        expected_pct = (current_price - 100.0) / 100.0
        assert abs(float(row['unrealized_pnl_pct']) - expected_pct) < 1e-5, (
            f"NaN mark should fall back to entry_price; expected {expected_pct:.6f}, "
            f"got {float(row['unrealized_pnl_pct']):.6f}"
        )

    # ─────────────────────────────────────────────────────────────────
    # target_date used for days_held when present
    # ─────────────────────────────────────────────────────────────────
    def test_target_date_used_for_days_held_when_present(self, db_conn, _db_meta):
        """target_date overrides signal_date for days_held calculation."""
        signal_date = date(2026, 5, 1)  # far earlier
        target_date = date(2026, 6, 10)
        run_date = date(2026, 5, 21)

        sig_id = _insert_signal(
            db_conn,
            ws_id=_db_meta['ws_id'],
            strategy_id=_db_meta['strategy_id'],
            signal_date=signal_date,
            ticker='ZZPNLTDT',
            direction='LONG',
            entry_price=100.0,
            mark_entry_price=100.0,
            target_date=target_date,
            lifecycle_state='COMPUTED',
            stop_loss=95.0,
            target_1=115.0,
        )

        prices = pd.DataFrame({'ZZPNLTDT': [101.0]})
        engine.update_pnl(db_conn, prices, run_date)

        db_conn.execute(
            "SELECT days_held FROM signal_pnl WHERE signal_id = %s",
            (sig_id,)
        )
        row = db_conn.fetchone()
        assert row is not None

        # days_held from target_date: run_date(2026-05-21) - target_date(2026-06-10) = -20
        expected_days = (run_date - target_date).days
        assert row['days_held'] == expected_days, (
            f"Expected days_held={expected_days} (from target_date), "
            f"got {row['days_held']}"
        )

        # Sanity: signal_date path would give different result
        signal_date_days = (run_date - signal_date).days
        assert expected_days != signal_date_days, (
            "test is trivial: target_date path and signal_date path give same days_held"
        )
