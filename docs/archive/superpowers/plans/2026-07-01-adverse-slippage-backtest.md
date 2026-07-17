# Adverse Slippage in Backtest (Phase 1a-slip) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Apply an always-unfavourable per-fill slippage inside `simulate_trade` (adverse entry AND exit fills), flag-gated `OPENCLAW_BACKTEST_SLIPPAGE=1` (default OFF → byte-identical), sourced from a recalibrated per-class `INSTRUMENT_COST_BPS`. So `pnl_pct` + true-MTM `daily_marks` reflect ~2·s round-trip drag and Sharpe measures harvestable edge.

**Architecture:** Recalibrate `INSTRUMENT_COST_BPS`; `run_backtest` computes a gated `slippage_bps` and threads it (like `fill_model`) to `_per_bar_simulate` → `simulate_trade`, which computes adverse fills. Spec: `docs/superpowers/specs/2026-07-01-adverse-slippage-backtest-design.md`.

**Tech Stack:** Python 3, pandas, `unittest`.

## Global Constraints
- PATH-SCOPED commit: stage EXACTLY `src/backtest/unified_backtest.py` and `tests/test_adverse_slippage.py`. NEVER `git add -A`/`.`. Live tree has UNRECOVERABLE WIP (`src/strategies/manifest.json`, `registry.py`, untracked `S_*`, `scripts/first_wide_fill_watcher.py`) — do not stage/touch. Verify staged set.
- Do NOT push, restart, or run any backtest / touch the live DB. Tests use synthetic in-memory DataFrames.
- **Flag OFF ⇒ byte-identical** (default). `OPENCLAW_BACKTEST_SLIPPAGE` unset ⇒ `slippage_bps=0.0` everywhere ⇒ `entry_fill==entry_price`, `exit_fill==level`.
- Slippage is ALWAYS unfavourable: for s>0, `pnl_with ≤ pnl_without` for every branch and both directions. `len(daily_marks)==holding_days`; longs `compound(marks)==pnl` (off fills).
- `os` is already imported (line 41). No schema change. Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`. Work from `/root/openclaw`.

---

### Task 1: Adverse slippage in `simulate_trade` (flag-gated, threaded)

**Files:**
- Modify: `src/backtest/unified_backtest.py` — `INSTRUMENT_COST_BPS` (line 77), `simulate_trade` (232-311, full rewrite), `_per_bar_simulate` (def 542 + the `simulate_trade` call ~719), `run_backtest` (log ~825-826 + threading ~843-853)
- Test: `tests/test_adverse_slippage.py` (create)

**Interfaces:**
- `simulate_trade(..., *, include_entry_bar=False, slippage_bps: float = 0.0)` — gains `slippage_bps`.
- `_per_bar_simulate(..., slippage_bps: float = 0.0)` — gains it, forwards to `simulate_trade`.
- `run_backtest` computes `slippage_bps = resolve_cost_model_bps(instrument_class) if OPENCLAW_BACKTEST_SLIPPAGE=='1' else 0.0`, threads to `_per_bar_simulate`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_adverse_slippage.py`:
```python
"""tests/test_adverse_slippage.py — always-adverse per-fill slippage."""
from __future__ import annotations
import math, sys, unittest
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest import unified_backtest as ub  # noqa: E402


def _bars(closes, highs=None, lows=None, start='2020-01-02'):
    idx = pd.bdate_range(start, periods=len(closes))
    highs = highs if highs is not None else [c * 1.001 for c in closes]
    lows = lows if lows is not None else [c * 0.999 for c in closes]
    return pd.DataFrame({'high': highs, 'low': lows, 'close': closes}, index=idx)


class TestSlippage(unittest.TestCase):
    def test_zero_slippage_byte_identical(self):
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        a = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5)
        b = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5, slippage_bps=0.0)
        self.assertEqual(a['pnl_pct'], b['pnl_pct'])
        self.assertEqual(a['exit_price'], b['exit_price'])
        self.assertEqual(a['daily_marks'], b['daily_marks'])
        self.assertEqual(a['holding_days'], b['holding_days'])

    def test_long_slippage_is_adverse(self):
        bars = _bars([102.0, 101.0, 105.0], highs=[102.1, 101.1, 105.1], lows=[101.9, 100.9, 104.9])
        base = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5)
        slip = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 90.0, 200.0, 5, slippage_bps=10.0)
        self.assertLess(slip['pnl_pct'], base['pnl_pct'])            # win shrinks
        self.assertLess(slip['exit_price'], base['exit_price'])      # exit fill worse (lower)
        self.assertEqual(len(slip['daily_marks']), slip['holding_days'])
        comp = 1.0
        for _, r in slip['daily_marks']:
            comp *= (1.0 + r)
        self.assertAlmostEqual(comp - 1.0, slip['pnl_pct'], places=9)  # marks off fills

    def test_short_slippage_is_adverse(self):
        bars = _bars([98.0, 99.0], highs=[98.5, 99.5], lows=[97.5, 98.5])
        base = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 200.0, 1.0, 5)
        slip = ub.simulate_trade(bars, bars.index[0], -1, 100.0, 200.0, 1.0, 5, slippage_bps=10.0)
        self.assertLess(slip['pnl_pct'], base['pnl_pct'])

    def test_roundtrip_drag_two_s(self):
        # long, exit == entry (zero gross move via max_hold at the entry price) -> pnl ~ -2s
        bars = _bars([100.0, 100.0], highs=[100.05, 100.05], lows=[99.95, 99.95])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 1.0, 1e9, 5, slippage_bps=10.0)
        self.assertAlmostEqual(out['pnl_pct'], -2 * 10.0 / 1e4, places=4)

    def test_stop_fills_worse_than_level(self):
        # 2 bars: the walk starts AFTER the entry bar, so bar[1] (low 94 <= stop 95) triggers
        bars = _bars([100.0, 98.0], highs=[100.1, 99.0], lows=[99.9, 94.0])
        out = ub.simulate_trade(bars, bars.index[0], +1, 100.0, 95.0, 200.0, 5, slippage_bps=10.0)
        self.assertEqual(out['exit_reason'], 'stop')
        self.assertLess(out['exit_price'], 95.0)  # long stop fills BELOW the level (adverse)

    def test_cost_bps_recalibrated(self):
        self.assertEqual(ub.resolve_cost_model_bps('equity'), 10.0)
        self.assertEqual(ub.resolve_cost_model_bps('etp'), 10.0)
        self.assertEqual(ub.resolve_cost_model_bps('option'), 5.0)
        self.assertEqual(ub.resolve_cost_model_bps('crypto'), 25.0)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 tests/test_adverse_slippage.py`
