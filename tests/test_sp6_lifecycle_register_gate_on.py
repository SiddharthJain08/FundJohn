"""Test: engine.write_signals lifecycle_state register (gate ON/OFF, SP-6 Task 2).

Key invariants under test:
  1. Gate ON  → new signal row has lifecycle_state='COMPUTED', target_date set
               to the next trading session (calendar-aware), computed_at recent.
  2. Gate OFF → lifecycle_state / target_date / computed_at are NULL (legacy
               INSERT byte-identical to pre-SP-6 behavior).
  3. SAVEPOINT path preserved: bad geometry rolls back, good signal survives.
  4. _next_trading_day is holiday-aware: 2026-07-02 (Thursday before observed
     July 4) → 2026-07-06 (Monday), NOT 2026-07-03.

All tests use rollback isolation — no persistent side-effects on the live DB.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from datetime import date, datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest
import psycopg2
import psycopg2.extras

from execution.engine import write_signals, _next_trading_day

pytestmark = pytest.mark.integration


# ─────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """Auto-rollback psycopg2 connection to the live DB."""
    uri = os.environ['POSTGRES_URI']
    conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def workspace_id(db_conn):
    """Resolve the 'default' workspace UUID."""
    cur = db_conn.cursor()
    cur.execute("SELECT id FROM workspaces WHERE name='default' LIMIT 1")
    row = cur.fetchone()
    assert row is not None, "no 'default' workspace found"
    return str(row['id'])


@pytest.fixture
def approved_strategy_id(db_conn):
    """Return the id of one approved strategy from strategy_registry."""
    cur = db_conn.cursor()
    cur.execute("SELECT id FROM strategy_registry WHERE status='approved' LIMIT 1")
    row = cur.fetchone()
    assert row is not None, "no approved strategy in strategy_registry"
    return row['id']


# ─────────────────────────────────────────────────────────
# _next_trading_day unit tests
# ─────────────────────────────────────────────────────────

def test_next_trading_day_weekday():
    """Tuesday → Wednesday (plain weekday case)."""
    assert _next_trading_day(date(2026, 5, 27)) == date(2026, 5, 28)


def test_next_trading_day_friday_to_monday():
    """Friday → Monday (skip weekend)."""
    assert _next_trading_day(date(2026, 5, 29)) == date(2026, 6, 1)


def test_next_trading_day_skip_weekend_saturday():
    """Saturday → Monday (input is on a weekend)."""
    assert _next_trading_day(date(2026, 5, 30)) == date(2026, 6, 1)


def test_next_trading_day_skip_weekend_sunday():
    """Sunday → Monday."""
    assert _next_trading_day(date(2026, 5, 31)) == date(2026, 6, 1)


def test_next_trading_day_holiday_aware():
    """2026-07-02 (Thu before observed July 4) → 2026-07-06, NOT 2026-07-03.

    This is the discriminating test: weekday-only math would return 2026-07-03
    (Friday), but the Alpaca calendar knows the market is closed and returns
    2026-07-06 (Monday).  This test FAILS if the implementation degrades to
    pure weekday-skip fallback.
    """
    result = _next_trading_day(date(2026, 7, 2))
    assert result == date(2026, 7, 6), (
        f"Expected 2026-07-06 (next open session after observed July 4 holiday), "
        f"got {result}. If weekday-only math is being used, the clock/calendar "
        f"fallback may have triggered."
    )


# ─────────────────────────────────────────────────────────
# DB integration: gate ON
# ─────────────────────────────────────────────────────────

def _make_signal(ticker: str, direction: str = 'LONG',
                 entry: float = 100.0, stop: float = 98.0,
                 t1: float = 104.0, t2: float = 106.0, t3: float = 108.0):
    """Build a minimal Signal object for testing."""
    from strategies.base import Signal
    return Signal(
        ticker=ticker,
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        target_1=t1,
        target_2=t2,
        target_3=t3,
        position_size_pct=0.05,
        confidence='HIGH',
        signal_params={'source': 'test'},
    )


def test_write_signals_gate_on_sets_lifecycle(
    db_conn, monkeypatch, workspace_id, approved_strategy_id
):
    """Gate ON: new signal row has lifecycle_state='COMPUTED' and target_date set."""
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setenv('WORKSPACE_ID', workspace_id)

    # Reload the module-level WORKSPACE binding to pick up the UUID
    import execution.engine as _eng
    monkeypatch.setattr(_eng, 'WORKSPACE', workspace_id)

    cur = db_conn.cursor()
    run_date = date(2026, 5, 27)  # Tuesday → next session Wednesday

    # Use a sentinel ticker that cannot collide with any live open position.
    strategy_results = {approved_strategy_id: [_make_signal('ZZSP6A')]}
    n_inserted = write_signals(cur, strategy_results, 'LOW_VOL', run_date)
    assert n_inserted == 1

    cur.execute("""
        SELECT lifecycle_state, target_date, computed_at
          FROM execution_signals
         WHERE strategy_id = %s AND ticker = %s AND status = 'open'
         ORDER BY created_at DESC LIMIT 1
    """, (approved_strategy_id, 'ZZSP6A'))
    row = cur.fetchone()

    assert row is not None, "Signal row not found after write_signals"
    assert row['lifecycle_state'] == 'COMPUTED'
    assert row['target_date'] == date(2026, 5, 28)
    assert row['computed_at'] is not None
    delta = (datetime.now(timezone.utc) - row['computed_at']).total_seconds()
    assert delta < 10, f"computed_at looks stale: {delta}s ago"


def test_write_signals_gate_off_leaves_null(
    db_conn, monkeypatch, workspace_id, approved_strategy_id
):
    """Gate OFF: lifecycle_state / target_date / computed_at are NULL (legacy behavior)."""
    monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)

    import execution.engine as _eng
    monkeypatch.setattr(_eng, 'WORKSPACE', workspace_id)

    cur = db_conn.cursor()
    run_date = date(2026, 5, 28)  # Wednesday

    # Use a sentinel ticker that cannot collide with any live open position.
    strategy_results = {approved_strategy_id: [_make_signal('ZZSP6B')]}
    n_inserted = write_signals(cur, strategy_results, 'LOW_VOL', run_date)
    assert n_inserted == 1

    cur.execute("""
        SELECT lifecycle_state, target_date, computed_at
          FROM execution_signals
         WHERE strategy_id = %s AND ticker = %s AND status = 'open'
         ORDER BY created_at DESC LIMIT 1
    """, (approved_strategy_id, 'ZZSP6B'))
    row = cur.fetchone()

    assert row is not None, "Signal row not found after write_signals"
    assert row['lifecycle_state'] is None, "lifecycle_state must be NULL when gate OFF"
    assert row['target_date'] is None, "target_date must be NULL when gate OFF"
    assert row['computed_at'] is None, "computed_at must be NULL when gate OFF"


def test_write_signals_gate_on_savepoint_preserved(
    db_conn, monkeypatch, workspace_id, approved_strategy_id
):
    """SAVEPOINT path preserved: degenerate-geometry signal is skipped, good signal persists."""
    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')

    import execution.engine as _eng
    monkeypatch.setattr(_eng, 'WORKSPACE', workspace_id)

    cur = db_conn.cursor()
    run_date = date(2026, 5, 27)

    # Use sentinel tickers that cannot collide with any live open positions.
    good_signal = _make_signal('ZZSP6C')
    # Degenerate geometry: stop == entry for LONG → will be dropped pre-SAVEPOINT
    bad_signal = _make_signal('ZZSP6D', entry=100.0, stop=100.0, t1=104.0)

    strategy_results = {approved_strategy_id: [good_signal, bad_signal]}
    n_inserted = write_signals(cur, strategy_results, 'LOW_VOL', run_date)
    assert n_inserted == 1, "Only the good signal should be inserted"

    # Good signal has lifecycle fields
    cur.execute("""
        SELECT lifecycle_state, target_date
          FROM execution_signals
         WHERE strategy_id = %s AND ticker = %s AND status = 'open'
         ORDER BY created_at DESC LIMIT 1
    """, (approved_strategy_id, 'ZZSP6C'))
    row = cur.fetchone()
    assert row is not None
    assert row['lifecycle_state'] == 'COMPUTED'
    assert row['target_date'] is not None

    # Bad signal was not inserted
    cur.execute("""
        SELECT id FROM execution_signals
         WHERE strategy_id = %s AND ticker = %s
    """, (approved_strategy_id, 'ZZSP6D'))
    assert cur.fetchone() is None, "Degenerate-geometry signal (ZZSP6D) must not be inserted"
