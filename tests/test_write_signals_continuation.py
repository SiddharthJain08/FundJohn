"""test_write_signals_continuation.py — SP-6 Phase C Task 1.

The §13 durable fix for the structural 1-day max-hold: filled execution_signals
rows keep status='open' until pnl closes them; the upsert in
engine.write_signals used to match (strategy_id, ticker, status='open') and
bracket-refresh the SPENT row (target_date already consumed) — so re-emissions
never minted a row for the next target_date, locking held tickers out of
subsequent target sets.

This suite proves:
  1. Spent open row + gate ON + re-emission ⇒ a NEW COMPUTED row is minted
     (target_date=_next_td, signal_date=run_date), the old row untouched.
  2. UNSPENT open row (target_date == _next_td, same-evening / intraday re-run)
     ⇒ bracket-refresh only, NO duplicate mint.
  3. Gate OFF ⇒ byte-identical legacy upsert (no mint; refresh exactly as before).
  4. No unique-key violation across consecutive-day mints (3 days).
  5. D1: a parity-mark fill of a continuation row CLOSES the spent sibling
     (execution_signals.status='closed'; signal_pnl.close_reason='rolled_continuation',
     closed_at set); a non-continuation fill changes nothing; an unfilled
     continuation leaves the sibling open.

All DB tests use rollback isolation — no persistent side-effects.  Gate-ON tests
monkeypatch engine._next_trading_day so no live Alpaca subprocess is spawned.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2
import psycopg2.extras

from execution import engine
from execution import open_reconcile
from execution.parity_mark import finalize_parity_marks


pytestmark = pytest.mark.integration


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_conn():
    """Auto-rollback psycopg2 connection to the live DB (DictCursor)."""
    uri = os.environ.get(
        'POSTGRES_URI', 'postgresql://openclaw:password@localhost:5432/openclaw'
    )
    conn = psycopg2.connect(uri, cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def _db_meta(db_conn):
    """Resolve the 'default' workspace_id and an approved strategy_id."""
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


class _Sig:
    """Minimal stand-in for strategies.base.Signal (the fields write_signals reads)."""

    def __init__(self, ticker, direction='LONG',
                 entry_price=100.0, stop_loss=95.0, target_1=110.0,
                 target_2=None, target_3=None, position_size_pct=0.05,
                 signal_params=None):
        self.ticker = ticker
        self.direction = direction
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.target_1 = target_1
        self.target_2 = target_2
        self.target_3 = target_3
        self.position_size_pct = position_size_pct
        self.signal_params = signal_params or {}
        self.features = None
        self.option_spec = None


def _open_rows(cur, strategy_id, ticker, ws_id):
    """Return all execution_signals rows for (strategy, ticker, workspace),
    newest target_date first."""
    cur.execute("""
        SELECT id, signal_date, target_date, status, lifecycle_state,
               entry_price, stop_loss, target_1
          FROM execution_signals
         WHERE strategy_id = %s AND ticker = %s AND workspace_id = %s
         ORDER BY target_date DESC NULLS LAST, signal_date DESC
    """, (strategy_id, ticker, ws_id))
    return cur.fetchall()


def _set_engine_workspace(monkeypatch, ws_id):
    """write_signals writes WORKSPACE (engine module constant) into workspace_id.
    Point it at the resolved 'default' workspace UUID so FK + scoping line up."""
    monkeypatch.setattr(engine, 'WORKSPACE', str(ws_id), raising=False)


# ──────────────────────────────────────────────────────────────────
# Test 1 — spent open row + gate ON ⇒ NEW row minted, old row untouched
# ──────────────────────────────────────────────────────────────────

def test_spent_open_row_mints_continuation(db_conn, _db_meta, monkeypatch):
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC1MINT'

    run_date = date(2026, 6, 5)
    next_td = date(2026, 6, 8)          # the new target_date
    spent_td = date(2026, 6, 4)         # the held row's spent target_date

    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setattr(engine, '_next_trading_day', lambda d: next_td)
    _set_engine_workspace(monkeypatch, ws_id)

    # Seed a held position: status='open', FILLED, target_date already spent.
    cur.execute("""
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, stop_loss, target_1, position_size_pct, regime_state,
             signal_params, status, lifecycle_state, target_date)
        VALUES (%s,%s,%s,%s,'LONG',100.0,95.0,110.0,0.05,'NORMAL',
                '{}'::jsonb,'open','FILLED',%s)
        RETURNING id, signal_date, entry_price
    """, (strat, ws_id, date(2026, 6, 3), ticker, spent_td))
    held = cur.fetchone()
    held_id = held['id']

    # Re-emission for the next target_date.
    sig = _Sig(ticker, stop_loss=96.0, target_1=112.0)  # current intent differs
    total = engine.write_signals(cur, {strat: [sig]}, 'NORMAL', run_date)

    rows = _open_rows(cur, strat, ticker, ws_id)
    assert len(rows) == 2, f"expected held + continuation, got {len(rows)}"
    assert total >= 1, "continuation mint should count as an emit"

    # Old held row UNTOUCHED (entry_price + signal_date frozen, still open).
    cur.execute("""
        SELECT signal_date, target_date, status, lifecycle_state,
               entry_price, stop_loss, target_1
          FROM execution_signals WHERE id = %s
    """, (held_id,))
    old = cur.fetchone()
    assert old['signal_date'] == date(2026, 6, 3), "held signal_date must be frozen"
    assert old['target_date'] == spent_td, "held target_date must be unchanged"
    assert old['status'] == 'open', "held row must stay open (pnl continuity)"
    assert float(old['entry_price']) == 100.0, "held entry_price must be frozen"
    # The spent row must NOT have been bracket-refreshed with the new intent.
    assert float(old['stop_loss']) == 95.0, "held stop must NOT be refreshed"
    assert float(old['target_1']) == 110.0, "held target must NOT be refreshed"

    # NEW continuation row: COMPUTED, target=next_td, signal_date=run_date.
    new_rows = [r for r in rows if r['id'] != held_id]
    assert len(new_rows) == 1
    new = new_rows[0]
    assert new['lifecycle_state'] == 'COMPUTED'
    assert new['target_date'] == next_td
    assert new['signal_date'] == run_date
    assert new['status'] == 'open'
    assert float(new['stop_loss']) == 96.0, "continuation carries current intent"
    assert float(new['target_1']) == 112.0


# ──────────────────────────────────────────────────────────────────
# Test 2 — UNSPENT open row (same-evening / intraday) ⇒ refresh, NO mint
# ──────────────────────────────────────────────────────────────────

def test_unspent_open_row_refreshes_no_mint(db_conn, _db_meta, monkeypatch):
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC2NOMINT'

    run_date = date(2026, 6, 5)
    next_td = date(2026, 6, 8)

    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    monkeypatch.setattr(engine, '_next_trading_day', lambda d: next_td)
    _set_engine_workspace(monkeypatch, ws_id)

    # Seed an UNSPENT row: target_date == next_td (this run's intent already there).
    cur.execute("""
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, stop_loss, target_1, position_size_pct, regime_state,
             signal_params, status, lifecycle_state, target_date)
        VALUES (%s,%s,%s,%s,'LONG',100.0,95.0,110.0,0.05,'NORMAL',
                '{}'::jsonb,'open','COMPUTED',%s)
        RETURNING id
    """, (strat, ws_id, run_date, ticker, next_td))
    existing_id = cur.fetchone()['id']

    sig = _Sig(ticker, stop_loss=97.0, target_1=113.0)
    engine.write_signals(cur, {strat: [sig]}, 'NORMAL', run_date)

    rows = _open_rows(cur, strat, ticker, ws_id)
    assert len(rows) == 1, f"unspent row must NOT mint a duplicate, got {len(rows)}"
    assert rows[0]['id'] == existing_id
    # Refresh applied the new bracket intent.
    assert float(rows[0]['stop_loss']) == 97.0
    assert float(rows[0]['target_1']) == 113.0


# ──────────────────────────────────────────────────────────────────
# Test 3 — Gate OFF ⇒ byte-identical legacy upsert (refresh, no mint)
# ──────────────────────────────────────────────────────────────────

def test_gate_off_legacy_upsert_refresh_only(db_conn, _db_meta, monkeypatch):
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC3GATEOFF'

    run_date = date(2026, 6, 5)

    monkeypatch.delenv('OPENCLAW_EOD_SIGNAL_REGISTER', raising=False)
    _set_engine_workspace(monkeypatch, ws_id)

    # _next_trading_day must NOT be invoked when gate is OFF — make it explode
    # if the legacy path ever calls it (proves no CLI subprocess is spawned).
    def _boom(d):
        raise AssertionError("_next_trading_day called on gate-OFF path")
    monkeypatch.setattr(engine, '_next_trading_day', _boom)

    # Seed a legacy open row that would look 'spent' IF the gate logic ran:
    # spent-looking target_date, but gate OFF means it's just refreshed.
    cur.execute("""
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, stop_loss, target_1, position_size_pct, regime_state,
             signal_params, status, target_date)
        VALUES (%s,%s,%s,%s,'LONG',100.0,95.0,110.0,0.05,'NORMAL',
                '{}'::jsonb,'open',%s)
        RETURNING id
    """, (strat, ws_id, date(2026, 6, 3), ticker, date(2026, 6, 4)))
    existing_id = cur.fetchone()['id']

    sig = _Sig(ticker, stop_loss=98.0, target_1=115.0)
    engine.write_signals(cur, {strat: [sig]}, 'NORMAL', run_date)

    rows = _open_rows(cur, strat, ticker, ws_id)
    assert len(rows) == 1, "gate-OFF must NOT mint a continuation row"
    assert rows[0]['id'] == existing_id, "gate-OFF must refresh the existing row"
    # Legacy refresh updates the bracket; lifecycle_state stays NULL (legacy shape).
    assert float(rows[0]['stop_loss']) == 98.0
    assert float(rows[0]['target_1']) == 115.0
    assert rows[0]['lifecycle_state'] is None, "legacy row keeps NULL lifecycle_state"


# ──────────────────────────────────────────────────────────────────
# Test 4 — no unique-key violation across consecutive-day mints (3 days)
# ──────────────────────────────────────────────────────────────────

def test_consecutive_day_mints_no_unique_violation(db_conn, _db_meta, monkeypatch):
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC4MULTI'

    monkeypatch.setenv('OPENCLAW_EOD_SIGNAL_REGISTER', '1')
    _set_engine_workspace(monkeypatch, ws_id)

    # Day 0: seed an initial held FILLED row (spent on day 1's run).
    cur.execute("""
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, stop_loss, target_1, position_size_pct, regime_state,
             signal_params, status, lifecycle_state, target_date)
        VALUES (%s,%s,%s,%s,'LONG',100.0,95.0,110.0,0.05,'NORMAL',
                '{}'::jsonb,'open','FILLED',%s)
    """, (strat, ws_id, date(2026, 6, 1), ticker, date(2026, 6, 2)))

    # Simulate 3 consecutive daily EOD runs. Each run_date's _next_td is the
    # following session; the previous continuation row becomes spent.
    schedule = [
        (date(2026, 6, 2), date(2026, 6, 3)),
        (date(2026, 6, 3), date(2026, 6, 4)),
        (date(2026, 6, 4), date(2026, 6, 5)),
    ]
    for run_date, next_td in schedule:
        monkeypatch.setattr(engine, '_next_trading_day', lambda d, _n=next_td: _n)
        sig = _Sig(ticker)
        # Must not raise a unique-key violation; each mint has a fresh signal_date.
        engine.write_signals(cur, {strat: [sig]}, 'NORMAL', run_date)
        # Mark the freshly minted row FILLED so the NEXT day sees it as a held,
        # spent open row (mirrors parity_mark advancing COMPUTED→FILLED).
        cur.execute("""
            UPDATE execution_signals SET lifecycle_state='FILLED'
             WHERE strategy_id=%s AND ticker=%s AND workspace_id=%s
               AND signal_date=%s
        """, (strat, ticker, ws_id, run_date))

    rows = _open_rows(cur, strat, ticker, ws_id)
    # 1 seed + 3 mints, all distinct signal_dates ⇒ 4 open rows, no collision.
    assert len(rows) == 4, f"expected 4 open rows over 3 mints, got {len(rows)}"
    sig_dates = sorted(r['signal_date'] for r in rows)
    assert sig_dates == [date(2026, 6, 1), date(2026, 6, 2),
                         date(2026, 6, 3), date(2026, 6, 4)]
    # All target_dates distinct too.
    tgt_dates = sorted(r['target_date'] for r in rows)
    assert len(set(tgt_dates)) == 4


# ──────────────────────────────────────────────────────────────────
# Test 5 — D1: parity-mark fill of a continuation closes the spent sibling
# ──────────────────────────────────────────────────────────────────

def _seed_row(cur, *, strat, ws_id, ticker, signal_date, target_date,
              lifecycle_state, status='open', entry=100.0, stop=95.0, t1=110.0,
              direction='LONG'):
    cur.execute("""
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, stop_loss, target_1, position_size_pct, regime_state,
             signal_params, status, lifecycle_state, target_date)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0.05,'NORMAL',
                '{}'::jsonb,%s,%s,%s)
        RETURNING id
    """, (strat, ws_id, signal_date, ticker, direction, entry, stop, t1,
          status, lifecycle_state, target_date))
    return cur.fetchone()['id']


def test_d1_continuation_fill_closes_spent_sibling(db_conn, _db_meta):
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC5D1ROLL'

    spent_td = date(2026, 6, 4)
    cont_td = date(2026, 6, 5)         # the continuation's target_date == run_date

    # Spent sibling: held live position, older target_date.
    sibling_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=date(2026, 6, 3), target_date=spent_td,
        lifecycle_state='FILLED',
    )
    # Continuation row: APPROVED, target_date == run_date, about to be filled.
    cont_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=cont_td, target_date=cont_td,
        lifecycle_state='APPROVED',
    )

    close_price = 103.0

    def _broker_held():
        return {ticker: 5000.0}

    marked = finalize_parity_marks(
        cur, {ticker: close_price}, cont_td, ws_id, broker_loader=_broker_held,
    )
    assert marked == 1, "continuation row should be marked FILLED"

    # Continuation now FILLED, still open.
    cur.execute("SELECT status, lifecycle_state, mark_entry_price "
                "FROM execution_signals WHERE id=%s", (cont_id,))
    cont = cur.fetchone()
    assert cont['lifecycle_state'] == 'FILLED'
    assert cont['status'] == 'open'
    assert abs(float(cont['mark_entry_price']) - close_price) < 1e-9

    # Spent sibling CLOSED in execution_signals.
    cur.execute("SELECT status, lifecycle_state, fill_price "
                "FROM execution_signals WHERE id=%s", (sibling_id,))
    sib = cur.fetchone()
    assert sib['status'] == 'closed', "spent sibling must be closed"
    assert sib['lifecycle_state'] == 'CLOSED_AT_OPEN'
    assert abs(float(sib['fill_price']) - close_price) < 1e-9, \
        "sibling closed at the continuation's mark (no gap)"

    # close_reason + closed_at live on signal_pnl (the existing close semantics).
    cur.execute("SELECT status, close_reason, closed_at "
                "FROM signal_pnl WHERE signal_id=%s "
                "ORDER BY pnl_date DESC LIMIT 1", (sibling_id,))
    pnl = cur.fetchone()
    assert pnl is not None, "signal_pnl close row must exist for the rolled sibling"
    assert pnl['status'] == 'closed'
    assert pnl['close_reason'] == 'rolled_continuation'
    assert pnl['closed_at'] is not None


def test_d1_non_continuation_fill_changes_nothing(db_conn, _db_meta):
    """A first-time fill (no older sibling) closes nothing."""
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC5D1FIRST'

    run_td = date(2026, 6, 5)
    sig_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=run_td, target_date=run_td,
        lifecycle_state='APPROVED',
    )

    def _broker_held():
        return {ticker: 5000.0}

    marked = finalize_parity_marks(
        cur, {ticker: 101.0}, run_td, ws_id, broker_loader=_broker_held,
    )
    assert marked == 1

    # No signal_pnl 'rolled_continuation' row anywhere for this ticker/strategy.
    cur.execute("""
        SELECT count(*) FROM signal_pnl
         WHERE strategy_id=%s AND close_reason='rolled_continuation'
           AND signal_id IN (SELECT id FROM execution_signals
                             WHERE strategy_id=%s AND ticker=%s AND workspace_id=%s)
    """, (strat, strat, ticker, ws_id))
    assert cur.fetchone()[0] == 0, "first-time fill must not roll any sibling"

    # The filled row itself stays open + FILLED.
    cur.execute("SELECT status, lifecycle_state FROM execution_signals WHERE id=%s",
                (sig_id,))
    row = cur.fetchone()
    assert row['status'] == 'open'
    assert row['lifecycle_state'] == 'FILLED'


def test_d1_unfilled_continuation_leaves_sibling_open(db_conn, _db_meta):
    """If the continuation row is NOT broker-held, it is not marked FILLED, so
    the sibling-close never fires — the spent sibling stays open."""
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC5D1NOFILL'

    spent_td = date(2026, 6, 4)
    cont_td = date(2026, 6, 5)

    sibling_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=date(2026, 6, 3), target_date=spent_td,
        lifecycle_state='FILLED',
    )
    cont_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=cont_td, target_date=cont_td,
        lifecycle_state='APPROVED',
    )

    # Broker does NOT hold the ticker → continuation never marked FILLED.
    def _broker_empty():
        return {}

    marked = finalize_parity_marks(
        cur, {ticker: 103.0}, cont_td, ws_id, broker_loader=_broker_empty,
    )
    assert marked == 0, "unfilled continuation must not be marked"

    # Continuation stays APPROVED; sibling stays open.
    cur.execute("SELECT status, lifecycle_state FROM execution_signals WHERE id=%s",
                (cont_id,))
    cont = cur.fetchone()
    assert cont['lifecycle_state'] == 'APPROVED'

    cur.execute("SELECT status FROM execution_signals WHERE id=%s", (sibling_id,))
    assert cur.fetchone()['status'] == 'open', "sibling must stay open when no roll"


# ──────────────────────────────────────────────────────────────────
# Test 6 — D1 multi-sibling roll: TWO older spent siblings both closed
# ──────────────────────────────────────────────────────────────────

def test_d1_continuation_fill_closes_all_spent_siblings(db_conn, _db_meta):
    """Two older spent FILLED+open siblings (distinct signal/target dates, same
    strategy/ticker) + a continuation row marked FILLED ⇒ BOTH siblings closed
    with close_reason='rolled_continuation' at the continuation's mark."""
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC6MULTISIB'

    older_td = date(2026, 6, 2)
    newer_td = date(2026, 6, 4)
    cont_td = date(2026, 6, 5)          # continuation target_date == run_date

    # Two spent siblings, both held live, distinct (signal_date, target_date).
    sib_older_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=date(2026, 6, 1), target_date=older_td,
        lifecycle_state='FILLED',
    )
    sib_newer_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=date(2026, 6, 3), target_date=newer_td,
        lifecycle_state='FILLED',
    )
    # Continuation row about to be filled.
    cont_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=cont_td, target_date=cont_td,
        lifecycle_state='APPROVED',
    )

    close_price = 104.0

    def _broker_held():
        return {ticker: 5000.0}

    marked = finalize_parity_marks(
        cur, {ticker: close_price}, cont_td, ws_id, broker_loader=_broker_held,
    )
    assert marked == 1, "only the continuation row should be marked FILLED"

    # Continuation now FILLED, still open.
    cur.execute("SELECT status, lifecycle_state FROM execution_signals WHERE id=%s",
                (cont_id,))
    cont = cur.fetchone()
    assert cont['lifecycle_state'] == 'FILLED'
    assert cont['status'] == 'open'

    # BOTH spent siblings closed at the continuation's mark.
    for sib_id in (sib_older_id, sib_newer_id):
        cur.execute("SELECT status, lifecycle_state, fill_price "
                    "FROM execution_signals WHERE id=%s", (sib_id,))
        sib = cur.fetchone()
        assert sib['status'] == 'closed', f"sibling {sib_id} must be closed"
        assert sib['lifecycle_state'] == 'CLOSED_AT_OPEN'
        assert abs(float(sib['fill_price']) - close_price) < 1e-9

        cur.execute("SELECT status, close_reason, closed_at FROM signal_pnl "
                    "WHERE signal_id=%s ORDER BY pnl_date DESC LIMIT 1", (sib_id,))
        pnl = cur.fetchone()
        assert pnl is not None, f"signal_pnl close row must exist for sibling {sib_id}"
        assert pnl['status'] == 'closed'
        assert pnl['close_reason'] == 'rolled_continuation'
        assert pnl['closed_at'] is not None


