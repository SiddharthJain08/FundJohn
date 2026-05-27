# Executor DTBP Guard + BP Monitoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily executor deploy only what day-trading buying power allows — highest-conviction opens first, the rest skipped cleanly as `skipped_dtbp` — instead of letting Alpaca 403 the tail; plus log buying-power state each run.

**Architecture:** Pure decision helpers (`_dtbp_opening_budget`, `_compute_dtbp_skips`) compute which opening orders to skip given a re-fetched account snapshot; `main()` re-fetches the account at the close→open tier boundary (so freed BP is reflected), computes the skip set once, and skips+records those opens. Closes/covers are never guarded. Behind a default-ON kill-switch `OPENCLAW_DTBP_GUARD`. Spec: `docs/superpowers/specs/2026-05-27-executor-dtbp-guard-design.md`.

**Tech Stack:** Python 3.11, `src/execution/alpaca_executor.py`, `unittest` + `unittest.mock` (existing test pattern in `tests/test_alpaca_executor_cli.py`). No live Alpaca calls in tests.

**Worktree:** create at execution time via `superpowers:using-git-worktrees`; symlink `data/master` and force `OPENCLAW_DIR` to the worktree for any real-run check (per SP handoff). All tests here are mock-only and need neither.

---

### Task 1: `_dtbp_opening_budget(account)` — the budget formula

**Files:**
- Modify: `src/execution/alpaca_executor.py` (add helper after `_exec_priority_for_test`, ~line 731)
- Test: `tests/test_alpaca_executor_dtbp_guard.py` (create)

- [ ] **Step 1: Write the failing test**

```python
"""tests/test_alpaca_executor_dtbp_guard.py — DTBP guard unit tests (mock-only)."""
from __future__ import annotations
import sys, unittest
from pathlib import Path
from unittest.mock import MagicMock
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import alpaca_executor as ae  # noqa: E402


class TestDtbpBudget(unittest.TestCase):
    def test_budget_is_min_of_dtbp_and_regt_floored_at_zero(self):
        # DTBP binds
        self.assertEqual(ae._dtbp_opening_budget(
            {'daytrading_buying_power': 0.0, 'regt_buying_power': 58000.0}), 0.0)
        # regt binds
        self.assertEqual(ae._dtbp_opening_budget(
            {'daytrading_buying_power': 90000.0, 'regt_buying_power': 58000.0}), 58000.0)
        # negative clamps to 0
        self.assertEqual(ae._dtbp_opening_budget(
            {'daytrading_buying_power': -5.0, 'regt_buying_power': 100.0}), 0.0)
        # missing keys → 0 (fail safe: skip all opens rather than over-submit)
        self.assertEqual(ae._dtbp_opening_budget({}), 0.0)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestDtbpBudget -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_dtbp_opening_budget'`

- [ ] **Step 3: Write minimal implementation**

In `src/execution/alpaca_executor.py`, after `_exec_priority_for_test`:

```python
def _dtbp_opening_budget(account: dict) -> float:
    """Max notional of NEW opening orders the account can fund this cycle.

    = max(0, min(daytrading_buying_power, regt_buying_power)).
    DTBP is the intraday day-trade limit Alpaca rejects opens against on a
    PDT account; regt is the Reg-T overnight cap. Missing/negative → 0.0
    (fail safe: skip opens rather than over-submit). No headroom by design.
    """
    dtbp = float(account.get('daytrading_buying_power') or 0.0)
    regt = float(account.get('regt_buying_power') or 0.0)
    return max(0.0, min(dtbp, regt))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestDtbpBudget -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_alpaca_executor_dtbp_guard.py src/execution/alpaca_executor.py
git commit -m "feat(dtbp): _dtbp_opening_budget = min(DTBP, regt_bp) floored at 0"
```

---

### Task 2: `_compute_dtbp_skips(open_orders, account, equity)` — conviction-ranked skip set

**Files:**
- Modify: `src/execution/alpaca_executor.py` (add helper after `_dtbp_opening_budget`)
- Test: `tests/test_alpaca_executor_dtbp_guard.py` (add class)

