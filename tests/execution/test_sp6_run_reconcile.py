"""tests/test_sp6_run_reconcile.py — SP-6 Task 8b: open_reconcile.run_reconcile.

The 9:30 ET open-window reconcile (Option B time-split, §12 of the design doc).
A pure CLASSIFY → drops/flatten close-at-open. NO sizing, NO opens.

Invariants verified (ae.execute_single is ALWAYS mocked — no real orders):
  (1) Gate OFF → no-op early return; ae.execute_single never called.
  (2) orphan_close (dropped: held, not APPROVED) → is_dropped close_only order built,
      ae.execute_single called, drop_signal_close invoked on confirmed fill
      (signal_pnl status='closed', close_reason='signal_dropped').
  (3) flip_close (APPROVED opposite-sign to broker) → old leg closed at the open;
      flip_open is NOT submitted by run_reconcile.
  (4) resize-down (held, APPROVED same direction → classifier emits delta<0) → NOT
      closed at the open (ignored — 3:55 sizer's job). And an OPEN (in APPROVED, not
      in broker → flip_open / delta>0) → NOT submitted at 9:30.
  (5) FLATTEN: zero APPROVED + non-empty broker + healthy + gate-ran → all positions
      closed (flatten_signal_close, close_reason='flattened').
  (6) FLATTEN guard — UNHEALTHY → NO flatten (positions preserved, zero submissions).
  (7) FLATTEN guard — GATE-NOT-RAN → NO flatten (the dangerous case: crashed gate ⇒
      zero APPROVED ⇒ would look like total drop). NOT promote. Loud warning.
      ae.execute_single called ZERO times.
  (8) dry_run=True → ae.execute_single never called, no ledger writes.

Run:
    source ./worktree-env.sh
    python3 -m pytest tests/test_sp6_run_reconcile.py -v

Requirements:
    - POSTGRES_URI set (worktree-env.sh loads it)
    - Migration 126 applied (lifecycle_state, mark_entry_price, fill_price,
      filled_at, target_date columns on execution_signals)
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import open_reconcile  # noqa: E402
from execution.open_reconcile import run_reconcile  # noqa: E402

pytestmark = pytest.mark.integration

DSN = os.environ.get("POSTGRES_URI")

# A unique trading day per run so APPROVED/health/gate rows for these tests can
# never collide with real production rows for today.
TEST_DAY = date(2021, 3, 17)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """Auto-rollback connection. Yields the CONNECTION (run_reconcile takes conn=
    and opens its own cursor); a RealDictCursor is the factory so helper rows
    read by name."""
    assert DSN, "POSTGRES_URI required"
    conn = psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def _db_meta(db_conn):
    """Resolve a real workspace_id + approved strategy_id for FK-valid inserts."""
    cur = db_conn.cursor()
    cur.execute("SELECT id FROM workspaces WHERE name='default' LIMIT 1")
    ws_row = cur.fetchone()
    assert ws_row is not None, "No 'default' workspace"
    ws_id = ws_row['id']

    cur.execute("SELECT id FROM strategy_registry WHERE status='approved' LIMIT 1")
    st_row = cur.fetchone()
    assert st_row is not None, "No approved strategy in strategy_registry"
    strategy_id = st_row['id']
    cur.close()
    return {'ws_id': ws_id, 'strategy_id': strategy_id}


@pytest.fixture(autouse=True)
def _gate_on():
    """OPENCLAW_EOD_RECONCILE=1 for every test except the gate-off test (which
    deletes it). conftest restores os.environ after each test."""
    os.environ['OPENCLAW_EOD_RECONCILE'] = '1'
    yield


def _insert_signal(
    conn, *, ws_id, strategy_id, ticker, direction, lifecycle_state,
    target_date, signal_date=date(2021, 3, 16),
    entry_price=100.0, mark_entry_price=100.0, status='open',
    stop_loss=95.0, target_1=115.0,
) -> str:
    """Insert one execution_signals row; return its id."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, mark_entry_price, target_date, lifecycle_state,
             stop_loss, target_1, position_size_pct, regime_state,
             signal_params, status, computed_at)
        VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s::jsonb,%s,%s)
        RETURNING id
        """,
        (strategy_id, ws_id, signal_date, ticker, direction,
         entry_price, mark_entry_price, target_date, lifecycle_state,
         stop_loss, target_1, 0.05, 'NORMAL', '{}', status,
         datetime.now(timezone.utc)),
    )
    sid = cur.fetchone()['id']
    cur.close()
    return sid


def _insert_health(conn, *, run_date, healthy):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO eod_compute_health
            (run_date, run_at, rc, n_strategies_ok, n_strategies_total,
             regime_ok, universe_size, healthy, detail)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        """,
        (run_date, datetime.now(timezone.utc), 0 if healthy else 1,
         5 if healthy else 0, 5, healthy, 100 if healthy else 0, healthy, '{}'),
    )
    cur.close()


