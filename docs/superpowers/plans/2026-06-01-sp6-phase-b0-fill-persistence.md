# SP-6 Phase B0 — Fill-Persistence + Execution Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the close[T+1] benchmark + materialize the `(close − fill) × signed_qty` execution ledger at the **order grain** (on `alpaca_submissions`, where the real fill already lives), so Phase B has ground-truth execution data once Phase A is live.

**Architecture:** One additive migration adds two nullable columns to `alpaca_submissions` (`official_close`, `exec_ledger_usd`). A new `finalize_execution_ledger(cur, closes, run_date)` — a **sibling** to `finalize_parity_marks` in `src/execution/parity_mark.py` — reads each filled entry order on `run_date`, looks up the official close, and writes the benchmark + signed ledger. It's wired into the gated SP-6 4 PM block right after `finalize_parity_marks`. No `execution_signals` changes; gate-off-inert.

**Tech Stack:** Python 3 + psycopg2 (DictCursor), PostgreSQL, pytest with live-DB rollback isolation.

**Spec:** `docs/superpowers/specs/2026-06-01-sp6-phase-b0-fill-persistence-design.md` (see §0 for why the grain is per-order, not per-signal).

---

## File Structure

- **Create:** `src/database/migrations/127_sp6_b0_fill_persistence.sql` — 2 additive columns on `alpaca_submissions`.
- **Modify:** `src/execution/parity_mark.py` — add `finalize_execution_ledger` (new function; the existing `finalize_parity_marks` is untouched).
- **Modify:** `src/execution/engine.py:1445-1458` — wire the sibling call into the gated 4 PM block.
- **Create (test):** `tests/test_sp6_b0_fill_capture.py` — TDD suite (live-DB rollback; inserts `alpaca_submissions` rows + a `closes` dict; no `execution_signals`, no broker injection).

**Grounding already done (do not re-litigate):** the production sizer consolidates per ticker — `alpaca_submissions` is one row per broker order (verified live 2026-05-28: 1 row/ticker). The real fill (`filled_avg_price`/`filled_qty`) is already populated there by `alpaca_reconcile`. That is why B0 is per-order columns, not per-signal.

---

### Task 1: Additive migration (two columns on `alpaca_submissions`)

**Files:**
- Create: `src/database/migrations/127_sp6_b0_fill_persistence.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 127_sp6_b0_fill_persistence.sql
-- SP-6 Phase B0: per-order execution ledger.
--
-- Additive only (master-DB NEVER-DELETE invariant): two nullable columns, NO DEFAULT,
-- on alpaca_submissions (the order grain — one row per consolidated broker order; the
-- real fill already lives here as filled_avg_price/filled_qty via alpaca_reconcile).
--   official_close  : official close[T+1] for this order's ticker (beat-close benchmark)
--   exec_ledger_usd : (official_close - filled_avg_price) x (direction_sign x filled_qty)
ALTER TABLE alpaca_submissions
    ADD COLUMN IF NOT EXISTS official_close  NUMERIC,
    ADD COLUMN IF NOT EXISTS exec_ledger_usd NUMERIC;
```

- [ ] **Step 2: Apply to the live DB (additive/idempotent — in-pattern with migrations 119/126)**

Run:
```bash
cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | head -1 | cut -d= -f2- | tr -d '"') python3 -c "
import os, psycopg2
sql = open('src/database/migrations/127_sp6_b0_fill_persistence.sql').read()
conn = psycopg2.connect(os.environ['POSTGRES_URI']); conn.autocommit = True
cur = conn.cursor(); cur.execute(sql)
cur.execute(\"\"\"SELECT column_name FROM information_schema.columns
                 WHERE table_name='alpaca_submissions'
                   AND column_name IN ('official_close','exec_ledger_usd')
                 ORDER BY column_name\"\"\")
print('cols:', [r[0] for r in cur.fetchall()])
conn.close()
"
```
Expected output: `cols: ['exec_ledger_usd', 'official_close']`

(Idempotent: `ADD COLUMN IF NOT EXISTS`. Nullable/no-default → existing rows unaffected; pure ADD, allowed by the NEVER-DELETE invariant. The `POSTGRES_URI` is read from `.env` without printing it.)

- [ ] **Step 3: Commit**

