# True-MTM Daily-Returns Fix (Phase 1a) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the flat-P&L smearing in `unified_backtest._portfolio_daily_returns` with true daily mark-to-market emitted by `simulate_trade`, gated `OPENCLAW_TRUE_MTM_MARKS` (default OFF → byte-identical to today). Fixes both inflated Sharpe and understated max-DD.

**Architecture:** `simulate_trade` accumulates a per-day `(date, return)` list during its existing bar walk and returns it as `daily_marks`; `_per_bar_simulate` attaches it to each trade dict only when the flag is ON; `_portfolio_daily_returns` prefers real marks and falls back to the smear when absent. `aggregate_metrics` and the DB write are unchanged. Spec: `docs/superpowers/specs/2026-07-01-true-mtm-daily-returns-fix-design.md`.

**Tech Stack:** Python 3, pandas, numpy, `unittest`.

## Global Constraints

- PATH-SCOPED commit: stage EXACTLY `src/backtest/unified_backtest.py` and `tests/test_true_mtm_marks.py`. NEVER `git add -A`/`.`. Live tree has UNRECOVERABLE WIP (`src/strategies/manifest.json`, `registry.py`, untracked `S_*`, `scripts/first_wide_fill_watcher.py`) — do not stage/touch. Verify staged set.
- Do NOT push, restart, or run any backtest / touch the live DB. Tests are pure (synthetic in-memory DataFrames).
- **Flag OFF ⇒ byte-identical** to current output (the default; deploy is inert until the flag is flipped for a controlled re-backtest).
- `len(daily_marks) == holding_days` for every trade (`[]` when holding_days==0). Longs: `compound(daily_marks) ≈ pnl_pct`. **Shorts: do NOT assert `compound == pnl_pct`** (path-dependent MTM — expected).
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work from `/root/openclaw`.
- Flag name is exactly `OPENCLAW_TRUE_MTM_MARKS`; `daily_marks` items are `(pd.Timestamp, float)`.

---

### Task 1: Emit + consume true daily marks (flag-gated)

**Files:**
- Modify: `src/backtest/unified_backtest.py` — `simulate_trade` (232-291), `_per_bar_simulate` (flag read ~543 + trade-dict assembly ~690-703), `_portfolio_daily_returns` (332-346)
- Test: `tests/test_true_mtm_marks.py` (create)

**Interfaces:**
- Consumes: `simulate_trade(bars, entry_date, direction, entry_price, stop_loss, target_1, max_hold_days, *, include_entry_bar=False) -> dict`. `_portfolio_daily_returns(trades) -> (np.ndarray, list)`. `os` imported at line 41.
- Produces: `simulate_trade` return dict gains key `daily_marks: list[tuple[pd.Timestamp, float]]`. Trade dicts from `_per_bar_simulate` gain `daily_marks` (real list when flag ON, `[]` when OFF).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_true_mtm_marks.py`:
```python
"""tests/test_true_mtm_marks.py — true daily mark-to-market (Phase 1a).
simulate_trade emits a real per-day return path; _portfolio_daily_returns
aggregates real marks (restoring volatility) with a smear fallback."""
from __future__ import annotations
import math, sys, unittest
from pathlib import Path
import pandas as pd, numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest import unified_backtest as ub  # noqa: E402


def _bars(closes, highs=None, lows=None, start='2020-01-02'):
    idx = pd.bdate_range(start, periods=len(closes))
    highs = highs if highs is not None else [c * 1.001 for c in closes]
    lows = lows if lows is not None else [c * 0.999 for c in closes]
    return pd.DataFrame({'high': highs, 'low': lows, 'close': closes}, index=idx)


class TestSimulateTradeMarks(unittest.TestCase):
    def test_len_equals_holding_days_maxhold_long(self):
        # entry 100; closes rise; target/stop never hit; max_hold=3 -> exit last close
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        entry = bars.index[0]  # entry_date; walk starts strictly after
        out = ub.simulate_trade(bars, entry, +1, 100.0, 90.0, 200.0, 3)
        # walk is bars strictly after entry -> 2 bars available here
        self.assertEqual(len(out['daily_marks']), out['holding_days'])
        self.assertTrue(all(isinstance(d, pd.Timestamp) for d, _ in out['daily_marks']))

    def test_target_exit_len_and_exit_price_mark(self):
        # bar after entry has high>=target -> target exit at 105
        bars = _bars([100.0, 103.0], highs=[100.5, 106.0], lows=[99.5, 102.0])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 105.0, 5)
        self.assertEqual(out['exit_reason'], 'target')
        self.assertEqual(out['holding_days'], len(out['daily_marks']))
        # last mark reflects exit at target 105 (from prior mark 100 base): +0.05
        self.assertAlmostEqual(out['daily_marks'][-1][1], 105.0 / 100.0 - 1.0, places=9)

    def test_long_compound_equals_pnl(self):
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5)
        comp = 1.0
        for _, r in out['daily_marks']:
            comp *= (1.0 + r)
        self.assertAlmostEqual(comp - 1.0, out['pnl_pct'], places=9)

    def test_short_len_ok_compound_not_asserted(self):
        bars = _bars([98.0, 99.0], highs=[98.5, 99.5], lows=[97.5, 98.5])
        out = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 200.0, 1.0, 5)
        self.assertEqual(out['holding_days'], len(out['daily_marks']))  # path-dependent; only len checked

    def test_empty_window_zero_marks(self):
        bars = _bars([100.0])  # only the entry bar; nothing strictly after
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 110.0, 5)
        self.assertEqual(out['holding_days'], 0)
        self.assertEqual(out['daily_marks'], [])