def _insert_gate_sentinel(conn, *, target_date):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO signal_gate_verdicts
            (signal_id, gate_type, ticker, target_date, verdict,
             panic_score, news_count, severity, model, metadata, actor, decided_at)
        VALUES (NULL,'__gate_ran__','__gate__',%s,'ok',0.0,0,NULL,NULL,'{}'::jsonb,
                'test', %s)
        """,
        (target_date, datetime.now(timezone.utc)),
    )
    cur.close()


def _submitted(entry):
    """A mock ae.execute_single result that counts as a CONFIRMED close fill.

    SP-6 Task 10: execute_single's is_dropped close path polls to terminal and
    returns status='filled' (with the real fill price) ONLY on a confirmed fill;
    open_reconcile._resolve_fill_price now gates the ledger-close strictly on
    status=='filled' (the 8b ack≠fill landmine fix). So the "confirmed fill"
    mock must carry status='filled' — a bare 'submitted' ack no longer triggers a
    ledger-close. Helper name kept for minimal churn; intent unchanged."""
    return {'status': 'filled', 'entry': entry, 'order_id': 'ord-1',
            'qty': 10, 'notional': abs(entry) * 10, 'http': 200,
            'tif': 'day', 'order_class': 'simple', 'client_order_id': 'coid-1'}


def _pnl_row(conn, sig_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT status, close_reason, closed_price, realized_pnl_pct "
        "FROM signal_pnl WHERE signal_id=%s", (sig_id,))
    row = cur.fetchone()
    cur.close()
    return row


def _es_row(conn, sig_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT status, lifecycle_state, fill_price FROM execution_signals "
        "WHERE id=%s", (sig_id,))
    row = cur.fetchone()
    cur.close()
    return row


# ──────────────────────────────────────────────────────────────────────────────
# (1) Gate OFF → no-op
# ──────────────────────────────────────────────────────────────────────────────

def test_gate_off_is_noop():
    os.environ.pop('OPENCLAW_EOD_RECONCILE', None)
    with mock.patch('execution.alpaca_executor.execute_single') as m_exec:
        res = run_reconcile(broker_loader=lambda: {'AAPL': 1000.0})
    assert res == {'drops': 0, 'flattens': 0, 'holds': 0, 'errors': 0}
    m_exec.assert_not_called()


# ──────────────────────────────────────────────────────────────────────────────
# (2) orphan_close (dropped signal) → close at open + ledger-close
# ──────────────────────────────────────────────────────────────────────────────

def test_orphan_close_drops_and_ledger_closes(db_conn, _db_meta):
    # AAPL held (FILLED, in broker) but NOT in APPROVED set → orphan_close.
    # MSFT held + APPROVED (same dir) so the APPROVED set is NON-empty — this is a
    # partial DROP, not a flatten (otherwise zero-APPROVED would trigger the
    # flatten path + guard).
    sig_id = _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='FILLED',
        target_date=TEST_DAY, entry_price=100.0, mark_entry_price=100.0)
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='MSFT', direction='LONG', lifecycle_state='APPROVED',
        target_date=TEST_DAY, status='open')

    with mock.patch('execution.alpaca_executor.execute_single',
                    return_value=_submitted(101.0)) as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0, 'MSFT': 3000.0},
            gate_ran=True,
            run_date=TEST_DAY,
        )

    # One close submitted — only the dropped AAPL (MSFT is held+APPROVED → delta,
    # ignored at the open). MSFT close_only is_dropped order.
    assert m_exec.call_count == 1, f"expected 1 execute_single call, got {m_exec.call_count}"
    order = m_exec.call_args.args[2]
    assert order['close_only'] is True
    assert order['is_dropped'] is True
    assert order['ticker'] == 'AAPL'
    assert order['side'] == 'sell'           # closing a LONG
    assert order['strategy_id'] == '__close_orphan__'
    assert order['current_usd'] == 5000.0

    assert res['drops'] == 1
    assert res['flattens'] == 0

    pnl = _pnl_row(db_conn, sig_id)
    assert pnl is not None and pnl['status'] == 'closed'
    assert pnl['close_reason'] == 'signal_dropped'
    assert abs(float(pnl['closed_price']) - 101.0) < 1e-6
    es = _es_row(db_conn, sig_id)
    assert es['lifecycle_state'] == 'CLOSED_AT_OPEN'


# ──────────────────────────────────────────────────────────────────────────────
# (3) flip_close → old leg closed at open; flip_open NOT submitted by run_reconcile
# ──────────────────────────────────────────────────────────────────────────────

def test_flip_close_closes_old_leg_only(db_conn, _db_meta):
    # Broker LONG AAPL; APPROVED says SHORT AAPL → flip. run_reconcile closes the
    # OLD (long) leg only. The new short open is the 3:55 sizer's job.
    held = _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='FILLED',
        target_date=date(2021, 3, 10))  # the held leg is an older FILLED row
    # APPROVED carried short signal for today (the new direction):
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='SHORT', lifecycle_state='APPROVED',
        target_date=TEST_DAY, status='open')

    with mock.patch('execution.alpaca_executor.execute_single',
                    return_value=_submitted(99.0)) as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0},  # broker LONG
            gate_ran=True,
            run_date=TEST_DAY,
        )

    # Exactly ONE submit — the flip_close. flip_open never submitted here.
    assert m_exec.call_count == 1
    order = m_exec.call_args.args[2]
    assert order['close_only'] is True
    assert order['strategy_id'] == '__flip_close__'
    assert order['side'] == 'sell'  # closing the long leg
    assert res['drops'] == 1

    # The held long leg got ledger-closed.
    assert _pnl_row(db_conn, held)['status'] == 'closed'


# ──────────────────────────────────────────────────────────────────────────────
# (4) resize-down (delta<0) NOT closed; OPEN (delta>0/flip_open) NOT submitted
# ──────────────────────────────────────────────────────────────────────────────

def test_resize_down_and_open_not_submitted_at_open(db_conn, _db_meta):
    # AAPL: held LONG + APPROVED LONG (same direction). Sign-only target=+1 vs
    # broker +5000 → classifier emits delta<0 (a resize-down) → MUST be ignored.
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='APPROVED',
        target_date=TEST_DAY, status='open')
    # NVDA: APPROVED LONG but NOT in broker → an OPEN (delta>0) → MUST be ignored.
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='NVDA', direction='LONG', lifecycle_state='APPROVED',
        target_date=TEST_DAY, status='open')

    with mock.patch('execution.alpaca_executor.execute_single') as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0},  # only AAPL held
            gate_ran=True,
            run_date=TEST_DAY,
        )

    # Neither the resize-down nor the open is submitted at 9:30.
    m_exec.assert_not_called()
    assert res['drops'] == 0
    assert res['flattens'] == 0
    assert res['holds'] == 1  # AAPL held, untouched


# ──────────────────────────────────────────────────────────────────────────────
# (5) FLATTEN — zero APPROVED + non-empty broker + healthy + gate-ran
# ──────────────────────────────────────────────────────────────────────────────

def test_flatten_when_healthy_and_gate_ran(db_conn, _db_meta):
    # Two held positions, ZERO APPROVED for today.
    s1 = _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='FILLED',
        target_date=TEST_DAY)
    s2 = _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='MSFT', direction='LONG', lifecycle_state='FILLED',
        target_date=TEST_DAY)
    _insert_health(db_conn, run_date=TEST_DAY, healthy=True)
    _insert_gate_sentinel(db_conn, target_date=TEST_DAY)

    with mock.patch('execution.alpaca_executor.execute_single',
                    return_value=_submitted(100.0)) as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0, 'MSFT': 3000.0},
            run_date=TEST_DAY,
            # gate_ran omitted → resolves via the sentinel we inserted.
        )

    assert m_exec.call_count == 2
    assert res['flattens'] == 2
    assert res['drops'] == 0
    # Both ledger-closed with reason='flattened'.
    assert _pnl_row(db_conn, s1)['close_reason'] == 'flattened'
    assert _pnl_row(db_conn, s2)['close_reason'] == 'flattened'


# ──────────────────────────────────────────────────────────────────────────────
# (6) FLATTEN guard — UNHEALTHY → no flatten, positions preserved
# ──────────────────────────────────────────────────────────────────────────────

def test_no_flatten_when_unhealthy(db_conn, _db_meta):
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='FILLED',
        target_date=TEST_DAY)
    _insert_health(db_conn, run_date=TEST_DAY, healthy=False)   # RED
    _insert_gate_sentinel(db_conn, target_date=TEST_DAY)         # gate did run

    with mock.patch('execution.alpaca_executor.execute_single') as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0},
            run_date=TEST_DAY,
        )

    m_exec.assert_not_called()           # NEVER entered the close loop
    assert res['flattens'] == 0
    assert res['drops'] == 0
    assert res['holds'] == 1             # preserved


def test_no_flatten_when_health_row_missing(db_conn, _db_meta):
    # No eod_compute_health row at all for TEST_DAY → treated as unhealthy.
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='FILLED',
        target_date=TEST_DAY)
    _insert_gate_sentinel(db_conn, target_date=TEST_DAY)

    with mock.patch('execution.alpaca_executor.execute_single') as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0},
            run_date=TEST_DAY,
        )
    m_exec.assert_not_called()
    assert res['holds'] == 1


# ──────────────────────────────────────────────────────────────────────────────
# (7) FLATTEN guard — GATE-NOT-RAN (the dangerous case) → NO flatten, NO promote
# ──────────────────────────────────────────────────────────────────────────────

def test_no_flatten_when_gate_not_ran(db_conn, _db_meta):
    # Healthy compute, ZERO APPROVED, but the gate-ran sentinel is ABSENT — this
    # is a crashed-gate scenario, NOT a legitimately-flat day. MUST NOT flatten.
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='FILLED',
        target_date=TEST_DAY)
    _insert_health(db_conn, run_date=TEST_DAY, healthy=True)
    # NO gate sentinel inserted.

    with mock.patch('execution.alpaca_executor.execute_single') as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0},
            run_date=TEST_DAY,
            # gate_ran omitted → resolves via _gate_ran_today → False (no row).
        )

    # CRITICAL: zero submissions — the close loop was never entered.
    m_exec.assert_not_called()
    assert res['flattens'] == 0
    assert res['drops'] == 0
    assert res['holds'] == 1             # preserved

    # NOT promoted: the APPROVED set must remain empty (no COMPUTED→APPROVED here).
    cur = db_conn.cursor()
    cur.execute(
        "SELECT count(*) AS n FROM execution_signals "
        "WHERE lifecycle_state='APPROVED' AND target_date=%s", (TEST_DAY,))
    assert cur.fetchone()['n'] == 0
    cur.close()


# ──────────────────────────────────────────────────────────────────────────────
# (8) dry_run=True → no submissions, no ledger writes
# ──────────────────────────────────────────────────────────────────────────────

def test_dry_run_no_submit_no_ledger(db_conn, _db_meta):
    # Partial-drop scenario (non-empty APPROVED set so it is NOT a flatten):
    # AAPL dropped, MSFT held+APPROVED.
    sig_id = _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='AAPL', direction='LONG', lifecycle_state='FILLED',
        target_date=TEST_DAY)
    _insert_signal(
        db_conn, ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'],
        ticker='MSFT', direction='LONG', lifecycle_state='APPROVED',
        target_date=TEST_DAY, status='open')

    with mock.patch('execution.alpaca_executor.execute_single') as m_exec:
        res = run_reconcile(
            conn=db_conn,
            broker_loader=lambda: {'AAPL': 5000.0, 'MSFT': 3000.0},
            gate_ran=True,
            run_date=TEST_DAY,
            dry_run=True,
        )

    m_exec.assert_not_called()
    # The planned drop is counted but nothing is submitted/written.
    assert res['drops'] == 1
    assert _pnl_row(db_conn, sig_id) is None      # no ledger write
    assert _es_row(db_conn, sig_id)['lifecycle_state'] == 'FILLED'  # unchanged
