# SP-6 Phase B0 — Fill-Persistence + Execution Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop discarding the real broker entry fill — capture it into parity-mark-safe columns on `execution_signals` and materialize the `(close[T+1] − actual_fill) × signed_qty` execution ledger Phase B grades against.

**Architecture:** One additive migration adds four nullable columns. `finalize_parity_marks` is extended to read the already-reconciled fill from `alpaca_submissions` (keyed by `(strategy_id, ticker)` for the run's `target_date`) and write `actual_fill_price/qty/at` + `exec_ledger_usd` in the SAME row UPDATE that already marks the signal FILLED — *before* `fill_price` is clobbered with the official close. Pure data layer; no consumer, no hot-path coupling, no new broker call. Gate-off-inert (runs only in the gated SP-6 4 PM block).

**Tech Stack:** Python 3 + psycopg2 (DictCursor), PostgreSQL, pytest with live-DB rollback isolation.

**Spec:** `docs/superpowers/specs/2026-06-01-sp6-phase-b0-fill-persistence-design.md`

---

## File Structure

- **Create:** `src/database/migrations/127_sp6_b0_fill_persistence.sql` — additive columns (responsibility: schema).
- **Modify:** `src/execution/parity_mark.py` — extend the SELECT (add `strategy_id`), load the day's fills into a `(strategy_id, norm_ticker)` dict, compute the ledger per row, extend the UPDATE (responsibility: capture + ledger).
- **Create (test):** `tests/test_sp6_b0_fill_capture.py` — TDD suite (mirrors `tests/test_sp6_parity_mark.py` harness).

**Pre-flight grounding (do before Task 1 — verify, don't assume):** confirm the production SP-6 sizer/executor records each open's `alpaca_submissions` row under the **signal's own** `strategy_id` (not a netted/synthetic aggregate id). Trace `src/execution/regime_blended_sizer.py` → `src/execution/alpaca_executor.py:record_submission` and confirm the `strategy_id` written matches `execution_signals.strategy_id`. If it nets per-ticker under a synthetic id, change the dict key in Task 2 to ticker-only (`_norm_ticker(ticker)`) and note the same-ticker-multi-strategy fills then share one VWAP. A mismatch is non-fatal (yields NULL ledger) but silently empty — so this must be checked.

---

### Task 1: Additive migration (four nullable columns)

**Files:**
- Create: `src/database/migrations/127_sp6_b0_fill_persistence.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 127_sp6_b0_fill_persistence.sql
-- SP-6 Phase B0: persist the real broker ENTRY fill + materialize the execution ledger.
--
-- Additive only (master-DB NEVER-DELETE invariant): four nullable columns, NO DEFAULT.
-- These hold the ground-truth broker fill (copied from alpaca_submissions before
-- parity_mark overwrites fill_price with the official close) and the entry execution
-- ledger (official close[T+1] - actual_fill) x signed_qty that Phase B grades against.
ALTER TABLE execution_signals
    ADD COLUMN IF NOT EXISTS actual_fill_price NUMERIC,
    ADD COLUMN IF NOT EXISTS actual_fill_qty   NUMERIC,
    ADD COLUMN IF NOT EXISTS actual_filled_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS exec_ledger_usd   NUMERIC;
```

- [ ] **Step 2: Apply to the live DB (additive/idempotent — in-pattern with migrations 119/126)**

Run:
```bash
cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence
python3 -c "
import os, psycopg2
sql = open('src/database/migrations/127_sp6_b0_fill_persistence.sql').read()
conn = psycopg2.connect(os.environ['POSTGRES_URI']); conn.autocommit = True
cur = conn.cursor(); cur.execute(sql)
cur.execute(\"\"\"SELECT column_name FROM information_schema.columns
                 WHERE table_name='execution_signals'
                   AND column_name IN ('actual_fill_price','actual_fill_qty',
                                       'actual_filled_at','exec_ledger_usd')
                 ORDER BY column_name\"\"\")
print('cols:', [r[0] for r in cur.fetchall()])
conn.close()
"
```
Expected output: `cols: ['actual_fill_price', 'actual_fill_qty', 'actual_filled_at', 'exec_ledger_usd']`

(The migration uses `ADD COLUMN IF NOT EXISTS`, so re-running is a no-op. Columns are nullable with no default → existing rows are unaffected; this is a pure ADD, allowed by the NEVER-DELETE invariant.)

- [ ] **Step 3: Commit**

```bash
git add src/database/migrations/127_sp6_b0_fill_persistence.sql
git commit -m "feat(sp6-b0): migration 127 — additive fill-persistence columns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Capture the real fill + ledger in `finalize_parity_marks` (core happy path)

**Files:**
- Modify: `src/execution/parity_mark.py` (SELECT ~line 94; row-unpack ~line 127; after `held` build ~line 120; per-row compute before the UPDATE ~line 172; UPDATE ~line 185)
- Test: `tests/test_sp6_b0_fill_capture.py`

- [ ] **Step 1: Write the failing test (LONG filled below close → +ledger, columns populated)**

Create `tests/test_sp6_b0_fill_capture.py`:

```python
"""test_sp6_b0_fill_capture.py — SP-6 Phase B0 fill-persistence + execution ledger.

finalize_parity_marks must, for each broker-held row it marks FILLED, copy the real
broker fill (already reconciled into alpaca_submissions) into actual_fill_price/qty/at
and materialize exec_ledger_usd = (official close - actual_fill) x (direction_sign x qty)
— WITHOUT changing fill_price/mark_entry_price (still the official close → parity intact).

DB tests use rollback isolation (no persistent side-effects). Broker is always injected.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.parity_mark import finalize_parity_marks


@pytest.fixture
def db_conn():
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(os.environ['POSTGRES_URI'],
                            cursor_factory=psycopg2.extras.DictCursor)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def _db_meta(db_conn):
    cur = db_conn.cursor()
    cur.execute("SELECT id FROM workspaces WHERE name='default' LIMIT 1")
    ws_row = cur.fetchone()
    assert ws_row is not None, "No 'default' workspace"
    cur.execute("SELECT id FROM strategy_registry WHERE status='approved' LIMIT 1")
    st_row = cur.fetchone()
    assert st_row is not None, "No approved strategy in strategy_registry"
    return {'ws_id': ws_row['id'], 'strategy_id': st_row['id']}


def _insert_signal(cur, ticker, direction, entry_price, stop_loss, target_1,
                   lifecycle_state, target_date, *, ws_id, strategy_id, run_date=None):
    signal_date = run_date or date.today()
    cur.execute("""
        INSERT INTO execution_signals
            (strategy_id, workspace_id, signal_date, ticker, direction,
             entry_price, stop_loss, target_1, target_2, target_3,
             position_size_pct, regime_state, signal_params, status,
             lifecycle_state, target_date)
        VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s::jsonb,%s, %s,%s)
        RETURNING id
    """, (strategy_id, ws_id, signal_date, ticker, direction,
          entry_price, stop_loss, target_1, None, None,
          0.05, 'NORMAL', '{}', 'open', lifecycle_state, target_date))
    row = cur.fetchone()
    return row['id'] if hasattr(row, 'keys') else row[0]


def _insert_submission(cur, *, run_date, ticker, strategy_id, direction,
                       qty, filled_qty, filled_avg_price, broker_status,
                       entry_price=100.0):
    """Insert an alpaca_submissions row carrying a (reconciled) broker fill."""
    coid = f"b0test-{strategy_id}-{ticker}".replace('/', '-')
    cur.execute("""
        INSERT INTO alpaca_submissions
            (run_date, ticker, strategy_id, direction, qty, entry_price,
             time_in_force, order_class, client_order_id,
             broker_status, filled_qty, filled_avg_price, reconciled_at)
        VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, NOW())
    """, (run_date, ticker, strategy_id, direction, qty, entry_price,
          'day', 'simple', coid, broker_status, filled_qty, filled_avg_price))


def _broker_held(*held):
    held_map = {t: 5000.0 for t in held}
    def _loader():
        return held_map
    return _loader


def _fetch(cur, sig_id):
    cur.execute("""SELECT lifecycle_state, fill_price, mark_entry_price,
                          actual_fill_price, actual_fill_qty, actual_filled_at,
                          exec_ledger_usd
                     FROM execution_signals WHERE id=%s""", (sig_id,))
    return cur.fetchone()


@pytest.mark.integration
def test_long_filled_below_close_positive_ledger(db_conn, _db_meta):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZB0LONG'
    close = 103.5
    sig_id = _insert_signal(cur, ticker, 'LONG', 100.0, 94.0, 112.0,
                            'EXECUTING', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    _insert_submission(cur, run_date=run_date, ticker=ticker,
                       strategy_id=_db_meta['strategy_id'], direction='long',
                       qty=100, filled_qty=100, filled_avg_price=99.0,
                       broker_status='filled')

    marked = finalize_parity_marks(cur, {ticker: close}, run_date,
                                   _db_meta['ws_id'], broker_loader=_broker_held(ticker))
    assert marked == 1

    r = _fetch(cur, sig_id)
    assert r['lifecycle_state'] == 'FILLED'
    assert float(r['actual_fill_price']) == 99.0
    assert float(r['actual_fill_qty']) == 100.0
    assert r['actual_filled_at'] is not None
    # (103.5 - 99.0) * (+1 * 100) = 450.0  → beat the close
    assert abs(float(r['exec_ledger_usd']) - 450.0) < 1e-6
    # parity invariant: fill_price + mark_entry_price are STILL the official close
    assert abs(float(r['fill_price']) - close) < 1e-6
    assert abs(float(r['mark_entry_price']) - close) < 1e-6
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence && python3 -m pytest tests/test_sp6_b0_fill_capture.py::test_long_filled_below_close_positive_ledger -v`
Expected: FAIL — `actual_fill_price`/`exec_ledger_usd` come back `None` (capture not implemented yet). (Migration 127 from Task 1 means the columns exist, so the failure is a NULL assertion, not a missing-column error.)

- [ ] **Step 3: Implement the capture in `finalize_parity_marks`**

In `src/execution/parity_mark.py`:

(a) Extend the main SELECT to also pull `strategy_id`:

```python
    cur.execute("""
        SELECT id, strategy_id, ticker, direction, entry_price, stop_loss, target_1
          FROM execution_signals
         WHERE lifecycle_state IN ('APPROVED', 'EXECUTING', 'FILLED')
           AND target_date = %s
           AND workspace_id = %s
         ORDER BY id
    """, (run_date, workspace_id))
```

(b) After the `held = {...}` line, load the day's broker fills into a `(strategy_id, norm_ticker)` dict:

```python
    # ── B0: load today's reconciled broker fills from alpaca_submissions ──
    # Keyed by (strategy_id, normalized ticker): that triple is UNIQUE per run_date,
    # so two strategies trading the same ticker stay distinct.  Only rows with a
    # finite fill price (and not an explicit non-fill broker_status) are kept.
    cur.execute("""
        SELECT strategy_id, ticker, filled_avg_price, filled_qty,
               reconciled_at, submitted_at, broker_status
          FROM alpaca_submissions
         WHERE run_date = %s
    """, (run_date,))
    fills = {}
    for srow in cur.fetchall():
        if hasattr(srow, 'keys'):
            s_strat, s_tkr = srow['strategy_id'], srow['ticker']
            s_avg, s_qty = srow['filled_avg_price'], srow['filled_qty']
            s_recon, s_sub = srow['reconciled_at'], srow['submitted_at']
            s_status = srow['broker_status']
        else:
            s_strat, s_tkr, s_avg, s_qty, s_recon, s_sub, s_status = srow
        avg = _safe_float(s_avg, 'filled_avg_price')
        if avg is None:
            continue  # no real fill price → leave ledger NULL
        if s_status is not None and s_status not in ('filled', 'partial'):
            continue  # explicit non-fill state (error/rejected/…)
        fills[(s_strat, _norm_ticker(s_tkr))] = (
            avg, _safe_float(s_qty, 'filled_qty'), (s_recon or s_sub),
        )
```

(c) Update the row-unpack block to read `strategy_id` (both cursor shapes):

```python
        if hasattr(row, 'keys'):
            row_id, ticker = row['id'], row['ticker']
            strategy_id = row['strategy_id']
            direction_raw = row['direction']
            entry_price_raw = row['entry_price']
            stop_raw = row['stop_loss']
            target_raw = row['target_1']
        else:
            (row_id, strategy_id, ticker, direction_raw,
             entry_price_raw, stop_raw, target_raw) = row
```

(d) Just before the `cur.execute("UPDATE ...")`, compute the fill + ledger:

```python
        # ── B0: capture the real entry fill + execution ledger ──
        fill = fills.get((strategy_id, _norm_ticker(ticker)))
        if fill is not None:
            actual_fill_price, actual_fill_qty, actual_filled_at = fill
        else:
            actual_fill_price, actual_fill_qty, actual_filled_at = None, None, None

        if actual_fill_price is not None and actual_fill_qty is not None:
            exec_ledger_usd = (
                (mark_price - actual_fill_price) * (direction_sign * actual_fill_qty)
            )
        else:
            exec_ledger_usd = None
```

(e) Extend the UPDATE statement to set the four new columns:

```python
        cur.execute("""
            UPDATE execution_signals
               SET mark_entry_price  = %s,
                   fill_price        = %s,
                   stop_loss         = %s,
                   target_1          = %s,
                   filled_at         = %s,
                   lifecycle_state   = 'FILLED',
                   actual_fill_price = %s,
                   actual_fill_qty   = %s,
                   actual_filled_at  = %s,
                   exec_ledger_usd   = %s
             WHERE id = %s
        """, (mark_price, mark_price, new_stop, new_target, now_ts,
              actual_fill_price, actual_fill_qty, actual_filled_at, exec_ledger_usd,
              row_id))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence && python3 -m pytest tests/test_sp6_b0_fill_capture.py::test_long_filled_below_close_positive_ledger -v`
Expected: PASS

- [ ] **Step 5: Update the module docstring + commit**

In `src/execution/parity_mark.py`, append to the module docstring (after the existing "mark_entry_price is the OFFICIAL close" sentence):

```
The real broker fill is preserved separately in actual_fill_price/qty/at (copied from
alpaca_submissions before fill_price is set to the official close), and exec_ledger_usd
= (official close - actual_fill) x (direction_sign x qty) is materialized for Phase B.
```

```bash
git add src/execution/parity_mark.py tests/test_sp6_b0_fill_capture.py
git commit -m "feat(sp6-b0): capture real entry fill + execution ledger in parity_mark

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Sign-convention + edge-case tests

**Files:**
- Test: `tests/test_sp6_b0_fill_capture.py` (append)

These assert distinct behaviors against the Task-2 implementation. Add all five, run them, and fix `parity_mark.py` if any fails.

- [ ] **Step 1: Add the edge-case tests**

Append to `tests/test_sp6_b0_fill_capture.py`:

```python
@pytest.mark.integration
def test_short_filled_above_close_positive_ledger(db_conn, _db_meta):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZB0SHORT'
    close = 103.5
    sig_id = _insert_signal(cur, ticker, 'SHORT', 100.0, 106.0, 88.0,
                            'EXECUTING', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    _insert_submission(cur, run_date=run_date, ticker=ticker,
                       strategy_id=_db_meta['strategy_id'], direction='short',
                       qty=100, filled_qty=100, filled_avg_price=105.0,
                       broker_status='filled')
    finalize_parity_marks(cur, {ticker: close}, run_date,
                          _db_meta['ws_id'], broker_loader=_broker_held(ticker))
    r = _fetch(cur, sig_id)
    # (103.5 - 105.0) * (-1 * 100) = 150.0  → sold above the close, beat it
    assert abs(float(r['exec_ledger_usd']) - 150.0) < 1e-6


@pytest.mark.integration
def test_long_filled_above_close_negative_ledger(db_conn, _db_meta):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZB0LONGNEG'
    close = 103.5
    sig_id = _insert_signal(cur, ticker, 'LONG', 100.0, 94.0, 112.0,
                            'EXECUTING', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    _insert_submission(cur, run_date=run_date, ticker=ticker,
                       strategy_id=_db_meta['strategy_id'], direction='long',
                       qty=100, filled_qty=100, filled_avg_price=105.0,
                       broker_status='filled')
    finalize_parity_marks(cur, {ticker: close}, run_date,
                          _db_meta['ws_id'], broker_loader=_broker_held(ticker))
    r = _fetch(cur, sig_id)
    # (103.5 - 105.0) * (+1 * 100) = -150.0  → paid up vs the close
    assert abs(float(r['exec_ledger_usd']) + 150.0) < 1e-6


@pytest.mark.integration
def test_no_submission_leaves_fill_and_ledger_null(db_conn, _db_meta):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZB0NOSUB'
    close = 103.5
    sig_id = _insert_signal(cur, ticker, 'LONG', 100.0, 94.0, 112.0,
                            'EXECUTING', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    # No alpaca_submissions row inserted.
    marked = finalize_parity_marks(cur, {ticker: close}, run_date,
                                   _db_meta['ws_id'], broker_loader=_broker_held(ticker))
    assert marked == 1
    r = _fetch(cur, sig_id)
    assert r['lifecycle_state'] == 'FILLED'         # still marked
    assert r['actual_fill_price'] is None
    assert r['actual_fill_qty'] is None
    assert r['exec_ledger_usd'] is None
    assert abs(float(r['fill_price']) - close) < 1e-6  # parity untouched


@pytest.mark.integration
def test_partial_fill_uses_filled_qty(db_conn, _db_meta):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZB0PARTIAL'
    close = 103.5
    sig_id = _insert_signal(cur, ticker, 'LONG', 100.0, 94.0, 112.0,
                            'EXECUTING', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    _insert_submission(cur, run_date=run_date, ticker=ticker,
                       strategy_id=_db_meta['strategy_id'], direction='long',
                       qty=100, filled_qty=60, filled_avg_price=99.0,
                       broker_status='partial')
    finalize_parity_marks(cur, {ticker: close}, run_date,
                          _db_meta['ws_id'], broker_loader=_broker_held(ticker))
    r = _fetch(cur, sig_id)
    assert float(r['actual_fill_qty']) == 60.0
    # (103.5 - 99.0) * (1 * 60) = 270.0
    assert abs(float(r['exec_ledger_usd']) - 270.0) < 1e-6


@pytest.mark.integration
def test_crypto_symbol_normalization(db_conn, _db_meta):
    """Signal ticker BTC-USD, submission ticker BTC/USD, broker holds BTC/USD —
    normalization (/ → -) must match all three; fractional qty preserved."""
    cur = db_conn.cursor()
    run_date = date.today()
    sig_ticker = 'BTC-USD'
    close = 50000.0
    sig_id = _insert_signal(cur, sig_ticker, 'LONG', 49000.0, 45000.0, 55000.0,
                            'EXECUTING', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    _insert_submission(cur, run_date=run_date, ticker='BTC/USD',
                       strategy_id=_db_meta['strategy_id'], direction='long',
                       qty=1, filled_qty=0.5, filled_avg_price=49500.0,
                       broker_status='filled', entry_price=49000.0)
    marked = finalize_parity_marks(cur, {sig_ticker: close}, run_date,
                                   _db_meta['ws_id'], broker_loader=_broker_held('BTC/USD'))
    assert marked == 1
    r = _fetch(cur, sig_id)
    assert float(r['actual_fill_qty']) == 0.5            # fractional preserved
    # (50000 - 49500) * (1 * 0.5) = 250.0
    assert abs(float(r['exec_ledger_usd']) - 250.0) < 1e-6
```

- [ ] **Step 2: Run the edge-case tests**

Run: `cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence && python3 -m pytest tests/test_sp6_b0_fill_capture.py -v -k "short or above_close or no_submission or partial or crypto"`
Expected: 5 PASS. If any fails, fix `parity_mark.py` (do NOT weaken the test) and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sp6_b0_fill_capture.py
git commit -m "test(sp6-b0): sign-convention + null/partial/crypto edge cases

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Regression + idempotency tests (parity preserved)

**Files:**
- Test: `tests/test_sp6_b0_fill_capture.py` (append)

- [ ] **Step 1: Add regression + idempotency tests**

Append to `tests/test_sp6_b0_fill_capture.py`:

```python
@pytest.mark.integration
def test_not_held_row_skipped_no_capture(db_conn, _db_meta):
    """APPROVED + NOT held in broker → stays APPROVED, no fill captured even if a
    submission row exists (the row is skipped before the capture)."""
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZB0NOTHELD'
    close = 103.5
    sig_id = _insert_signal(cur, ticker, 'LONG', 100.0, 94.0, 112.0,
                            'APPROVED', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    _insert_submission(cur, run_date=run_date, ticker=ticker,
                       strategy_id=_db_meta['strategy_id'], direction='long',
                       qty=100, filled_qty=100, filled_avg_price=99.0,
                       broker_status='filled')
    # Broker holds NOTHING.
    marked = finalize_parity_marks(cur, {ticker: close}, run_date,
                                   _db_meta['ws_id'], broker_loader=_broker_held())
    assert marked == 0
    r = _fetch(cur, sig_id)
    assert r['lifecycle_state'] == 'APPROVED'   # untouched
    assert r['actual_fill_price'] is None
    assert r['exec_ledger_usd'] is None


@pytest.mark.integration
def test_idempotent_rerun_identical(db_conn, _db_meta):
    """Re-running parity_mark over the now-FILLED row yields identical capture."""
    cur = db_conn.cursor()
    run_date = date.today()
    ticker = 'ZZB0IDEM'
    close = 103.5
    sig_id = _insert_signal(cur, ticker, 'LONG', 100.0, 94.0, 112.0,
                            'EXECUTING', run_date,
                            ws_id=_db_meta['ws_id'], strategy_id=_db_meta['strategy_id'])
    _insert_submission(cur, run_date=run_date, ticker=ticker,
                       strategy_id=_db_meta['strategy_id'], direction='long',
                       qty=100, filled_qty=100, filled_avg_price=99.0,
                       broker_status='filled')
    loader = _broker_held(ticker)
    finalize_parity_marks(cur, {ticker: close}, run_date, _db_meta['ws_id'], broker_loader=loader)
    first = _fetch(cur, sig_id)
    finalize_parity_marks(cur, {ticker: close}, run_date, _db_meta['ws_id'], broker_loader=loader)
    second = _fetch(cur, sig_id)
    assert float(second['exec_ledger_usd']) == float(first['exec_ledger_usd'])
    assert float(second['actual_fill_price']) == float(first['actual_fill_price'])
    assert float(second['fill_price']) == float(first['fill_price'])
```

- [ ] **Step 2: Run the full B0 suite + the existing parity regression suite**

Run: `cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence && python3 -m pytest tests/test_sp6_b0_fill_capture.py tests/test_sp6_parity_mark.py -v`
Expected: all PASS (new B0 suite green AND the pre-existing `test_sp6_parity_mark.py` still green — proves the extension didn't regress parity behavior).

- [ ] **Step 3: Commit**

```bash
git add tests/test_sp6_b0_fill_capture.py
git commit -m "test(sp6-b0): not-held skip + idempotency regression (parity preserved)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage:**
- §3 data model (4 columns) → Task 1. ✓
- §4 capture mechanism (submissions dict keyed by `(strategy_id, norm_ticker)`, SELECT+`strategy_id`, single extended UPDATE, ledger formula) → Task 2. ✓
- §4 sign convention (LONG/SHORT beat-close) → Task 3 (short-above-close, long-above-close-negative). ✓
- §5 edge cases: no submission → NULL (Task 3); partial → filled_qty (Task 3); fractional crypto + normalization (Task 3); gate-off-inert (structural — `finalize_parity_marks` only runs in the gated block, no code path added outside it); parity unchanged (Task 2 + Task 4 assertions on `fill_price`/`mark_entry_price`); idempotent re-run (Task 4); not-held skip preserved (Task 4). ✓
- §6 testing — every listed test mapped to Tasks 2–4. ✓
- §4 plan-grounding verification (strategy_id mapping) → pre-flight grounding section. ✓

**Placeholder scan:** none — every step has concrete SQL/Python/commands + expected output.

**Type/name consistency:** `actual_fill_price`/`actual_fill_qty`/`actual_filled_at`/`exec_ledger_usd`, `fills[(strategy_id, norm_ticker)]`, `_norm_ticker`, `_safe_float`, `direction_sign`, `mark_price` — used identically across migration, implementation, and tests. Helper names (`_insert_signal`, `_insert_submission`, `_broker_held`, `_fetch`) are defined once in Task 2 and reused in Tasks 3–4.

**Out of scope (confirmed not in any task):** exit/close-side slippage; any consumer/readout; B1 scheduler / B2 Hawkes.