class TestPortfolioDailyReturns(unittest.TestCase):
    def _trade(self, entry_str, marks, hold, pnl):
        return {'pnl_pct': pnl, 'holding_days': hold, 'entry_date': pd.Timestamp(entry_str),
                'entry_regime': 'LOW_VOL', 'daily_marks': marks}

    def test_marks_restore_volatility_vs_smear(self):
        # same total pnl, but a volatile daily path -> real std >> smear std
        d = pd.bdate_range('2020-01-02', periods=4)
        volatile = [(d[0], 0.10), (d[1], -0.08), (d[2], 0.06), (d[3], -0.02)]  # nets ~+0.05
        t_marks = self._trade('2020-01-01', volatile, 4, 0.05)
        t_smear = {'pnl_pct': 0.05, 'holding_days': 4, 'entry_date': pd.Timestamp('2020-01-01'),
                   'entry_regime': 'LOW_VOL'}  # no daily_marks -> smear
        dr_marks, _ = ub._portfolio_daily_returns([t_marks])
        dr_smear, _ = ub._portfolio_daily_returns([t_smear])
        self.assertGreater(float(dr_marks.std(ddof=1)), 5 * float(dr_smear.std(ddof=1)))

    def test_smear_fallback_when_no_marks(self):
        t = {'pnl_pct': 0.04, 'holding_days': 4, 'entry_date': pd.Timestamp('2020-01-01'),
             'entry_regime': 'LOW_VOL'}  # no daily_marks key
        dr, dates = ub._portfolio_daily_returns([t])
        self.assertEqual(len(dr), 4)
        for r in dr:
            self.assertAlmostEqual(float(r), 0.04 / 4, places=9)  # flat smear (byte-identical)

    def test_empty_marks_uses_smear(self):
        t = {'pnl_pct': 0.04, 'holding_days': 4, 'entry_date': pd.Timestamp('2020-01-01'),
             'entry_regime': 'LOW_VOL', 'daily_marks': []}  # flag-OFF shape
        dr, _ = ub._portfolio_daily_returns([t])
        self.assertEqual(len(dr), 4)
        self.assertAlmostEqual(float(dr[0]), 0.01, places=9)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 tests/test_true_mtm_marks.py`
Expected: FAIL — `KeyError: 'daily_marks'` (simulate_trade doesn't emit it yet) / the smear-vs-marks assertion errors.

- [ ] **Step 3: Implement `simulate_trade` marks (Component 1)**

Rewrite the body of `simulate_trade` (232-291) to accumulate marks and return them at every branch. Keep all existing keys; add `daily_marks`:
```python
    if bars_future.empty:
        return {'exit_date': entry_date, 'exit_price': entry_price,
                'exit_reason': 'end_of_data', 'holding_days': 0, 'pnl_pct': 0.0,
                'daily_marks': []}
    bars_window = bars_future.iloc[:max_hold_days]
    daily_marks = []
    prev_mark = entry_price
    for i, (dt, bar) in enumerate(bars_window.iterrows(), start=1):
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        if direction > 0:   # long
            if high >= target_1:
                pnl = (target_1 - entry_price) / entry_price
                daily_marks.append((dt, direction * (float(target_1) / prev_mark - 1.0)))
                return {'exit_date': dt, 'exit_price': float(target_1),
                        'exit_reason': 'target', 'holding_days': i, 'pnl_pct': pnl,
                        'daily_marks': daily_marks}
            if low <= stop_loss:
                pnl = (stop_loss - entry_price) / entry_price
                daily_marks.append((dt, direction * (float(stop_loss) / prev_mark - 1.0)))
                return {'exit_date': dt, 'exit_price': float(stop_loss),
                        'exit_reason': 'stop', 'holding_days': i, 'pnl_pct': pnl,
                        'daily_marks': daily_marks}
        else:               # short
            if low <= target_1:
                pnl = (entry_price - target_1) / entry_price
                daily_marks.append((dt, direction * (float(target_1) / prev_mark - 1.0)))
                return {'exit_date': dt, 'exit_price': float(target_1),
                        'exit_reason': 'target', 'holding_days': i, 'pnl_pct': pnl,
                        'daily_marks': daily_marks}
            if high >= stop_loss:
                pnl = (entry_price - stop_loss) / entry_price
                daily_marks.append((dt, direction * (float(stop_loss) / prev_mark - 1.0)))
                return {'exit_date': dt, 'exit_price': float(stop_loss),
                        'exit_reason': 'stop', 'holding_days': i, 'pnl_pct': pnl,
                        'daily_marks': daily_marks}
        # no exit this bar -> mark to close
        daily_marks.append((dt, direction * (close / prev_mark - 1.0)))
        prev_mark = close
    # Neither fired within max_hold -> exit at last close (already the final mark above)
    last_dt = bars_window.index[-1]
    last_close = float(bars_window.iloc[-1]['close'])
    pnl_raw = (last_close - entry_price) / entry_price
    pnl = pnl_raw if direction > 0 else -pnl_raw
    reason = 'max_hold' if len(bars_window) == max_hold_days else 'end_of_data'
    return {'exit_date': last_dt, 'exit_price': last_close,
            'exit_reason': reason, 'holding_days': len(bars_window), 'pnl_pct': pnl,
            'daily_marks': daily_marks}
