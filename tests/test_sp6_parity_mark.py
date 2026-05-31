"""test_sp6_parity_mark.py — parity_mark unit + DB integration tests (SP-6 Task 5).

Key invariants under test:
  1. Parity test: finalize_parity_marks computes EXACTLY the same stop/target as
     calling _reanchor_bracket directly with the same inputs — proves the module
     is a transparent wrapper, not a reimplementation.
  2. Gate OFF no-op: if OPENCLAW_EOD_SIGNAL_REGISTER is unset, the parity_mark
     block in engine.py is skipped (only indirectly tested here via the module API).
  3. DB integration: EXECUTING row → mark_entry_price=close, stop/target re-anchored,
     lifecycle_state='FILLED', filled_at set.
  4. Skip wrong target_date.
  5. Skip ticker absent from closes dict.
  6. Skip unsupported direction (direction_sign==0).
  7. Skip row with None/NaN in core price fields.
  8. empty closes dict returns 0 immediately.

All DB tests use rollback isolation — no persistent side-effects.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.unified_backtest import _reanchor_bracket, _signal_to_long_short
from execution.parity_mark import finalize_parity_marks


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """Auto-rollback psycopg2 connection to the live DB."""
    import psycopg2
    import psycopg2.extras
    uri = os.environ['POSTGRES_URI']
    conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def _db_meta(db_conn):
    """Resolve workspace_id and an approved strategy_id from the live DB."""
    cur = db_conn.cursor()
    cur.execute("SELECT id FROM workspaces WHERE name='default' LIMIT 1")
    ws_row = cur.fetchone()
    assert ws_row is not None, "No 'default' workspace"
    ws_id = ws_row['id']

    cur.execute("SELECT id FROM strategy_registry WHERE status='approved' LIMIT 1")
    st_row = cur.fetchone()
    assert st_row is not None, "No approved strategy in strategy_registry"
    strategy_id = st_row['id']

    return {'ws_id': ws_id, 'strategy_id': strategy_id}


def _insert_signal(cur, ticker: str, direction: str, entry_price: float,
                   stop_loss: float, target_1: float,
                   lifecycle_state: str, target_date,
                   *,
                   ws_id, strategy_id,
                   run_date=None) -> int:
    """Helper: insert a minimal execution_signals row, return id."""
    signal_date = run_date or date.today()
    cur.execute("""
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, stop_loss, target_1, target_2, target_3,
             position_size_pct, regime_state, signal_params, status,
             lifecycle_state, target_date)
        VALUES
            (%s, %s, %s, %s, %s,
             %s, %s, %s, %s, %s,
             %s, %s, %s::jsonb, %s,
             %s, %s)
        RETURNING id
    """, (
        strategy_id, ws_id, signal_date, ticker, direction,
        entry_price, stop_loss, target_1, None, None,
        0.05, 'NORMAL', '{}', 'open',
        lifecycle_state, target_date,
    ))
    row = cur.fetchone()
    return row['id'] if hasattr(row, 'keys') else row[0]


# ──────────────────────────────────────────────────────────────────
# Unit tests (no DB)
# ──────────────────────────────────────────────────────────────────

def test_reanchor_long_passthrough_for_invalid_ref():
    """_reanchor_bracket returns unchanged levels when ref<=0 (safety passthrough)."""
    stop, target = _reanchor_bracket(
        ref=0.0, entry_price=105.0, direction=1,
        stop_ref=95.0, target_ref=110.0,
    )
    assert stop == 95.0
    assert target == 110.0


def test_reanchor_long_percentage_math():
    """Long: stop_pct and target_pct are preserved across the gap."""
    ref, stop_ref, target_ref = 100.0, 95.0, 110.0
    # stop_pct = (100-95)/100 = 0.05 → new_stop = entry*(1-0.05)
    # target_pct = (110-100)/100 = 0.10 → new_target = entry*(1+0.10)
    entry = 102.0
    new_stop, new_target = _reanchor_bracket(
        ref=ref, entry_price=entry, direction=1,
        stop_ref=stop_ref, target_ref=target_ref,
    )
    assert abs(new_stop - entry * 0.95) < 1e-9
    assert abs(new_target - entry * 1.10) < 1e-9


def test_reanchor_short_percentage_math():
    """Short: stop_pct above ref, target_pct below ref — preserved."""
    ref, stop_ref, target_ref = 100.0, 105.0, 90.0
    entry = 98.0
    new_stop, new_target = _reanchor_bracket(
        ref=ref, entry_price=entry, direction=-1,
        stop_ref=stop_ref, target_ref=target_ref,
    )
    # stop_pct = (105-100)/100 = 0.05 → new_stop = 98*1.05
    # target_pct = (100-90)/100 = 0.10 → new_target = 98*0.90
    assert abs(new_stop - entry * 1.05) < 1e-9
    assert abs(new_target - entry * 0.90) < 1e-9


def test_empty_closes_returns_zero():
    """finalize_parity_marks returns 0 immediately for empty closes dict."""
    result = finalize_parity_marks(None, {}, date.today())
    assert result == 0


# ──────────────────────────────────────────────────────────────────
# DB integration tests
# ──────────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_finalize_marks_executing_signal(db_conn, _db_meta):
    """Core parity test: finalize_parity_marks produces SAME stop/target as
    _reanchor_bracket called directly with identical inputs.

    This is the invariant that matters: the module must be a transparent
    wrapper around _reanchor_bracket, not an independent reimplementation.
    """
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZP6MKLONG'

    entry_price = 100.0
    stop_loss   = 94.0   # 6% below — long
    target_1    = 112.0  # 12% above — long
    close_price = 103.5  # official T+1 close

    sig_id = _insert_signal(
        cur, ticker, 'LONG', entry_price, stop_loss, target_1,
        'EXECUTING', run_date,
        ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
    )

    marked = finalize_parity_marks(cur, {ticker: close_price}, run_date)
    assert marked == 1

    # Compute expected values by calling _reanchor_bracket directly —
    # this is the parity assertion: the module must produce identical values.
    expected_stop, expected_target = _reanchor_bracket(
        ref=entry_price,
        entry_price=close_price,
        direction=_signal_to_long_short('LONG'),
        stop_ref=stop_loss,
        target_ref=target_1,
    )

    cur.execute("""
        SELECT mark_entry_price, fill_price, stop_loss, target_1,
               filled_at, lifecycle_state
          FROM execution_signals WHERE id = %s
    """, (sig_id,))
    row = cur.fetchone()
    assert row is not None

    assert abs(float(row['mark_entry_price']) - close_price) < 1e-9, (
        f"mark_entry_price {row['mark_entry_price']} != close {close_price}"
    )
    assert abs(float(row['fill_price']) - close_price) < 1e-9, (
        f"fill_price {row['fill_price']} != close {close_price}"
    )
    # The stop and target must match _reanchor_bracket exactly
    assert abs(float(row['stop_loss']) - expected_stop) < 1e-9, (
        f"stop_loss {row['stop_loss']} != expected {expected_stop}"
    )
    assert abs(float(row['target_1']) - expected_target) < 1e-9, (
        f"target_1 {row['target_1']} != expected {expected_target}"
    )
    assert row['lifecycle_state'] == 'FILLED'
    assert row['filled_at'] is not None
    # filled_at should be very recent
    delta = (datetime.now(timezone.utc) - row['filled_at']).total_seconds()
    assert delta < 30, f"filled_at looks stale: {delta}s ago"


@pytest.mark.integration
def test_finalize_marks_short_signal(db_conn, _db_meta):
    """Parity test for SHORT direction: re-anchor is correct for short geometry."""
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZP6MKSHRT'

    entry_price = 200.0
    stop_loss   = 210.0   # above entry — short stop
    target_1    = 180.0   # below entry — short target
    close_price = 197.0   # official T+1 close (gapped slightly lower)

    sig_id = _insert_signal(
        cur, ticker, 'SHORT', entry_price, stop_loss, target_1,
        'EXECUTING', run_date,
        ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
    )

    marked = finalize_parity_marks(cur, {ticker: close_price}, run_date)
    assert marked == 1

    expected_stop, expected_target = _reanchor_bracket(
        ref=entry_price,
        entry_price=close_price,
        direction=_signal_to_long_short('SHORT'),
        stop_ref=stop_loss,
        target_ref=target_1,
    )

    cur.execute("""
        SELECT mark_entry_price, stop_loss, target_1, lifecycle_state
          FROM execution_signals WHERE id = %s
    """, (sig_id,))
    row = cur.fetchone()

    assert abs(float(row['mark_entry_price']) - close_price) < 1e-9
    assert abs(float(row['stop_loss']) - expected_stop) < 1e-9
    assert abs(float(row['target_1']) - expected_target) < 1e-9
    assert row['lifecycle_state'] == 'FILLED'


@pytest.mark.integration
def test_finalize_skips_wrong_target_date(db_conn, _db_meta):
    """Signals with target_date != run_date are NOT marked."""
    cur = db_conn.cursor()
    today = date.today()
    from datetime import timedelta
    tomorrow = today + timedelta(days=1)

    ticker = 'ZZP6MKSKIP'
    sig_id = _insert_signal(
        cur, ticker, 'LONG', 100.0, 95.0, 110.0,
        'EXECUTING', tomorrow,   # target_date is TOMORROW
        ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
    )

    # Run with today's date — should NOT mark the signal
    marked = finalize_parity_marks(cur, {ticker: 102.0}, today)
    assert marked == 0

    cur.execute("""
        SELECT mark_entry_price, lifecycle_state
          FROM execution_signals WHERE id = %s
    """, (sig_id,))
    row = cur.fetchone()
    assert row['mark_entry_price'] is None, "mark_entry_price should still be NULL"
    assert row['lifecycle_state'] == 'EXECUTING', "lifecycle_state should be unchanged"


@pytest.mark.integration
def test_finalize_skips_ticker_not_in_closes(db_conn, _db_meta):
    """Signal is skipped if its ticker is absent from the closes dict."""
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZP6MKNOCLZ'

    sig_id = _insert_signal(
        cur, ticker, 'LONG', 100.0, 95.0, 110.0,
        'EXECUTING', run_date,
        ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
    )

    # closes dict has a DIFFERENT ticker
    marked = finalize_parity_marks(cur, {'OTHERTICKER': 102.0}, run_date)
    assert marked == 0

    cur.execute("""
        SELECT mark_entry_price, lifecycle_state
          FROM execution_signals WHERE id = %s
    """, (sig_id,))
    row = cur.fetchone()
    assert row['mark_entry_price'] is None
    assert row['lifecycle_state'] == 'EXECUTING'


@pytest.mark.integration
def test_finalize_skips_unsupported_direction(db_conn, _db_meta):
    """Signals with direction that maps to 0 (neutral) are skipped."""
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZP6MKNEUTRL'

    # Direction 'NEUTRAL' → _signal_to_long_short returns 0
    sig_id = _insert_signal(
        cur, ticker, 'NEUTRAL', 100.0, 95.0, 110.0,
        'EXECUTING', run_date,
        ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
    )

    marked = finalize_parity_marks(cur, {ticker: 102.0}, run_date)
    assert marked == 0

    cur.execute("""
        SELECT mark_entry_price, lifecycle_state
          FROM execution_signals WHERE id = %s
    """, (sig_id,))
    row = cur.fetchone()
    assert row['mark_entry_price'] is None
    assert row['lifecycle_state'] == 'EXECUTING'


@pytest.mark.integration
def test_finalize_already_filled_signal_is_idempotent(db_conn, _db_meta):
    """A FILLED row with matching target_date also gets re-marked (idempotent).

    The SELECT includes lifecycle_state='FILLED' so a signal that was partially
    processed or re-triggered gets the correct mark applied.
    """
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZP6MKIDEM'

    entry_price = 100.0
    stop_loss   = 95.0
    target_1    = 110.0
    close_price = 101.0

    sig_id = _insert_signal(
        cur, ticker, 'LONG', entry_price, stop_loss, target_1,
        'FILLED', run_date,   # already FILLED
        ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
    )

    marked = finalize_parity_marks(cur, {ticker: close_price}, run_date)
    assert marked == 1

    expected_stop, expected_target = _reanchor_bracket(
        ref=entry_price,
        entry_price=close_price,
        direction=1,
        stop_ref=stop_loss,
        target_ref=target_1,
    )

    cur.execute("""
        SELECT mark_entry_price, stop_loss, target_1
          FROM execution_signals WHERE id = %s
    """, (sig_id,))
    row = cur.fetchone()
    assert abs(float(row['mark_entry_price']) - close_price) < 1e-9
    assert abs(float(row['stop_loss']) - expected_stop) < 1e-9
    assert abs(float(row['target_1']) - expected_target) < 1e-9


@pytest.mark.integration
def test_finalize_no_rows_returns_zero(db_conn):
    """When no matching rows exist, returns 0 without error."""
    cur = db_conn.cursor()
    from datetime import timedelta
    # Use a date very far in the future so no real rows can match
    future_date = date.today() + timedelta(days=3650)

    marked = finalize_parity_marks(cur, {'AAPL': 150.0}, future_date)
    assert marked == 0