Expected: FAIL — `simulate_trade() got an unexpected keyword argument 'slippage_bps'` / `resolve_cost_model_bps('equity') == 1.0 != 10.0`.

- [ ] **Step 3: Recalibrate `INSTRUMENT_COST_BPS`**

Line 77: change to
```python
INSTRUMENT_COST_BPS: dict[str, float] = {"equity": 10.0, "etp": 10.0, "option": 5.0, "crypto": 25.0}
```

- [ ] **Step 4: Rewrite `simulate_trade` with adverse fills (fold last-bar exit into the loop)**

Replace the body of `simulate_trade` (232-311). Add `slippage_bps: float = 0.0` to the signature (after `include_entry_bar`). New body:
```python
    s = float(slippage_bps) / 10000.0
    entry_fill = entry_price * (1.0 + direction * s)  # adverse entry: pay up (long) / sell down (short)
    if include_entry_bar:
        bars_future = bars.loc[bars.index >= entry_date]
    else:
        bars_future = bars.loc[bars.index > entry_date]
    if bars_future.empty:
        return {'exit_date': entry_date, 'exit_price': entry_price,
                'exit_reason': 'end_of_data', 'holding_days': 0, 'pnl_pct': 0.0,
                'daily_marks': []}
    bars_window = bars_future.iloc[:max_hold_days]
    n = len(bars_window)
    daily_marks = []
    prev_mark = entry_fill
    for i, (dt, bar) in enumerate(bars_window.iterrows(), start=1):
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        exit_level, reason = None, None
        if direction > 0:   # long
            if high >= target_1:
                exit_level, reason = float(target_1), 'target'
            elif low <= stop_loss:
                exit_level, reason = float(stop_loss), 'stop'
        else:               # short
            if low <= target_1:
                exit_level, reason = float(target_1), 'target'
            elif high >= stop_loss:
                exit_level, reason = float(stop_loss), 'stop'
        if exit_level is None and i == n:  # last bar, no bracket -> exit at close
            exit_level = close
            reason = 'max_hold' if n == max_hold_days else 'end_of_data'
        if exit_level is not None:  # this bar is the exit -> adverse exit fill
            exit_fill = exit_level * (1.0 - direction * s)
            daily_marks.append((dt, direction * (exit_fill / prev_mark - 1.0)))
            pnl = direction * (exit_fill - entry_fill) / entry_fill
            return {'exit_date': dt, 'exit_price': exit_fill, 'exit_reason': reason,
                    'holding_days': i, 'pnl_pct': pnl, 'daily_marks': daily_marks}
        # interior non-exit bar -> mark to market at the close (no transaction, no slippage)
        daily_marks.append((dt, direction * (close / prev_mark - 1.0)))
        prev_mark = close
    # Unreachable: the i == n branch always exits. Defensive fallback.
    return {'exit_date': bars_window.index[-1], 'exit_price': entry_fill,
            'exit_reason': 'end_of_data', 'holding_days': n, 'pnl_pct': 0.0,
            'daily_marks': daily_marks}
```
Also update the docstring return line to note `exit_price` is the adverse fill and `mark_0 == entry_fill` when `slippage_bps > 0`. Preserve the existing `include_entry_bar` docstring paragraph.