```
Note: on a target/stop bar we append the EXIT-price mark (not a close mark) and return — so `len==i`. On non-exit bars we append the close mark and advance `prev_mark`. On max_hold/end_of_data the final loop iteration already appended the last close (== exit_price), so no double-append — `len==len(bars_window)==holding_days`.

- [ ] **Step 4: Implement `_per_bar_simulate` flag + attach (Component 2)**

Near the top of `_per_bar_simulate` (by the `_include_fill_bar` setup, ~line 543) add:
```python
    _true_mtm = os.environ.get('OPENCLAW_TRUE_MTM_MARKS') == '1'
```
In the per-trade dict assembly (~690-703; read the actual keys first — `ticker, direction, entry_date, entry_price, exit_date, exit_price, exit_reason, holding_days, pnl_pct, entry_regime, signal_stop, signal_target`), add ONE key:
```python
        'daily_marks': exit_info.get('daily_marks', []) if _true_mtm else [],
```

- [ ] **Step 5: Implement `_portfolio_daily_returns` marks-preference (Component 3)**

In `_portfolio_daily_returns` (332-346), inside the `for t in trades:` loop, AFTER `if hold <= 0: continue`, prefer real marks and keep the smear as fallback:
```python
        marks = t.get('daily_marks')
        if marks:
            for d, r in marks:
                daily_pnls.setdefault(pd.Timestamp(d), []).append(float(r))
            continue
        per_day = float(t['pnl_pct']) / hold
        start = pd.Timestamp(t['entry_date'])
        for i in range(1, hold + 1):
            d = start + pd.Timedelta(days=i)
            daily_pnls.setdefault(d, []).append(per_day)
```
Everything below (`sorted_dates`, the equal-weight average, and all of `aggregate_metrics`) is unchanged.

- [ ] **Step 6: Run the new tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 tests/test_true_mtm_marks.py`
Expected: `OK` (10 tests). Also `python3 -c "import ast; ast.parse(open('src/backtest/unified_backtest.py').read())"` → no output.

- [ ] **Step 7: Regression — flag OFF byte-identical + existing suite**

Run the existing unified-backtest tests with the flag UNSET (default OFF): `cd /root/openclaw && PYTHONPATH=src python3 tests/test_unified_backtest.py` and, if present, `tests/test_unified_backtest_t_plus_1.py`. Expected: PASS (flag OFF ⇒ smear path ⇒ byte-identical). Report exactly which suites ran and their results. Confirm no test asserts the exact key-set of a trade dict (adding `daily_marks` must not break them).

- [ ] **Step 8: Commit (path-scoped)**

```bash
cd /root/openclaw
git add src/backtest/unified_backtest.py tests/test_true_mtm_marks.py
git status --porcelain   # MUST show ONLY those two paths staged
git commit -m "feat(backtest): true daily mark-to-market returns, flag-gated (Phase 1a)

simulate_trade emits a real per-day (date,return) path from its bar walk;
_per_bar_simulate attaches it when OPENCLAW_TRUE_MTM_MARKS=1;
_portfolio_daily_returns aggregates real marks (smear fallback when absent).
Fixes inflated Sharpe + understated max-DD. Flag default OFF => byte-identical.
No migration. 10 tests.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:** Component 1 (simulate_trade marks) → Step 3 + tests 1-5. Component 2 (flag + attach) → Step 4 (+ flag-OFF byte-identical via test `test_empty_marks_uses_smear` + Step 7 regression). Component 3 (`_portfolio_daily_returns` prefer marks / smear fallback) → Step 5 + tests 6-8. Invariants (len==holding_days, long compound==pnl, shorts not asserted, no migration, no consumer break) all covered. DB-reconstruction gap explicitly out of scope (spec). ✓
**Placeholder scan:** none — all code concrete. ✓
**Type consistency:** `daily_marks: list[(pd.Timestamp, float)]` used identically in simulate_trade, the attach, and `_portfolio_daily_returns` (`for d, r in marks`). Flag `OPENCLAW_TRUE_MTM_MARKS`/`_true_mtm` consistent. ✓