Notional basis is `equity * pct_nav` (matches the loop's existing `projected`, robust to `notional_usd=NULL`). Rank by conviction descending (`kelly_final` then `pct_nav`); admit while they fit; at the first that does NOT fit, skip it and **all lower-conviction opens** (spec §3.3). Returns a set of `(ticker, strategy_id)` keys to skip.

- [ ] **Step 1: Write the failing test**

```python
class TestComputeDtbpSkips(unittest.TestCase):
    def _open(self, tkr, pct, kelly, sid=None):
        return {'ticker': tkr, 'strategy_id': sid or tkr,
                'direction': 'long', 'pct_nav': pct, 'kelly_final': kelly}

    def test_skips_lowest_conviction_when_budget_tight(self):
        # equity 100k. opens: A 30k(k=.30) B 30k(k=.20) C 30k(k=.10). budget 65k.
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.30, 0.20),
                 self._open('C', 0.30, 0.10)]
        acct = {'daytrading_buying_power': 65000.0, 'regt_buying_power': 999999.0}
        skips = ae._compute_dtbp_skips(opens, acct, equity=100000.0)
        # A(30k) fits→35k left; B(30k) fits→5k left; C(30k) does not → skip C
        self.assertEqual(skips, {('C', 'C')})

    def test_stop_and_skip_all_after_first_nonfit(self):
        # budget only fits the top one; everything below the first non-fit is skipped
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.05, 0.20),
                 self._open('C', 0.30, 0.10)]
        acct = {'daytrading_buying_power': 31000.0, 'regt_buying_power': 999999.0}
        skips = ae._compute_dtbp_skips(opens, acct, equity=100000.0)
        # A(30k) fits→1k left; B(5k) does NOT fit → skip B AND C (all lower-conviction)
        self.assertEqual(skips, {('B', 'B'), ('C', 'C')})

    def test_budget_zero_skips_all(self):
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.30, 0.20)]
        acct = {'daytrading_buying_power': 0.0, 'regt_buying_power': 50000.0}
        self.assertEqual(ae._compute_dtbp_skips(opens, acct, equity=100000.0),
                         {('A', 'A'), ('B', 'B')})

    def test_no_opens_no_skips(self):
        self.assertEqual(ae._compute_dtbp_skips([], {'daytrading_buying_power': 0.0,
                         'regt_buying_power': 0.0}, equity=100000.0), set())

    def test_ample_budget_skips_nothing(self):
        opens = [self._open('A', 0.30, 0.30), self._open('B', 0.30, 0.20)]
        acct = {'daytrading_buying_power': 999999.0, 'regt_buying_power': 999999.0}
        self.assertEqual(ae._compute_dtbp_skips(opens, acct, equity=100000.0), set())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestComputeDtbpSkips -v`
Expected: FAIL — `AttributeError: ... '_compute_dtbp_skips'`

- [ ] **Step 3: Write minimal implementation**

```python
def _open_conviction(o: dict) -> float:
    """Conviction rank for an opening order: kelly_final, else pct_nav."""
    k = o.get('kelly_final')
    if k is not None:
        try:
            return abs(float(k))
        except (TypeError, ValueError):
            pass
    return abs(float(o.get('pct_nav') or 0.0))


def _compute_dtbp_skips(open_orders: list[dict], account: dict, equity: float) -> set:
    """Return the set of (ticker, strategy_id) opening orders to skip because
    they exceed the day-trade/Reg-T budget. Highest-conviction opens are
    funded first; at the first open that does not fit the remaining budget,
    it and ALL lower-conviction opens are skipped (spec §3.3). Notional basis
    = equity * pct_nav."""
    budget = _dtbp_opening_budget(account)
    ranked = sorted(open_orders, key=_open_conviction, reverse=True)
    skips: set = set()
    remaining = budget
    cutoff = False
    for o in ranked:
        key = (o.get('ticker'), o.get('strategy_id') or 'unknown')
        notional = float(equity) * float(o.get('pct_nav') or 0.0)
        if cutoff or notional > remaining:
            cutoff = True
            skips.add(key)
        else:
            remaining -= notional
    return skips
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestComputeDtbpSkips -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add src/execution/alpaca_executor.py tests/test_alpaca_executor_dtbp_guard.py
git commit -m "feat(dtbp): _compute_dtbp_skips — conviction-ranked stop-and-skip-rest"
```

---

### Task 3: `_dtbp_guard_enabled()` — default-ON kill-switch

**Files:**
- Modify: `src/execution/alpaca_executor.py`
- Test: `tests/test_alpaca_executor_dtbp_guard.py`

- [ ] **Step 1: Write the failing test**

```python
import os
from unittest.mock import patch

class TestDtbpGate(unittest.TestCase):
    def test_default_on_when_unset(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCLAW_DTBP_GUARD', None)
            self.assertTrue(ae._dtbp_guard_enabled())

    def test_off_when_zero(self):
        with patch.dict(os.environ, {'OPENCLAW_DTBP_GUARD': '0'}):
            self.assertFalse(ae._dtbp_guard_enabled())

    def test_on_when_one(self):
        with patch.dict(os.environ, {'OPENCLAW_DTBP_GUARD': '1'}):
            self.assertTrue(ae._dtbp_guard_enabled())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestDtbpGate -v`
Expected: FAIL — `AttributeError: ... '_dtbp_guard_enabled'`

- [ ] **Step 3: Write minimal implementation**

```python
def _dtbp_guard_enabled() -> bool:
    """Kill-switch, default ON. Set OPENCLAW_DTBP_GUARD=0 to disable (then
    the executor behaves byte-identically to the pre-guard path)."""
    return os.environ.get('OPENCLAW_DTBP_GUARD', '1') != '0'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestDtbpGate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/alpaca_executor.py tests/test_alpaca_executor_dtbp_guard.py
git commit -m "feat(dtbp): OPENCLAW_DTBP_GUARD kill-switch (default ON)"
```

---

### Task 4: `_record_dtbp_skip()` — persist a skipped_dtbp audit row

**Files:**
- Modify: `src/execution/alpaca_executor.py` (add after `record_submission`, ~line 388)
- Test: `tests/test_alpaca_executor_dtbp_guard.py`

Reuses `record_submission` with a synthetic result so the row lands with `alpaca_status='skipped_dtbp'`, NULL order id, `alpaca_error='dtbp_budget_exhausted'` — `already_executed()` still treats it as not-executed (a later cycle with restored capacity can pick it up).

- [ ] **Step 1: Write the failing test**

```python
class TestRecordDtbpSkip(unittest.TestCase):
    def test_writes_skipped_dtbp_row(self):
        conn = MagicMock(); cur = MagicMock(); conn.cursor.return_value = cur
        order = {'ticker': 'AMAT', 'strategy_id': 'S_x', 'direction': 'long',
                 'pct_nav': 0.0644, 'order_class': 'bracket'}
        ae._record_dtbp_skip(conn, '2026-05-27', order, equity=110000.0)
        cur.execute.assert_called_once()
        params = cur.execute.call_args[0][1]
        self.assertIn('skipped_dtbp', params)            # alpaca_status
        self.assertIn('dtbp_budget_exhausted', params)   # alpaca_error
        conn.commit.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestRecordDtbpSkip -v`
Expected: FAIL — `AttributeError: ... '_record_dtbp_skip'`

- [ ] **Step 3: Write minimal implementation**

```python
def _record_dtbp_skip(conn, run_date, order, equity: float) -> None:
    """Persist a skipped-for-buying-power order as an audit row
    (alpaca_status='skipped_dtbp', no order id) via record_submission."""
    notional = round(float(equity) * float(order.get('pct_nav') or 0.0), 2)
    resp = {
        'status':   'skipped_dtbp',
        'qty':      0,
        'notional': notional,
        'order_id': None,
        'http':     None,
        'reason':   'dtbp_budget_exhausted',
        'entry':    order.get('entry'),
    }
    record_submission(
        conn, run_date, order, resp,
        order.get('tif') or 'day',
        order.get('order_class') or 'simple',
        '',
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestRecordDtbpSkip -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/alpaca_executor.py tests/test_alpaca_executor_dtbp_guard.py
git commit -m "feat(dtbp): _record_dtbp_skip writes skipped_dtbp audit row"
```

---

### Task 5: Wire the guard into `main()`

**Files:**
- Modify: `src/execution/alpaca_executor.py` — `main()` submit loop (~line 1469 for re-fetch hook; ~line 1505 loop body)

No new behavior beyond wiring the tested helpers. The skip set is computed once, lazily, at the first opening-tier order (so the re-fetched account reflects BP freed by the already-submitted closes).

- [ ] **Step 1: Add guard state before the loop**

Immediately before `for order in orders:` (after the `flip_tickers`/`failed_flip_closes` setup, ~line 1500), insert:

```python
    # DTBP guard: computed lazily at the first opening-tier order so the
    # re-fetched account reflects buying power freed by the closes above.
    guard_on = _dtbp_guard_enabled()
    dtbp_skip_keys: set = set()
    dtbp_ready = False
```

- [ ] **Step 2: Compute the skip set at the close→open boundary**

At the TOP of the loop body (first lines inside `for order in orders:`), before the existing `sid = ...`/`ticker = ...` lines, insert:

```python
        if guard_on and not dtbp_ready and _exec_priority_for_test(order)[0] >= 2:
            acct2 = _fetch_account_state(sess)
            open_orders = [o for o in orders if _exec_priority_for_test(o)[0] >= 2]
            dtbp_skip_keys = _compute_dtbp_skips(open_orders, acct2, equity)
            dtbp_ready = True
            if dtbp_skip_keys:
                log(f'[dtbp-guard] budget=${_dtbp_opening_budget(acct2):,.0f} '
                    f'(DTBP=${acct2.get("daytrading_buying_power",0):,.0f} '
                    f'regt=${acct2.get("regt_buying_power",0):,.0f}); '
                    f'skipping {len(dtbp_skip_keys)} low-conviction opens')
```

- [ ] **Step 3: Skip + record flagged opens**

Immediately after the existing `sid = order.get('strategy_id') or 'unknown'` and `ticker = order.get('ticker') or '???'` lines, insert (before the flip-pair `is_flip_close` block):

```python
        if guard_on and (ticker, sid) in dtbp_skip_keys:
            _record_dtbp_skip(conn, run_date, order, equity)
            skipped.append({'ticker': ticker, 'reason': 'dtbp_budget_exhausted'})
            continue
```

- [ ] **Step 4: Update the pre-loop log line (truth-in-advertising)**

Change the existing line (~1477):
```python
    log(f'[executor] session={session}, submitting {len(orders)} orders (pure sizer output — no executor-side cap)')
```
to:
```python
    log(f'[executor] session={session}, {len(orders)} orders queued '
        f'(DTBP guard {"ON" if _dtbp_guard_enabled() else "OFF"})')
```

- [ ] **Step 5: Run the full executor suite — no regressions**

Run: `python3 -m pytest tests/test_alpaca_executor_cli.py tests/test_alpaca_executor_bracket_recompute.py tests/test_alpaca_executor_ext_hours.py tests/test_alpaca_executor_dtbp_guard.py -v`
Expected: all PASS (existing executor tests unchanged; guard tests green).

- [ ] **Step 6: Commit**

```bash
git add src/execution/alpaca_executor.py
git commit -m "feat(dtbp): wire DTBP guard into executor main() loop"
```

---

### Task 6: Report skipped_dtbp in the executor summary

**Files:**
- Modify: `src/execution/alpaca_executor.py` — `_post_executor_summary` (~line 1583)
- Test: `tests/test_alpaca_executor_dtbp_guard.py`

- [ ] **Step 1: Write the failing test** (assert the helper that builds the summary line)

```python
class TestSummaryBpLine(unittest.TestCase):
    def test_bp_skip_line_counts_dtbp_skips(self):
        skipped = [{'ticker': 'AMAT', 'reason': 'dtbp_budget_exhausted'},
                   {'ticker': 'AMD', 'reason': 'dtbp_budget_exhausted'},
                   {'ticker': 'XYZ', 'reason': 'already executed'}]
        line = ae._dtbp_summary_line(skipped)
        self.assertIn('2', line)              # two BP skips
        self.assertIn('buying-power', line.lower())

    def test_no_line_when_no_bp_skips(self):
        self.assertEqual(ae._dtbp_summary_line(
            [{'ticker': 'X', 'reason': 'already executed'}]), '')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestSummaryBpLine -v`
Expected: FAIL — `AttributeError: ... '_dtbp_summary_line'`

- [ ] **Step 3: Implement the helper and call it in `_post_executor_summary`**

Add at module scope:
```python
def _dtbp_summary_line(skipped: list) -> str:
    """One-line buying-power-skip summary, or '' if none were skipped for BP."""
    bp = [s for s in skipped if s.get('reason') == 'dtbp_budget_exhausted']
    if not bp:
        return ''
    tickers = ', '.join(s['ticker'] for s in bp[:10])
    return f'⛔ {len(bp)} opens skipped for buying-power (lowest-conviction): {tickers}'
```

In `_post_executor_summary`, after the `lines = [ ... ]` block is built (and before the Discord POST), append:
```python
    _bp_line = _dtbp_summary_line(skipped)
    if _bp_line:
        lines.append(_bp_line)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestSummaryBpLine -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/alpaca_executor.py tests/test_alpaca_executor_dtbp_guard.py
git commit -m "feat(dtbp): surface skipped_dtbp count in executor #trade-reports summary"
```

---

### Task 7: BP-snapshot monitoring

**Files:**
- Modify: `src/execution/alpaca_executor.py` — add `_bp_snapshot` helper + call it in `main()` after `account = _fetch_account_state(sess)` (~line 1469)
- Test: `tests/test_alpaca_executor_dtbp_guard.py`

- [ ] **Step 1: Write the failing test**

```python
import tempfile, csv, os as _os

class TestBpSnapshot(unittest.TestCase):
    def test_appends_csv_row_with_header(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bp_snapshots.csv'
            acct = {'equity': 110000.0, 'daytrading_buying_power': 0.0,
                    'regt_buying_power': 58000.0, 'daytrade_count': 64, 'multiplier': 4}
            ae._bp_snapshot(acct, '2026-05-27', path=str(path))
            ae._bp_snapshot(acct, '2026-05-27', path=str(path))  # append, no dup header
            rows = list(csv.reader(open(path)))
            self.assertEqual(rows[0][0], 'timestamp')   # header once
            self.assertEqual(len(rows), 3)              # header + 2 data rows
            self.assertIn('64', rows[1])                # daytrade_count present
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestBpSnapshot -v`
Expected: FAIL — `AttributeError: ... '_bp_snapshot'`

- [ ] **Step 3: Implement**

```python
def _bp_snapshot(account: dict, run_date, path: str | None = None) -> None:
    """Append one buying-power snapshot row to logs/bp_snapshots.csv
    (append-only; header written once). Best-effort — never raises into
    the executor."""
    import csv as _csv, datetime as _dt
    try:
        p = Path(path) if path else (ROOT / 'logs' / 'bp_snapshots.csv')
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with open(p, 'a', newline='') as f:
            w = _csv.writer(f)
            if new:
                w.writerow(['timestamp', 'run_date', 'equity',
                            'daytrading_buying_power', 'regt_buying_power',
                            'daytrade_count', 'multiplier'])
            w.writerow([
                _dt.datetime.utcnow().isoformat(), str(run_date),
                account.get('equity'), account.get('daytrading_buying_power'),
                account.get('regt_buying_power'), account.get('daytrade_count'),
                account.get('multiplier'),
            ])
    except Exception as e:
        log(f'[dtbp-guard] bp_snapshot write failed: {e}')
```

> **Note for the implementer:** `_fetch_account_state` (in `alpaca_trader.py`) currently narrows the Alpaca `/v2/account` response to a fixed key set that does **not** include `daytrade_count`. Add `'daytrade_count'` to its `state` defaults (default `0`) and to the copy loop's key tuple so the snapshot captures it. One-line additions in each place; no behavior change for existing callers.

In `main()`, right after `regt_bp = account['regt_buying_power']` (~line 1469):
```python
    _bp_snapshot(account, run_date)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_alpaca_executor_dtbp_guard.py::TestBpSnapshot -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/alpaca_executor.py src/execution/alpaca_trader.py tests/test_alpaca_executor_dtbp_guard.py
git commit -m "feat(dtbp): per-run bp_snapshots.csv + capture daytrade_count"
```

---

### Task 8: Document the gate + final verification

**Files:**
- Modify: `.env.example` (document the new gate)
- Verify: full executor + sizer regression suites

- [ ] **Step 1: Document the env var**

Add to `.env.example` (near other `OPENCLAW_*` gates):
```
# Executor day-trading-buying-power guard. Default ON. Set =0 to disable
# (executor reverts to submitting the full sized set; Alpaca then rejects
# opens beyond available buying power). See docs/superpowers/specs/2026-05-27-executor-dtbp-guard-design.md
OPENCLAW_DTBP_GUARD=1
```

- [ ] **Step 2: Guard-OFF byte-identity check (manual reasoning + grep)**

Confirm every guard branch is keyed on `guard_on` / `_dtbp_guard_enabled()` and the only unconditional additions are the `_bp_snapshot` call (pure logging) and the summary line (empty when no BP skips). With `OPENCLAW_DTBP_GUARD=0`, `dtbp_skip_keys` stays empty and the skip branch never fires → submission behavior identical to pre-guard.

Run: `grep -n "guard_on\|_dtbp_guard_enabled\|dtbp_skip_keys\|_compute_dtbp_skips" src/execution/alpaca_executor.py`
Expected: every skip/compute reference sits under a `guard_on` guard.

- [ ] **Step 3: Full regression**

Run: `python3 -m pytest tests/test_alpaca_executor_cli.py tests/test_alpaca_executor_bracket_recompute.py tests/test_alpaca_executor_ext_hours.py tests/test_alpaca_executor_dtbp_guard.py -v`
Expected: all PASS.

- [ ] **Step 4: Dry-run smoke (no orders fired)**

Run (in the worktree, with env loaded — does NOT submit because `--dry-run`):
`python3 -m execution.alpaca_executor --date $(date +%F) --dry-run`
Expected: logs `DTBP guard ON`, computes a budget line if any opens, exits 0, **submits nothing**.

- [ ] **Step 5: Commit**

```bash
git add .env.example
git commit -m "docs(dtbp): document OPENCLAW_DTBP_GUARD in .env.example"
```

---

## Post-merge (operator, on VPS)

- Regenerate the integrity manifest: `./scripts/regen-integrity-manifest.sh` (alpaca_executor.py is manifest-covered). **Do NOT commit the manifest.**
- Append `OPENCLAW_DTBP_GUARD=1` to prod `.env` via `printf >>` only if you want it explicit (absent already defaults ON). Do not read `.env` into context; do not create `.env.bak`.
- Watch `logs/bp_snapshots.csv` + the next 2–3 daily cycles: confirm zero 403s, clean `skipped_dtbp` reporting, and whether DTBP recovers as the 05-21/22 day-trades roll off (~05-28/29). If DTBP stays pinned at 0, escalate to Gap 2 (RTH-redeploy churn — deferred).

## Out of scope (per spec §7)

RTH-redeploy churn reduction; re-enabling extended-hours redeploys; flattening the stale SPY options; any sizer math change (λ=1.5 stands).