```bash
git add src/database/migrations/127_sp6_b0_fill_persistence.sql
git commit -m "feat(sp6-b0): migration 127 — per-order execution-ledger columns

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `finalize_execution_ledger` + wiring + core happy-path test

**Files:**
- Modify: `src/execution/parity_mark.py` (add the new function at end of file)
- Modify: `src/execution/engine.py:1447,1454-1455` (import + sibling call)
- Test: `tests/test_sp6_b0_fill_capture.py`

- [ ] **Step 1: Write the failing test (long filled below close → +ledger, benchmark set)**

Create `tests/test_sp6_b0_fill_capture.py`:

```python
"""test_sp6_b0_fill_capture.py — SP-6 Phase B0 per-order execution ledger.

finalize_execution_ledger reads filled ENTRY orders from alpaca_submissions on run_date
and writes official_close (the close[T+1] benchmark) + exec_ledger_usd
  = (official_close - filled_avg_price) x (direction_sign x filled_qty).
exec_ledger_usd > 0 ⟺ the fill beat the close (long below close / short above close).

DB tests use rollback isolation (no persistent side-effects). No execution_signals rows,
no broker injection — just alpaca_submissions rows + a closes dict.
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

from execution.parity_mark import finalize_execution_ledger


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


def _insert_submission(cur, *, run_date, ticker, strategy_id, direction,
                       qty, filled_qty, filled_avg_price, broker_status,
                       entry_price=100.0):
    """Insert one alpaca_submissions row carrying a (reconciled) broker fill.
    filled_avg_price=None / broker_status=None models an unreconciled order."""
    coid = f"b0t-{strategy_id}-{ticker}".replace('/', '-')
    cur.execute("""
        INSERT INTO alpaca_submissions
            (run_date, ticker, strategy_id, direction, qty, entry_price,
             time_in_force, order_class, client_order_id,
             broker_status, filled_qty, filled_avg_price, reconciled_at)
        VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, NOW())
    """, (run_date, ticker, strategy_id, direction, qty, entry_price,
          'day', 'simple', coid, broker_status, filled_qty, filled_avg_price))


def _fetch(cur, *, run_date, ticker, strategy_id):
    cur.execute("""SELECT official_close, exec_ledger_usd
                     FROM alpaca_submissions
                    WHERE run_date=%s AND ticker=%s AND strategy_id=%s""",
                (run_date, ticker, strategy_id))
    return cur.fetchone()


@pytest.mark.integration
def test_long_filled_below_close_positive_ledger(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0LONG', 'ZZB0_STRAT_A'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')

    n = finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    assert n == 1

    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert abs(float(r['official_close']) - 103.5) < 1e-6
    # (103.5 - 99.0) * (+1 * 100) = 450.0  → beat the close
    assert abs(float(r['exec_ledger_usd']) - 450.0) < 1e-6
```

- [ ] **Step 2: Run it to verify it fails**

Run:
```bash
cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | head -1 | cut -d= -f2- | tr -d '"') python3 -m pytest tests/test_sp6_b0_fill_capture.py::test_long_filled_below_close_positive_ledger -v
```
Expected: FAIL — `ImportError: cannot import name 'finalize_execution_ledger'` (function not defined yet).

- [ ] **Step 3: Implement `finalize_execution_ledger` in `src/execution/parity_mark.py`**

Append at the end of `src/execution/parity_mark.py`:

```python
def finalize_execution_ledger(cur, closes: dict, run_date,
                              workspace_id: str = 'default') -> int:
    """Materialize the per-ORDER execution ledger on alpaca_submissions.

    For each filled ENTRY order on run_date, record the official close[T+1]
    benchmark (official_close) and
        exec_ledger_usd = (official_close - filled_avg_price)
                          x (direction_sign x filled_qty)
    where direction_sign = +1 for 'long', -1 for 'short'.

    exec_ledger_usd > 0 ⟺ the fill BEAT the close benchmark (long filled below the
    close / short filled above it) — the §28 beat-close objective.

    Order grain: alpaca_submissions is one row per consolidated broker order, so the
    ledger is intrinsically per-order (NOT per signal/strategy). Sentinel close/orphan
    orders (strategy_id starting with '__') are excluded (entry-only scope). Orders
    with no reconciled fill yet (filled_avg_price NULL) are left NULL and backfilled by
    a later idempotent re-run.

    Args:
        cur:          psycopg2 cursor (caller owns the transaction).
        closes:       dict[ticker -> float] official close per ticker (the same dict
                      finalize_parity_marks receives).
        run_date:     date — matched against alpaca_submissions.run_date.
        workspace_id: unused (alpaca_submissions has no workspace_id); accepted so the
                      call signature mirrors finalize_parity_marks.

    Returns:
        Number of alpaca_submissions rows updated.
    """
    if not closes:
        return 0

    # Normalize the closes keys once so BTC-USD (closes) matches BTC/USD (submission).
    closes_norm = {}
    for _k, _v in closes.items():
        _fv = _safe_float(_v, 'close')
        if _fv is not None:
            closes_norm[_norm_ticker(_k)] = _fv

    cur.execute("""
        SELECT id, strategy_id, ticker, direction,
               filled_avg_price, filled_qty, broker_status
          FROM alpaca_submissions
         WHERE run_date = %s
         ORDER BY id
    """, (run_date,))
    rows = cur.fetchall()
    if not rows:
        return 0

    updated = 0
    for row in rows:
        if hasattr(row, 'keys'):
            row_id = row['id']
            strategy_id = row['strategy_id']
            ticker = row['ticker']
            direction_raw = row['direction']
            avg_raw = row['filled_avg_price']
            qty_raw = row['filled_qty']
            status = row['broker_status']
        else:
            (row_id, strategy_id, ticker, direction_raw,
             avg_raw, qty_raw, status) = row

        # Entry-only scope: skip sentinel close/orphan orders.
        if strategy_id and str(strategy_id).startswith('__'):
            continue
        # Skip explicit non-fill broker states (error / rejected / ...).
        if status is not None and status not in ('filled', 'partial'):
            continue

        avg = _safe_float(avg_raw, 'filled_avg_price')
        qty = _safe_float(qty_raw, 'filled_qty')
        if avg is None or qty is None:
            continue  # no reconciled fill yet → leave NULL (deferrable)

        close = closes_norm.get(_norm_ticker(ticker))
        if close is None:
            continue

        d = str(direction_raw or '').lower()
        if d not in ('long', 'short'):
            continue
        direction_sign = 1 if d == 'long' else -1

        exec_ledger_usd = (close - avg) * (direction_sign * qty)

        cur.execute("""
            UPDATE alpaca_submissions
               SET official_close  = %s,
                   exec_ledger_usd = %s
             WHERE id = %s
        """, (close, exec_ledger_usd, row_id))
        updated += max(cur.rowcount, 0)

    logger.info(
        "[exec_ledger] %d order(s) ledgered for run_date=%s", updated, run_date,
    )
    return updated
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | head -1 | cut -d= -f2- | tr -d '"') python3 -m pytest tests/test_sp6_b0_fill_capture.py::test_long_filled_below_close_positive_ledger -v
```
Expected: PASS

- [ ] **Step 5: Wire the sibling call into the gated 4 PM block**

In `src/execution/engine.py`, change line 1447 from:
```python
                from execution.parity_mark import finalize_parity_marks
```
to:
```python
                from execution.parity_mark import (
                    finalize_parity_marks, finalize_execution_ledger)
```

And immediately after line 1455 (`logger.info(f"Parity marks finalized: {parity_mark_count}")`), inside the same `try`, add:
```python
                ledger_count = finalize_execution_ledger(cur, _closes, run_date)
                logger.info(f"Execution ledger finalized: {ledger_count}")
```

- [ ] **Step 6: Verify the module imports cleanly + commit**

Run:
```bash
cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence
python3 -c "import sys; sys.path.insert(0,'src'); import execution.engine, execution.parity_mark; print('import ok')"
```
Expected: `import ok`

```bash
git add src/execution/parity_mark.py src/execution/engine.py tests/test_sp6_b0_fill_capture.py
git commit -m "feat(sp6-b0): per-order execution ledger (finalize_execution_ledger + wiring)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Sign + edge-case + idempotency tests (and parity regression)

**Files:**
- Test: `tests/test_sp6_b0_fill_capture.py` (append)

- [ ] **Step 1: Append the remaining tests**

```python
@pytest.mark.integration
def test_short_filled_above_close_positive_ledger(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0SHORT', 'ZZB0_STRAT_B'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='short', qty=100, filled_qty=100,
                       filled_avg_price=105.0, broker_status='filled')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    # (103.5 - 105.0) * (-1 * 100) = 150.0  → sold above the close, beat it
    assert abs(float(r['exec_ledger_usd']) - 150.0) < 1e-6


@pytest.mark.integration
def test_long_filled_above_close_negative_ledger(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0LONGNEG', 'ZZB0_STRAT_C'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=105.0, broker_status='filled')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    # (103.5 - 105.0) * (+1 * 100) = -150.0  → paid up vs the close
    assert abs(float(r['exec_ledger_usd']) + 150.0) < 1e-6


@pytest.mark.integration
def test_unreconciled_fill_left_null(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0NULL', 'ZZB0_STRAT_D'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=None,
                       filled_avg_price=None, broker_status=None)
    n = finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    assert n == 0
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert r['official_close'] is None
    assert r['exec_ledger_usd'] is None


@pytest.mark.integration
def test_partial_fill_uses_filled_qty(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0PARTIAL', 'ZZB0_STRAT_E'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=60,
                       filled_avg_price=99.0, broker_status='partial')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    # (103.5 - 99.0) * (1 * 60) = 270.0
    assert abs(float(r['exec_ledger_usd']) - 270.0) < 1e-6


@pytest.mark.integration
def test_crypto_symbol_normalization(db_conn):
    """Submission ticker BTC/USD, closes keyed BTC-USD — normalization (/ → -) must
    match; fractional qty preserved."""
    cur = db_conn.cursor()
    run_date = date.today()
    strat = 'ZZB0_STRAT_F'
    _insert_submission(cur, run_date=run_date, ticker='BTC/USD', strategy_id=strat,
                       direction='long', qty=1, filled_qty=0.5,
                       filled_avg_price=49500.0, broker_status='filled',
                       entry_price=49000.0)
    finalize_execution_ledger(cur, {'BTC-USD': 50000.0}, run_date)
    r = _fetch(cur, run_date=run_date, ticker='BTC/USD', strategy_id=strat)
    # (50000 - 49500) * (1 * 0.5) = 250.0
    assert abs(float(r['exec_ledger_usd']) - 250.0) < 1e-6


@pytest.mark.integration
def test_ticker_absent_from_closes_left_null(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0ABSENT', 'ZZB0_STRAT_G'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')
    n = finalize_execution_ledger(cur, {'SOMETHINGELSE': 103.5}, run_date)
    assert n == 0
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert r['exec_ledger_usd'] is None


@pytest.mark.integration
def test_orphan_close_order_excluded(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0ORPHAN', '__close_orphan__'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')
    n = finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    assert n == 0
    r = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert r['official_close'] is None
    assert r['exec_ledger_usd'] is None


@pytest.mark.integration
def test_idempotent_rerun_identical(db_conn):
    cur = db_conn.cursor()
    run_date = date.today()
    ticker, strat = 'ZZB0IDEM', 'ZZB0_STRAT_H'
    _insert_submission(cur, run_date=run_date, ticker=ticker, strategy_id=strat,
                       direction='long', qty=100, filled_qty=100,
                       filled_avg_price=99.0, broker_status='filled')
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    first = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    finalize_execution_ledger(cur, {ticker: 103.5}, run_date)
    second = _fetch(cur, run_date=run_date, ticker=ticker, strategy_id=strat)
    assert float(second['exec_ledger_usd']) == float(first['exec_ledger_usd'])
    assert float(second['official_close']) == float(first['official_close'])


@pytest.mark.integration
def test_empty_closes_returns_zero(db_conn):
    assert finalize_execution_ledger(db_conn.cursor(), {}, date.today()) == 0
```

- [ ] **Step 2: Run the full B0 suite + the existing parity regression suite**

Run:
```bash
cd /root/.config/superpowers/worktrees/sp6-phase-b0-fill-persistence
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | head -1 | cut -d= -f2- | tr -d '"') python3 -m pytest tests/test_sp6_b0_fill_capture.py tests/test_sp6_parity_mark.py -v
```
Expected: all PASS — the new B0 suite green AND the pre-existing `test_sp6_parity_mark.py` still green (B0 touches a different table + a separate function, so parity behavior is unaffected).

- [ ] **Step 3: Commit**

```bash
git add tests/test_sp6_b0_fill_capture.py
git commit -m "test(sp6-b0): sign + null/partial/crypto/orphan/idempotency edge cases

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review (completed during plan authoring)

**Spec coverage:**
- §3 data model (2 cols on `alpaca_submissions`) → Task 1. ✓
- §4 capture mechanism (`finalize_execution_ledger`: SELECT, sentinel/status/fill/closes/direction skips, ledger formula, UPDATE, idempotent, empty-closes→0) → Task 2 (+ wiring into engine.py). ✓
- §4 sign convention (long/short beat-close) → Task 3 (short-above-close, long-above-close-negative). ✓
- §5 edge cases: unreconciled fill → NULL (Task 3 `test_unreconciled_fill_left_null`); partial → filled_qty (Task 3); fractional crypto + normalization (Task 3); ticker-absent → NULL (Task 3); orphan exclusion (Task 3); idempotent (Task 3); gate-off-inert (structural — only called in the gated block, Task 2 wiring); parity unchanged (writes a different table; Task 3 reruns the parity suite). ✓
- §6 testing — all 9 listed tests mapped to Tasks 2–3 (+ empty-closes). ✓

**Placeholder scan:** none — every step has concrete SQL/Python/commands + expected output.

**Type/name consistency:** `official_close`, `exec_ledger_usd`, `finalize_execution_ledger`, `_norm_ticker`, `_safe_float`, `closes_norm`, `direction_sign` — identical across migration, implementation, wiring, and tests. Helpers (`_insert_submission`, `_fetch`) defined once in Task 2 and reused in Task 3. `_insert_submission` arg names match the `alpaca_submissions` columns.

**Out of scope (confirmed not in any task):** exit/close-side ledger; per-strategy attribution / per-symbol rollup; any consumer/readout; B1 scheduler / B2 Hawkes.