# ──────────────────────────────────────────────────────────────────
# Test 7 — D1 failure path: a raising sibling-close must NOT poison the txn
# ──────────────────────────────────────────────────────────────────

def test_d1_sibling_close_failure_does_not_poison_txn(db_conn, _db_meta, monkeypatch):
    """Savepoint-isolation pin: when drop_signal_close raises (aborting the PG
    txn) for a sibling, finalize_parity_marks must NOT propagate the exception,
    the continuation row must still end FILLED, the transaction must remain
    usable (a further statement executes + commit/rollback works), and the
    sibling must stay open (its roll degraded to a loudly-logged skip).

    This test MUST fail on the pre-fix code: without the per-row savepoint the
    aborting SQL inside drop_signal_close leaves the connection in
    InFailedSqlTransaction, and the exception propagates out of
    finalize_parity_marks.
    """
    cur = db_conn.cursor()
    strat = _db_meta['strategy_id']
    ws_id = _db_meta['ws_id']
    ticker = 'ZZC7FAILSIB'

    spent_td = date(2026, 6, 4)
    cont_td = date(2026, 6, 5)

    sibling_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=date(2026, 6, 3), target_date=spent_td,
        lifecycle_state='FILLED',
    )
    cont_id = _seed_row(
        cur, strat=strat, ws_id=ws_id, ticker=ticker,
        signal_date=cont_td, target_date=cont_td,
        lifecycle_state='APPROVED',
    )

    def _broker_held():
        return {ticker: 5000.0}

    # Make drop_signal_close raise by executing aborting SQL on the cursor —
    # this poisons the PG transaction exactly like a corrupt-row failure would.
    # Patch the name at its definition module (the function-top import resolves
    # execution.open_reconcile.drop_signal_close at call time).
    def _boom(cur, *args, **kwargs):
        cur.execute("SELECT 1/0")

    monkeypatch.setattr(open_reconcile, 'drop_signal_close', _boom)

    # Must NOT raise — the savepoint contains the abort.
    marked = finalize_parity_marks(
        cur, {ticker: 103.0}, cont_td, ws_id, broker_loader=_broker_held,
    )
    assert marked == 1, "continuation should still be marked FILLED"

    # Continuation row still FILLED (the FILLED UPDATE is outside the savepoint).
    cur.execute("SELECT status, lifecycle_state FROM execution_signals WHERE id=%s",
                (cont_id,))
    cont = cur.fetchone()
    assert cont['lifecycle_state'] == 'FILLED', "continuation must remain FILLED"
    assert cont['status'] == 'open'

    # Transaction is USABLE — a further statement runs and commit/rollback works.
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1, "txn must remain usable after the failed roll"

    # Sibling stays OPEN — its roll degraded to a skip, not a partial close.
    cur.execute("SELECT status FROM execution_signals WHERE id=%s", (sibling_id,))
    assert cur.fetchone()['status'] == 'open', \
        "sibling must stay open when its roll failed"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