Verify byte-identity by inspection: `s=0` ⇒ `entry_fill=entry_price`, `exit_fill=exit_level`, so every branch's `pnl`/`exit_price`/marks match the pre-change function (target: `(target_1-entry)/entry`; stop likewise; max_hold: `direction*(last_close-entry)/entry`).

- [ ] **Step 5: Thread `slippage_bps` through `_per_bar_simulate`**

In `_per_bar_simulate`'s signature (def at 542) add `slippage_bps: float = 0.0` (keyword-only, alongside `fill_model`). At the `simulate_trade(...)` call (~719) add `slippage_bps=slippage_bps`.

- [ ] **Step 6: Compute + thread the gated slippage in `run_backtest`**

Replace lines 825-826 with:
```python
    _cost_bps = resolve_cost_model_bps(instrument_class)
    _slippage_on = os.environ.get('OPENCLAW_BACKTEST_SLIPPAGE') == '1'
    _slippage_bps = _cost_bps if _slippage_on else 0.0
    _log(f'instrument_class={instrument_class} cost_model_bps={_cost_bps} slippage_applied={_slippage_on}')
```
In the `if _sim_fn is _per_bar_simulate:` block (852-853), add:
```python
        _sim_kwargs['slippage_bps'] = _slippage_bps
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 tests/test_adverse_slippage.py` → `OK` (6 tests). Also `python3 -c "import ast; ast.parse(open('src/backtest/unified_backtest.py').read())"`.

- [ ] **Step 8: Regression — flag OFF byte-identical**

Run `cd /root/openclaw && PYTHONPATH=src python3 tests/test_true_mtm_marks.py` and `python3 tests/test_unified_backtest.py` with `OPENCLAW_BACKTEST_SLIPPAGE` unset → PASS (byte-identical). Report the exact suites + results.

- [ ] **Step 9: Commit (path-scoped)**

```bash
cd /root/openclaw
git add src/backtest/unified_backtest.py tests/test_adverse_slippage.py
git status --porcelain   # MUST show ONLY those two paths
git commit -m "feat(backtest): always-adverse per-fill slippage, flag-gated (Phase 1a-slip)

simulate_trade applies adverse entry/exit fills (entry*(1+dir*s),
level*(1-dir*s)) from recalibrated INSTRUMENT_COST_BPS (equity/etp 10bps),
gated OPENCLAW_BACKTEST_SLIPPAGE (default OFF => byte-identical). pnl + true-MTM
marks reflect ~2*s round-trip drag. Max_hold exit folded into the loop. 6 tests.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review
**Spec coverage:** magnitude (`INSTRUMENT_COST_BPS`) → Step 3 + test 6. Mechanics (adverse fills, marks off fills, last-bar fold) → Step 4 + tests 1-5. Threading + gating → Steps 5-6 (+ flag-OFF via test 1 and Step 8). ✓
**Placeholder scan:** none — full code given. ✓
**Type consistency:** `slippage_bps: float` uniform across `simulate_trade`/`_per_bar_simulate`/`run_backtest`; `exit_price` is the fill; dict key-set unchanged. ✓
