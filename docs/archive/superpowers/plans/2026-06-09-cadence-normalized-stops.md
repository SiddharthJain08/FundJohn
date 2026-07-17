# √cadence-normalized stops/take-profits — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Normalize each contributing strategy's stop/take-profit gap to a single-day equivalent (÷√cadence) at candidate-construction in the live sizer, so corroborated brackets are horizon-consistent with the daily-rebalanced book — gated, default-OFF, combine functions and backtest untouched.

**Architecture:** A pure helper in `bracket_stacking.py` shrinks each bracket gap-from-entry by `1/√cadence_days` (the bracket analogue of `daily_weight = effective_sharpe/√cadence_days`). `_sharpe_cadence_path` calls it when building each bracket candidate, behind a new gate `OPENCLAW_STRATEGY_CADENCE_STOP_NORM`. The existing combine logic (`stacked_bracket` tightest-stop/capped-sum-TP, and the legacy `_select_bracket`) receives already-normalized levels and is unchanged.

**Tech Stack:** Python 3, pytest (`pythonpath = src` in `pytest.ini`).

**Spec:** `docs/superpowers/specs/2026-06-09-cadence-normalized-stops-design.md`

---

### Task 1: Pure helper `daily_normalized_levels` in `bracket_stacking.py`

**Files:**
- Modify: `src/execution/bracket_stacking.py` (add one module-level function; `math` and `_finite` already exist there)
- Test: `tests/test_bracket_stacking.py` (append tests; file already has `sys.path` shim + imports `bracket_stacking as bs`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bracket_stacking.py` (it already does `import execution.bracket_stacking as bs` — confirm the alias; if it imports as a different alias, match it):

```python
class TestDailyNormalizedLevels:
    def test_long_scales_gap_by_inv_sqrt_cadence(self):
        # cadence 4 -> f = 1/2. long: entry 100, stop 95, t1 110
        stop, t1, t2 = bs.daily_normalized_levels(100.0, 95.0, 110.0, None, 4.0)
        assert math.isclose(stop, 97.5)
        assert math.isclose(t1, 105.0)
        assert t2 is None

    def test_short_scales_gap_by_inv_sqrt_cadence(self):
        # cadence 9 -> f = 1/3. short: entry 100, stop 106 (above), t1 90 (below)
        stop, t1, t2 = bs.daily_normalized_levels(100.0, 106.0, 90.0, None, 9.0)
        assert math.isclose(stop, 100.0 + (106.0 - 100.0) / 3.0)   # 102.0
        assert math.isclose(t1, 100.0 + (90.0 - 100.0) / 3.0)      # 96.6667

    def test_cadence_one_is_noop(self):
        assert bs.daily_normalized_levels(100.0, 95.0, 110.0, 115.0, 1.0) == (95.0, 110.0, 115.0)

    def test_monthly_cadence_shrinks_gap(self):
        stop, _, _ = bs.daily_normalized_levels(100.0, 90.0, 110.0, None, 21.0)
        assert math.isclose(stop, 100.0 - 10.0 / math.sqrt(21.0))

    def test_pct_identity_matches_weight_scaling(self):
        # normalized stop_pct == raw stop_pct / sqrt(cadence)
        e, raw_stop, c = 200.0, 180.0, 16.0
        stop, _, _ = bs.daily_normalized_levels(e, raw_stop, None, None, c)
        raw_pct = (e - raw_stop) / e
        norm_pct = (e - stop) / e
        assert math.isclose(norm_pct, raw_pct / math.sqrt(c))

    def test_bad_entry_passes_all_levels_through(self):
        assert bs.daily_normalized_levels(None, 95.0, 110.0, None, 4.0) == (95.0, 110.0, None)
        assert bs.daily_normalized_levels(0.0, 95.0, 110.0, None, 4.0) == (95.0, 110.0, None)
        assert bs.daily_normalized_levels(float('nan'), 95.0, 110.0, None, 4.0) == (95.0, 110.0, None)

    def test_per_level_finite_guard(self):
        # None/NaN levels pass through; finite levels still normalize (cadence 4 -> f=0.5)
        stop, t1, t2 = bs.daily_normalized_levels(100.0, None, float('nan'), 120.0, 4.0)
        assert stop is None
        assert math.isnan(t1)
        assert math.isclose(t2, 110.0)   # 100 + (120-100)*0.5

    def test_cadence_floored_at_one(self):
        # cadence 0 or None -> treated as 1 -> no-op
        assert bs.daily_normalized_levels(100.0, 95.0, 110.0, None, 0.0) == (95.0, 110.0, None)
        assert bs.daily_normalized_levels(100.0, 95.0, 110.0, None, None) == (95.0, 110.0, None)
```

Confirm `import math` is present at the top of `tests/test_bracket_stacking.py` (it is — existing tests use `math.isclose`). If the existing import alias is not `bs`, adjust the calls accordingly.

- [ ] **Step 2: Run tests to verify they fail**

Run: `nice -n 19 python3 -m pytest tests/test_bracket_stacking.py::TestDailyNormalizedLevels -q`
Expected: FAIL — `AttributeError: module 'execution.bracket_stacking' has no attribute 'daily_normalized_levels'`.

- [ ] **Step 3: Implement the helper**

Add to `src/execution/bracket_stacking.py` (after the existing `_to_fractions` / `_pick_top_sharpe` helpers, before `stacked_bracket`). `math`, `os`, and `_finite` are already defined at module top:

```python
def daily_normalized_levels(entry, stop, t1, t2, cadence_days):
    """Shrink each finite bracket gap-from-entry to its single-day equivalent by
    1/sqrt(max(1, cadence_days)) — the bracket analogue of strategy_weights'
    daily_weight = effective_sharpe / sqrt(cadence_days).

    Direction-agnostic: the signed gap (level - entry) is scaled, so both longs
    and shorts shrink toward entry. Returns (stop, t1, t2). A level that is
    None/non-finite passes through unchanged; ALL levels pass through unchanged
    when `entry` is None/non-finite/<= 0 (no valid anchor to scale around).
    cadence_days is floored at 1 (a daily strategy is a no-op).
    """
    if not _finite(entry) or float(entry) <= 0:
        return stop, t1, t2
    e = float(entry)
    f = 1.0 / math.sqrt(max(1.0, float(cadence_days or 1.0)))
    def _norm(x):
        return e + (float(x) - e) * f if _finite(x) else x
    return _norm(stop), _norm(t1), _norm(t2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `nice -n 19 python3 -m pytest tests/test_bracket_stacking.py -q`
Expected: PASS (the new `TestDailyNormalizedLevels` class + the 13 pre-existing tests = all green).

- [ ] **Step 5: Commit**

```bash
git add src/execution/bracket_stacking.py tests/test_bracket_stacking.py
git commit -m "feat(sizer): daily_normalized_levels — ÷√cadence bracket-gap helper

Pure helper that shrinks each bracket gap-from-entry to a single-day
equivalent (1/√cadence_days), mirroring daily_weight scaling. Direction-
agnostic; finite-guards entry and each level. No caller yet (Task 2).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire normalization into `_sharpe_cadence_path` behind the gate

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (gate read ~line 886; candidate-construction block ~line 1000–1008)
- Test: `tests/test_sizer_cadence_stop_norm.py` (new)

**Context for the implementer:**
- `cadence_by_strat` is ALREADY built at ~line 881: `{r['strategy_id']: float(r['cadence_days']) for r in rows}`. Reuse it — do NOT rebuild.
- Gate idiom: `_ortho_enabled('GATE')` returns `os.environ.get('GATE') == '1'` (line 52).
- `_size_scalar_on = os.environ.get('OPENCLAW_STRATEGY_SIZE_SCALAR') == '1'` is at ~line 886 — add the new gate read right after it.
- `bracket_stacking` is imported lazily elsewhere as `from execution import bracket_stacking as _bs`.
- The candidate dict is appended to `ticker_meta[tkr]['brackets']` and consumed ONLY by `_choose_bracket` (line ~1238). `entry` stays raw; only `stop`/`t1`/`t2` are normalized.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sizer_cadence_stop_norm.py`:

```python
"""tests/test_sizer_cadence_stop_norm.py — √cadence stop/TP normalization.

Drives size_positions -> _sharpe_cadence_path with the mock harness from
test_sizer_sp6_eod_mode.py (load_current patched, loaders + broker
monkeypatched), controlling cadence_days via the fake weights row. Asserts
the emitted order's stop/t1 are √cadence-normalized when the gate is ON and
byte-identical (raw) when OFF.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer


def _sig(sid='S1', ticker='AAPL', direction=1, entry=100.0, stop=95.0, t1=110.0):
    return {
        'signal_id': hash((sid, ticker, direction)),
        'strategy_id': sid, 'ticker': ticker, 'direction': direction,
        'entry_price': entry, 'stop_loss': stop, 'target_1': t1, 'target_2': None,
    }


def _account(equity=100_000):
    return {'equity': equity, 'regt_buying_power': 2 * equity,
            'long_market_value': 0, 'cash': equity}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 100,
            'position_circuit_breaker_pct': 0.02}


# cadence_days 4.0 -> f = 1/2; effective_sharpe high enough to clear min-cum-sharpe gate
def _weights_row(cadence=4.0):
    return {'strategy_id': 'S1', 'daily_weight': 5.0,
            'effective_sharpe': 5.0, 'cadence_days': cadence}


def _drive(monkeypatch, gate_on: bool, cadence=4.0):
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    monkeypatch.delenv('OPENCLAW_STRATEGY_FOLD', raising=False)
    if gate_on:
        monkeypatch.setenv('OPENCLAW_STRATEGY_CADENCE_STOP_NORM', '1')
    else:
        monkeypatch.delenv('OPENCLAW_STRATEGY_CADENCE_STOP_NORM', raising=False)
    # active-window empty -> path falls through to today's `signals`
    monkeypatch.setattr(_sizer, '_load_active_window_signals', lambda *a, **k: [])
    # no broker positions -> full target emits an opening order with a bracket
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    with _mock.patch('execution.strategy_weights.load_current',
                     return_value=[_weights_row(cadence)]):
        return _sizer.size_positions(
            signals=[_sig('S1')], account_state=_account(),
            regime={'state': 'LOW_VOL'}, run_date=date(2026, 5, 12),
            strategy_state={}, regime_params=_params(), confirmer=None,
        )


def _order_for(orders, ticker='AAPL'):
    hits = [o for o in orders if o.get('ticker') == ticker and not o.get('close_only')]
    assert hits, f'expected an opening order for {ticker}, got {orders}'
    return hits[0]


def test_gate_off_is_byte_identical_raw_levels(monkeypatch):
    orders = _drive(monkeypatch, gate_on=False)
    o = _order_for(orders)
    assert o['entry'] == 100.0
    assert o['stop'] == 95.0
    assert o['t1'] == 110.0


def test_gate_on_normalizes_stop_and_t1(monkeypatch):
    # cadence 4 -> f = 0.5: stop 95 -> 97.5, t1 110 -> 105.0; entry unchanged
    orders = _drive(monkeypatch, gate_on=True, cadence=4.0)
    o = _order_for(orders)
    assert o['entry'] == 100.0
    assert abs(o['stop'] - 97.5) < 1e-6
    assert abs(o['t1'] - 105.0) < 1e-6


def test_gate_on_daily_cadence_is_noop(monkeypatch):
    orders = _drive(monkeypatch, gate_on=True, cadence=1.0)
    o = _order_for(orders)
    assert o['stop'] == 95.0
    assert o['t1'] == 110.0
```

NOTE for implementer: this drives the real emission path. If the opening order does not emit (e.g. a min-cumulative-sharpe or circuit-breaker gate drops the ticker), raise `effective_sharpe`/`daily_weight` in `_weights_row` until it emits, or add the missing mock seam — do NOT weaken the assertions. If end-to-end driving proves too brittle, fall back to monkeypatching `_sizer._choose_bracket` with a passthrough spy that captures its `candidates` argument and assert the captured candidate's `stop`/`t1` instead (same normalized values). Keep `test_gate_off_is_byte_identical_raw_levels` as the byte-identical guard either way.

- [ ] **Step 2: Run test to verify it fails**

Run: `nice -n 19 python3 -m pytest tests/test_sizer_cadence_stop_norm.py -q`
Expected: `test_gate_off_*` PASSES (gate unset → no behavior change yet), `test_gate_on_*` FAIL (stop still 95.0, not 97.5 — wiring not present).

- [ ] **Step 3: Add the gate read**

In `src/execution/regime_blended_sizer.py`, right after `_size_scalar_on = os.environ.get('OPENCLAW_STRATEGY_SIZE_SCALAR') == '1'` (~line 886):

```python
    _cadence_stop_norm_on = _ortho_enabled('OPENCLAW_STRATEGY_CADENCE_STOP_NORM')
    if _cadence_stop_norm_on:
        from execution import bracket_stacking as _bs
```

- [ ] **Step 4: Normalize at candidate construction**

Replace the existing `ticker_meta[tkr]['brackets'].append({...})` block (~line 1000–1008) with:

```python
        _b_entry = s.get('entry_price')
        _b_stop  = s.get('stop_loss')
        _b_t1    = s.get('target_1')
        _b_t2    = s.get('target_2')
        if _cadence_stop_norm_on:
            # Normalize each bracket gap-from-entry to a single-day equivalent
            # (÷√cadence), mirroring daily_weight = sharpe/√cadence, so corroborated
            # stops/TPs share the horizon of the daily-rebalanced book. Entry (the
            # fill anchor) is never scaled. Combine logic downstream is unchanged.
            _b_stop, _b_t1, _b_t2 = _bs.daily_normalized_levels(
                _b_entry, _b_stop, _b_t1, _b_t2, cadence_by_strat.get(sid, 1.0))
        ticker_meta[tkr]['brackets'].append({
            'sid':        sid,
            'direction':  d,
            'weight':     weight_by_strat[sid],   # raw conviction: bracket-leader by sharpe, NOT size_scalar
            'entry':      _b_entry,
            'stop':       _b_stop,
            't1':         _b_t1,
            't2':         _b_t2,
        })
```

- [ ] **Step 5: Run the new test to verify it passes**

Run: `nice -n 19 python3 -m pytest tests/test_sizer_cadence_stop_norm.py -q`
Expected: all 3 PASS.

- [ ] **Step 6: Run regression — sizer + bracket suites stay green**

Run: `nice -n 19 python3 -m pytest tests/test_bracket_stacking.py tests/test_bracket_stacking_sizer.py tests/test_regime_blended_sizer_live.py tests/test_sizer_sp6_eod_mode.py tests/test_sizer_dust_floor.py tests/test_sizer_per_ticker_cap.py tests/test_orthogonalization_sizer.py tests/test_sizer_cadence_stop_norm.py -q`
Expected: all PASS, 0 failures. (Any pre-existing failure unrelated to this change should be reported, not "fixed" by weakening.)

- [ ] **Step 7: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/test_sizer_cadence_stop_norm.py
git commit -m "feat(sizer): √cadence-normalize stops/TPs at candidate construction (gated)

When OPENCLAW_STRATEGY_CADENCE_STOP_NORM=1, each contributing strategy's
stop/t1/t2 gap-from-entry is shrunk by 1/√cadence_days before the bracket
combine, so corroborated stops/TPs are horizon-consistent with the daily-
rebalanced book. Reuses the existing cadence_by_strat map and the same
cadence value that scales daily_weight. Combine functions (stacked_bracket /
_select_bracket) untouched; gate default-OFF is byte-identical.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- ÷√cadence gap normalization, direction-agnostic, entry unchanged → Task 1 helper + tests.
- Insertion at candidate construction, combine functions untouched → Task 2 Step 4.
- Reuse existing `cadence_by_strat` (same value as daily_weight) → Task 2 Step 4.
- Gate `OPENCLAW_STRATEGY_CADENCE_STOP_NORM`, default-OFF byte-identical → Task 2 Steps 3 + test `test_gate_off_*`.
- Applies in both combine paths + any corroboration count → satisfied structurally (normalization precedes `_choose_bracket`); regression suite covers stacked + legacy.
- Backtest / recs / option path untouched → no files outside `bracket_stacking.py` + `regime_blended_sizer.py` candidate block are modified.
- Edge cases (c=1 no-op, bad entry, None/NaN levels) → Task 1 tests.

**Placeholder scan:** none — all steps contain runnable code/commands.

**Type consistency:** helper `daily_normalized_levels(entry, stop, t1, t2, cadence_days) -> (stop, t1, t2)` used identically in Task 1 and Task 2; candidate dict keys `entry/stop/t1/t2` match the existing structure and `_choose_bracket` consumer.
