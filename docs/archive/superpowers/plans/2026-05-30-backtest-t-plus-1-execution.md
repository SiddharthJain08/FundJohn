# Backtest signal[t] → execute[t+1] Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the unified backtest fill a signal generated on bar `t` at the next bar's close (`close[t+1]`) instead of the signal bar's close, re-anchoring brackets by pct shape, so backtest metrics reflect realistic next-day execution and the position-recs coupling operates on a clean baseline.

**Architecture:** A single behavioral change inside `_per_bar_simulate` in `src/backtest/unified_backtest.py` (the per-trade fill loop shared by `run_backtest` and `universe_grid_cli`). The change: locate the first bar strictly after the signal bar, fill `entry_price` at that bar's close (overriding any strategy-supplied entry_price), re-anchor stop/target as pct distances from the new fill, stamp `entry_date = t+1` while keeping `entry_regime = signal-day t`, and pass the fill date to `simulate_trade` so exits walk from t+2. No strategy files, no schema, no other engine changes.

**Tech Stack:** Python 3, pandas, pytest/unittest, PostgreSQL (psycopg2, only via `commit=False` ephemeral rollback in tests).

**Spec:** `docs/superpowers/specs/2026-05-30-backtest-t-plus-1-execution-design.md`

---

## Context the implementer needs

- The file is `src/backtest/unified_backtest.py`. The loop you change is `_per_bar_simulate` (lines ~542–598). The per-signal body currently does (paraphrased):
  ```python
  for sig in signals[:instance.MAX_SIGNALS]:
      direction = _signal_to_long_short(sig.direction)
      if direction == 0: continue
      ticker = sig.ticker
      if ticker not in bars_by_ticker: continue
      ticker_bars = bars_by_ticker[ticker]
      if current_date not in ticker_bars.index: continue
      entry_price = float(sig.entry_price) if (sig.entry_price and sig.entry_price > 0) else float(ticker_bars.loc[current_date, 'close'])
      stop_loss = float(sig.stop_loss) if (sig.stop_loss and sig.stop_loss > 0) else (entry_price * 0.93 if direction > 0 else entry_price * 1.07)
      target_1 = float(sig.target_1) if (sig.target_1 and sig.target_1 > 0) else (entry_price * 1.08 if direction > 0 else entry_price * 0.92)
      _ov = regime_param_override.resolve_override(strategy_id, str(regime_state), injected=param_override)
      if _ov:
          stop_loss, target_1 = regime_param_override.apply_override(entry_price=entry_price, direction=direction, stop_loss=stop_loss, target_1=target_1, override=_ov)
      if direction > 0 and (stop_loss >= entry_price or target_1 <= entry_price): continue
      if direction < 0 and (stop_loss <= entry_price or target_1 >= entry_price): continue
      exit_info = simulate_trade(ticker_bars, current_date, direction, entry_price, stop_loss, target_1, max_hold_days)
      ... trades.append({... 'entry_date': cur_d, 'entry_price': entry_price, 'entry_regime': str(regime_state), 'signal_stop': stop_loss, 'signal_target': target_1, ...})
  ```
- `_signal_to_long_short(direction)` → +1 long / −1 short / 0 unsupported.
- `simulate_trade(bars, entry_date, direction, entry_price, stop_loss, target_1, max_hold_days)` walks `bars.index > entry_date`. So passing the **fill date** makes exits start at the bar after the fill.
- **Why fill must override `sig.entry_price`:** 127 of ~140 strategy files set `sig.entry_price` to the signal-day price, so the existing `else close[t]` fallback is almost never reached. The t+1 fill therefore must be computed unconditionally at this boundary.
- Run a single test file with: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py -v` (the repo runs pytest; `tests/test_unified_backtest.py` uses `unittest` classes but pytest discovers them).
- The test fixture pattern (stub strategy + `bars_by_ticker` + regimes Series + mocked IO) is copied from `tests/test_universe_grid.py::TestRunBacktestResolverNone`. Reuse it verbatim where the tasks reference it.

## File Structure

- **Modify:** `src/backtest/unified_backtest.py` — only `_per_bar_simulate`'s per-signal body (fill, re-anchor, `simulate_trade` arg, trade-record `entry_date`). One module-level helper added: `_reanchor_bracket`.
- **Create:** `tests/test_unified_backtest_t_plus_1.py` — all new behavior tests.
- **Modify (docs only):** `CLAUDE.md` "Recent Changes" — one entry after merge (Task 6).

---

## Task 1: Add the `_reanchor_bracket` helper (pure function, TDD)

**Files:**
- Modify: `src/backtest/unified_backtest.py` (add a module-level function near `simulate_trade`, before `_per_bar_simulate`)
- Test: `tests/test_unified_backtest_t_plus_1.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_unified_backtest_t_plus_1.py` with:

```python
"""Tests for the signal[t] -> execute[t+1] fill model in unified_backtest.

Covers the pure bracket re-anchor helper and the _per_bar_simulate fill/exit
behavior (next-bar-close fill overriding strategy entry_price, pct-shape
bracket re-anchor, entry_date=t+1 / entry_regime=t stamping, last-bar skip,
and coupling-override composition).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


class TestReanchorBracket:
    def test_long_preserves_pct_distances(self):
        # ref=100, stop=93 (7% below), target=108 (8% above); new fill=110
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=110.0, direction=1,
            stop_ref=93.0, target_ref=108.0)
        assert abs(stop - 110.0 * 0.93) < 1e-9
        assert abs(target - 110.0 * 1.08) < 1e-9

    def test_short_preserves_pct_distances(self):
        # short: ref=100, stop=107 (7% above), target=92 (8% below); new fill=90
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=90.0, direction=-1,
            stop_ref=107.0, target_ref=92.0)
        assert abs(stop - 90.0 * 1.07) < 1e-9
        assert abs(target - 90.0 * 0.92) < 1e-9

    def test_identity_when_fill_equals_ref(self):
        stop, target = ub._reanchor_bracket(
            ref=100.0, entry_price=100.0, direction=1,
            stop_ref=95.0, target_ref=110.0)
        assert abs(stop - 95.0) < 1e-9
        assert abs(target - 110.0) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py::TestReanchorBracket -v`
Expected: FAIL with `AttributeError: module 'backtest.unified_backtest' has no attribute '_reanchor_bracket'`

- [ ] **Step 3: Write minimal implementation**

In `src/backtest/unified_backtest.py`, add this function immediately after `simulate_trade` (after its closing `return`, before the `# ── Metric aggregation ──` section):

```python
def _reanchor_bracket(*, ref: float, entry_price: float, direction: int,
                      stop_ref: float, target_ref: float) -> tuple[float, float]:
    """Re-express a stop/target defined as pct distances from ``ref`` so they
    sit the SAME pct distances from ``entry_price`` (the actual fill).

    Mirrors the live executor's re-anchor: preserves R:R geometry across an
    overnight gap instead of carrying absolute levels (which would invert the
    bracket when the fill gaps through a level). ``direction`` is +1 long / -1
    short. Returns (stop_loss, target_1).
    """
    if ref <= 0:
        return stop_ref, target_ref
    if direction > 0:  # long: stop below, target above
        stop_pct   = (ref - stop_ref) / ref
        target_pct = (target_ref - ref) / ref
        return entry_price * (1 - stop_pct), entry_price * (1 + target_pct)
    # short: stop above, target below
    stop_pct   = (stop_ref - ref) / ref
    target_pct = (ref - target_ref) / ref
    return entry_price * (1 + stop_pct), entry_price * (1 - target_pct)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py::TestReanchorBracket -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/backtest/unified_backtest.py tests/test_unified_backtest_t_plus_1.py
git commit -m "feat(backtest): add _reanchor_bracket pct-shape helper for t+1 fills

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Fill at close[t+1] in `_per_bar_simulate` (the core change, TDD)

**Files:**
- Modify: `src/backtest/unified_backtest.py` — the per-signal body of `_per_bar_simulate` (lines ~542–598)
- Test: `tests/test_unified_backtest_t_plus_1.py`

This task uses a shared fixture that drives a real `run_backtest` over a tiny in-memory dataset with fully mocked IO. Add the fixture helpers first, then the behavior tests.

- [ ] **Step 1: Add the shared fixture helpers to the test file**

Append to `tests/test_unified_backtest_t_plus_1.py`:

```python
def _make_mock_conn():
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


def _bars_from_closes(closes_by_ticker, dates):
    """Build bars_by_ticker with explicit OHLC. closes_by_ticker maps
    ticker -> list[ (open, high, low, close) ] aligned to dates."""
    out = {}
    for t, rows in closes_by_ticker.items():
        df = pd.DataFrame(
            {'open':  [r[0] for r in rows],
             'high':  [r[1] for r in rows],
             'low':   [r[2] for r in rows],
             'close': [r[3] for r in rows]},
            index=pd.DatetimeIndex(dates, name='date'),
        )
        out[t] = df
    return out


def _run_capture(strategy_cls, close_wide, bars_by_ticker, regimes,
                 *, param_override=None):
    """Run run_backtest with mocked IO; return the list of trade dicts the
    simulator produced. We capture trades by patching aggregate_metrics to
    stash its input (the trades list) on a mutable holder."""
    captured = {'trades': None}
    real_aggregate = ub.aggregate_metrics

    def capturing_aggregate(trades):
        captured['trades'] = list(trades)
        return real_aggregate(trades)

    mock_conn = _make_mock_conn()
    with (
        patch('backtest.unified_backtest.load_prices_panels',
              return_value=(close_wide, bars_by_ticker)),
        patch('backtest.unified_backtest.load_regimes', return_value=regimes),
        patch('backtest.unified_backtest.load_strategy_class',
              return_value=strategy_cls),
        patch('backtest.unified_backtest.find_strategy_file',
              return_value=str(ROOT / 'src/strategies/implementations/momentum_12_1.py')),
        patch('backtest.unified_backtest._code_sha', return_value='abc123'),
        patch('backtest.unified_backtest.aggregate_metrics',
              side_effect=capturing_aggregate),
        patch('backtest.unified_backtest.psycopg2.extras.execute_values'),
    ):
        ub.run_backtest('stub_t1', conn=mock_conn, commit=False,
                        param_override=param_override)
    return captured['trades']


def _stub_cls(entry_offset_pct=0.0, stop_pct=0.07, target_pct=0.08,
              direction='LONG'):
    """A strategy that, once it has >=10 bars, emits one signal on the LAST
    bar of `prices` for the first universe ticker. entry_price is set to the
    signal-day close * (1+entry_offset_pct) to exercise the override path."""
    from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

    class Stub(BaseStrategy):
        id = 'stub_t1'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or not universe:
                return []
            ticker = universe[0]
            if ticker not in prices.columns:
                return []
            close = float(prices[ticker].iloc[-1])
            ep = close * (1 + entry_offset_pct)
            if direction == 'LONG':
                return [Signal(ticker=ticker, direction='LONG', entry_price=ep,
                               stop_loss=ep * (1 - stop_pct),
                               target_1=ep * (1 + target_pct))]
            return [Signal(ticker=ticker, direction='SHORT', entry_price=ep,
                           stop_loss=ep * (1 + stop_pct),
                           target_1=ep * (1 - target_pct))]

    return Stub
```

- [ ] **Step 2: Write the failing fill test**

Append:

```python
class TestNextBarFill:
    def _trivial_dataset(self):
        # 12 business days; flat-ish path with a deliberate close jump at the
        # signal->fill boundary so close[t] != close[t+1].
        dates = pd.date_range('2024-01-01', periods=12, freq='B')
        # close ramps 100,101,...,111 — every adjacent pair differs.
        closes = [100.0 + i for i in range(12)]
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        # OHLC wide enough that neither stop nor target trips immediately
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        bars = _bars_from_closes({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        return close_wide, bars, regimes, dates, closes

    def test_fill_is_next_bar_close_overriding_entry_price(self):
        close_wide, bars, regimes, dates, closes = self._trivial_dataset()
        trades = _run_capture(_stub_cls(), close_wide, bars, regimes)
        assert trades, 'stub should have produced at least one trade'
        tr = trades[0]
        # The strategy emits on the LAST bar of prices_to_date (= current_date).
        # Find that bar's index position from entry_date (which is now t+1).
        entry_dt = pd.Timestamp(tr['entry_date'])
        pos = list(dates).index(entry_dt)
        # entry_price must equal close at the fill bar (t+1), NOT close[t].
        assert abs(tr['entry_price'] - closes[pos]) < 1e-9
        # And it must differ from the signal-bar close (t = pos-1).
        assert abs(tr['entry_price'] - closes[pos - 1]) > 0.5
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py::TestNextBarFill -v`
Expected: FAIL — current code fills at close[t] and stamps `entry_date=t`, so `tr['entry_price']` equals `closes[pos]` where `pos` is the signal bar, and the "differ from signal-bar close" assertion fails (entry equals the signal-bar close).

- [ ] **Step 4: Write the implementation**

In `src/backtest/unified_backtest.py`, replace the per-signal body inside `_per_bar_simulate` (the block from `entry_price = float(sig.entry_price) ...` through the `simulate_trade(...)` call and the `trades.append({...})`). Replace these specific regions:

Replace the entry/stop/target construction (lines ~553–558):
```python
            entry_price = float(sig.entry_price) if (sig.entry_price and sig.entry_price > 0) \
                          else float(ticker_bars.loc[current_date, 'close'])
            stop_loss = float(sig.stop_loss) if (sig.stop_loss and sig.stop_loss > 0) \
                        else (entry_price * 0.93 if direction > 0 else entry_price * 1.07)
            target_1 = float(sig.target_1) if (sig.target_1 and sig.target_1 > 0) \
                       else (entry_price * 1.08 if direction > 0 else entry_price * 0.92)
```
with:
```python
            # signal[t] -> execute[t+1]: the strategy decides on `current_date`
            # (t) but the order fills on the NEXT available bar's close (t+1).
            # `ref` is the strategy's intended price (signal-day close in
            # practice — 127/140 strategies set entry_price themselves); brackets
            # are shaped around it, then re-anchored to the actual fill.
            ref = float(sig.entry_price) if (sig.entry_price and sig.entry_price > 0) \
                  else float(ticker_bars.loc[current_date, 'close'])
            stop_ref = float(sig.stop_loss) if (sig.stop_loss and sig.stop_loss > 0) \
                       else (ref * 0.93 if direction > 0 else ref * 1.07)
            target_ref = float(sig.target_1) if (sig.target_1 and sig.target_1 > 0) \
                         else (ref * 1.08 if direction > 0 else ref * 0.92)
            # Locate t+1: first bar strictly after the signal bar.
            _future_idx = ticker_bars.index[ticker_bars.index > current_date]
            if len(_future_idx) == 0:
                continue  # signal on the last available bar — cannot fill
            fill_date = _future_idx[0]
            entry_price = float(ticker_bars.loc[fill_date, 'close'])
            stop_loss, target_1 = _reanchor_bracket(
                ref=ref, entry_price=entry_price, direction=direction,
                stop_ref=stop_ref, target_ref=target_ref)
```

Then change the `simulate_trade` call (line ~572) from:
```python
            exit_info = simulate_trade(ticker_bars, current_date, direction,
                                       entry_price, stop_loss, target_1, max_hold_days)
```
to:
```python
            exit_info = simulate_trade(ticker_bars, fill_date, direction,
                                       entry_price, stop_loss, target_1, max_hold_days)
```

Then change the trade-record `entry_date` (line ~588) from:
```python
                'entry_date':     cur_d,
```
to:
```python
                'entry_date':     fill_date.date() if hasattr(fill_date, 'date') else fill_date,
```

Leave the `_ov` / `regime_param_override.apply_override` block, the wrong-side sanity skips, and `'entry_regime': str(regime_state)` exactly as they are — the override now re-anchors on the t+1 `entry_price` automatically, and `entry_regime` stays the signal-day (t) regime.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py::TestNextBarFill -v`
Expected: PASS (1 passed)

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw
git add src/backtest/unified_backtest.py tests/test_unified_backtest_t_plus_1.py
git commit -m "feat(backtest): fill signals at close[t+1] in _per_bar_simulate

Override strategy entry_price at the simulate boundary (127/140 strategies
set it to signal-day close), re-anchor brackets by pct shape, stamp
entry_date=t+1, walk exits from t+2. entry_regime stays signal-day t.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Exit walks from t+2, and last-bar signals are skipped (TDD)

**Files:**
- Test: `tests/test_unified_backtest_t_plus_1.py` (no source change — these assert behavior delivered by Task 2)

These are characterization tests that lock in the exit-timing and no-fill-on-last-bar guarantees. If Task 2 was implemented correctly they pass immediately; write them anyway (they guard against regressions).

- [ ] **Step 1: Write the exit-walk test**

Append to `tests/test_unified_backtest_t_plus_1.py`:

```python
class TestExitTimingAndLastBar:
    def test_exit_ignores_fill_bar_and_walks_from_t_plus_2(self):
        # Construct: signal fires on bar t (the last bar of prices when len>=10).
        # The TARGET is touchable on the FILL bar (t+1) but must be ignored
        # (no same-bar exit on the fill), then first legitimately reachable on
        # t+2. We assert holding_days >= 1 measured from the fill bar AND that
        # the exit_date is strictly after the fill bar.
        dates = pd.date_range('2024-01-01', periods=13, freq='B')
        closes = [100.0] * 13
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        # Default OHLC = flat at 100. The stub sets target ~ +8% = 108.
        rows = [(100.0, 100.2, 99.8, 100.0) for _ in range(13)]
        # Make the FILL bar (index of first signal+1) spike its HIGH to 999 so,
        # if the simulator wrongly checked the fill bar, it'd exit there.
        # Signal fires when len(prices)>=10 -> current_date = dates[9]; fill = dates[10].
        rows[10] = (100.0, 999.0, 99.8, 100.0)   # fill bar: huge high (must be ignored)
        rows[11] = (100.0, 999.0, 99.8, 100.0)   # t+2: target legitimately hit here
        bars = _bars_from_closes({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        trades = _run_capture(_stub_cls(stop_pct=0.50, target_pct=0.08),
                              close_wide, bars, regimes)
        assert trades
        tr = trades[0]
        fill_dt = pd.Timestamp(tr['entry_date'])     # = dates[10]
        exit_dt = pd.Timestamp(tr['exit_date'])
        assert exit_dt > fill_dt, 'exit must be strictly after the fill bar'
        assert tr['exit_reason'] == 'target'
        # The target was reachable at dates[11] (t+2), one bar after the fill.
        assert exit_dt == dates[11]

    def test_signal_on_last_bar_is_skipped(self):
        # A dataset where the strategy's signal bar is the FINAL bar -> no t+1
        # -> the trade is skipped. We force this by making the stub emit ONLY
        # on the final bar.
        from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES

        dates = pd.date_range('2024-01-01', periods=12, freq='B')
        closes = [100.0 + i for i in range(12)]
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        bars = _bars_from_closes({'AAA': rows}, dates)
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})

        class LastBarStub(BaseStrategy):
            id = 'stub_t1'
            min_lookback = 5
            active_in_regimes = list(CANONICAL_REGIMES)

            def generate_signals(self, prices, regime, universe, aux_data=None):
                # fire ONLY when prices ends on the very last dataset date
                if prices.index[-1] != dates[-1] or not universe:
                    return []
                t = universe[0]
                c = float(prices[t].iloc[-1])
                return [Signal(ticker=t, direction='LONG', entry_price=c,
                               stop_loss=c * 0.93, target_1=c * 1.08)]

        trades = _run_capture(LastBarStub, close_wide, bars, regimes)
        # The only signal is on the last bar -> no fill bar -> zero trades.
        assert trades == []
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py::TestExitTimingAndLastBar -v`
Expected: PASS (2 passed). If `test_exit_ignores_fill_bar_and_walks_from_t_plus_2` fails with an exit on the fill bar, Task 2 passed `current_date` (not `fill_date`) to `simulate_trade` — fix Task 2.

- [ ] **Step 3: Commit**

```bash
cd /root/openclaw
git add tests/test_unified_backtest_t_plus_1.py
git commit -m "test(backtest): lock t+2 exit walk and last-bar no-fill skip

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Regime stamping = signal day, and coupling override composes (TDD)

**Files:**
- Test: `tests/test_unified_backtest_t_plus_1.py`

- [ ] **Step 1: Write the regime-stamping test**

Append:

```python
class TestRegimeStampingAndOverride:
    def _dataset_regime_changes_at_fill(self):
        # Regime is LOW_VOL on the signal bar (t) and CRISIS on the fill bar
        # (t+1). entry_regime must remain the SIGNAL-day regime (LOW_VOL).
        dates = pd.date_range('2024-01-01', periods=12, freq='B')
        closes = [100.0 + i for i in range(12)]
        close_wide = pd.DataFrame({'AAA': closes}, index=dates)
        close_wide.index.name = 'date'
        rows = [(c, c + 0.2, c - 0.2, c) for c in closes]
        bars = _bars_from_closes({'AAA': rows}, dates)
        # signal fires at dates[9] (t); fill at dates[10] (t+1).
        regimes = pd.Series({d: 'LOW_VOL' for d in dates})
        regimes[dates[10]] = 'CRISIS'   # different regime on the fill bar
        return close_wide, bars, regimes, dates

    def test_entry_regime_is_signal_day_not_fill_day(self):
        close_wide, bars, regimes, dates = self._dataset_regime_changes_at_fill()
        trades = _run_capture(_stub_cls(), close_wide, bars, regimes)
        assert trades
        tr = trades[0]
        assert pd.Timestamp(tr['entry_date']) == dates[10]   # fill = t+1
        assert tr['entry_regime'] == 'LOW_VOL'               # signal day = t

    def test_coupling_override_reanchors_to_t_plus_1_fill(self):
        # With an injected param_override forcing stop_pct/target_pct, the
        # recorded signal_stop/signal_target must sit those pct distances from
        # the t+1 fill (entry_price), not from close[t].
        close_wide, bars, regimes, dates = self._dataset_regime_changes_at_fill()
        # Override applies to the signal-day regime LOW_VOL. Shape: absolute
        # replace stop=10% / target=15% from entry (matches regime_param_override).
        override = {'LOW_VOL': {'stop_pct': 0.10, 'target_pct': 0.15}}
        trades = _run_capture(_stub_cls(), close_wide, bars, regimes,
                              param_override=override)
        assert trades
        tr = trades[0]
        ep = tr['entry_price']
        assert abs(tr['signal_stop'] - ep * (1 - 0.10)) < 1e-6
        assert abs(tr['signal_target'] - ep * (1 + 0.15)) < 1e-6
```

- [ ] **Step 2: Verify the override injection shape**

Before running, confirm the `param_override` dict shape the test uses matches what `regime_param_override.resolve_override(strategy_id, regime_state, injected=param_override)` expects. Run:

```bash
cd /root/openclaw && python3 -c "
import inspect, sys; sys.path.insert(0,'src')
from execution import regime_param_override as r
print(inspect.getsource(r.resolve_override))
print('---apply---')
print(inspect.getsource(r.apply_override))
"
```
Expected: shows `resolve_override` reading `injected[regime_state]` (or equivalent) and `apply_override` using `stop_pct`/`target_pct` as absolute-replace fractions. If the real keys differ (e.g. nested under another field), adjust the `override` dict in the test to match before Step 3. **Do not change the source to fit the test — fit the test to the real contract.**

- [ ] **Step 3: Run tests to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py::TestRegimeStampingAndOverride -v`
Expected: PASS (2 passed)

- [ ] **Step 4: Commit**

```bash
cd /root/openclaw
git add tests/test_unified_backtest_t_plus_1.py
git commit -m "test(backtest): regime stamped at signal day, override re-anchors to t+1 fill

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Regression — existing backtest tests still pass + real-strategy smoke (TDD-adjacent)

**Files:**
- Test: run existing suites (no new file)

- [ ] **Step 1: Run the existing unified-backtest unit tests**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest.py tests/test_universe_grid.py -v -m "not integration"`
Expected: PASS. These cover `simulate_trade`, `aggregate_metrics`, `aggregate_per_regime`, and the resolver=None / resolver-set paths. None of them assert the entry bar equals the signal bar, so they remain green. If `test_universe_grid.py::TestRunBacktestResolverNone` fails, the change leaked outside the per-signal body — review the Task 2 edit.

- [ ] **Step 2: Run the coupling + panel + instrument-dispatch suites**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_coupled_recs.py tests/test_unified_backtest_panel_hook.py tests/test_backtest_instrument_class_dispatch.py tests/test_param_change_backtest_cols.py -v -m "not integration"`
Expected: PASS. The coupling derives candidate brackets from `median_stop_pct`/`median_target_pct`, which now reflect re-anchored (t+1) brackets — still internally consistent.

- [ ] **Step 3: Real-strategy smoke (ephemeral, no DB write)**

Run a short-window real backtest with `--no-commit --return-metrics` on a fast strategy and confirm it produces metrics without error:

```bash
cd /root/openclaw
python3 -m backtest.unified_backtest --strategy-id S25_dual_momentum \
  --start-date 2024-01-01 --end-date 2024-06-30 --no-commit --return-metrics
```
Expected: a metrics JSON line on stdout with `sharpe`, `total_trades`, `median_stop_pct`, `median_target_pct` keys; non-zero `total_trades`. (`S25_dual_momentum` has the shortest avg hold ~2.9d, so it exercises many fills.) This writes nothing to the DB (`--no-commit`).

- [ ] **Step 4: Run the full t+1 test file once more (all classes together)**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py -v`
Expected: PASS (all classes: TestReanchorBracket, TestNextBarFill, TestExitTimingAndLastBar, TestRegimeStampingAndOverride).

- [ ] **Step 5: Commit (only if any test needed a fixture tweak; otherwise skip)**

```bash
cd /root/openclaw
git add -A
git commit -m "test(backtest): regression green for t+1 fill model" || echo "nothing to commit"
```

---

## Task 6: Rollout doc + CLAUDE.md entry (docs only — NO live deploy in this plan)

**Files:**
- Modify: `CLAUDE.md` (the "Recent Changes" list, top entry)

> **Operator-gated steps are OUT of this plan.** The merge to `main`, the
> `--all-live` rebuild, and any service restart are operator actions (the safety
> classifier blocks behavior-changing prod deploys from the agent). This task
> only records the change and the runbook. The implementer does NOT push, does
> NOT run `--all-live` against the live DB, and does NOT restart johnbot.

- [ ] **Step 1: Add the CLAUDE.md Recent Changes entry**

In `CLAUDE.md`, insert this as the new first bullet under `## Recent Changes`:

```markdown
- **2026-05-30: Backtest signal[t] → execute[t+1] fill model** (branch `feat/backtest-t-plus-1-execution`). `_per_bar_simulate` in `src/backtest/unified_backtest.py` now fills each signal at the NEXT bar's close (`close[t+1]`) instead of the signal-bar close, overriding any strategy-supplied `entry_price` at the simulate boundary (127/140 strategies set it to the signal-day close, so the old fallback was almost never hit). Brackets are re-anchored to the new fill preserving pct shape (`_reanchor_bracket`, mirrors the live executor); `entry_date` is stamped to the fill bar (t+1) while `entry_regime` stays the signal-day (t) regime (so `aggregate_per_regime` keeps decision-day partitioning). Exits walk from t+2 (fill date passed to `simulate_trade`). Signals on the last available bar are skipped (no fill possible). Scope: `unified_backtest._per_bar_simulate` only — shared by `run_backtest` (weekly refresh + coupling) and `universe_grid_cli`; `quick_backtest`/`regime_blended_backtest`/`intraday_regime_backtest`/`regime_performance_analyzer`/`options_backtest` unchanged (they don't use per-trade close[t] fills). **Conservatism note:** live fills ~same-day close (`close[t]`); backtest now fills `close[t+1]`, so the backtest is deliberately MORE conservative than live — and the coupling decision is backtest-to-backtest so the level shift cancels in the ΔSharpe. **Operator rollout (NOT auto):** let the Sat 12:00 UTC weekend run finish on the OLD model, then merge, then run `python3 -m backtest.unified_backtest --all-live` once to rebuild every live strategy's metrics on t+1 (the next weekend `refresh_backtests.sh` does this automatically thereafter), then verify a couple of short-hold strategies' Sharpe shifted + the dashboard panel refreshed. Spec: `docs/superpowers/specs/2026-05-30-backtest-t-plus-1-execution-design.md`. Plan: `docs/superpowers/plans/2026-05-30-backtest-t-plus-1-execution.md`.
```

- [ ] **Step 2: Commit**

```bash
cd /root/openclaw
git add CLAUDE.md
git commit -m "docs: record backtest t+1 execution change in CLAUDE.md

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: Report the operator handoff (do NOT execute)**

Print a final summary for the operator stating exactly:
1. Branch is ready; tests green (`python3 -m pytest tests/test_unified_backtest_t_plus_1.py tests/test_unified_backtest.py tests/test_universe_grid.py -m "not integration"`).
2. The Sat 12:00 UTC weekend run will execute on the OLD model (timer `openclaw-weekend-saturday`, already armed) — do not merge until it completes.
3. After that run: merge → `python3 -m backtest.unified_backtest --all-live` → verify. **Exclude the 2 in-flight strategies** from any manual rebuild (they'll be re-backtested by the next 8 AM ET `refresh_backtests.sh` on the new model); name them at rebuild time from the manifest's non-live recent additions.

---

## Self-Review

**1. Spec coverage:**
- Fill at close[t+1] overriding strategy entry_price → Task 2 (impl) + TestNextBarFill.
- Re-anchor brackets preserving pct shape → Task 1 (`_reanchor_bracket`) + Task 2 (call site) + TestReanchorBracket + TestRegimeStampingAndOverride.
- entry_date = t+1 → Task 2 + TestNextBarFill/TestRegimeStampingAndOverride.
- entry_regime = signal-day t → Task 2 (unchanged line) + TestRegimeStampingAndOverride.
- Exit walks from t+2 → Task 2 (`simulate_trade(fill_date, ...)`) + TestExitTimingAndLastBar.
- Last-bar signal skipped → Task 2 (`continue`) + TestExitTimingAndLastBar.
- signal_stop/signal_target record re-anchored values → Task 2 (uses re-anchored `stop_loss`/`target_1`) + TestRegimeStampingAndOverride.
- Coupling override composes on t+1 fill → Task 2 (order: re-anchor then override) + TestRegimeStampingAndOverride.
- Scope = unified only → no other files touched; Task 5 regression confirms other suites green.
- Conservatism note + operator rollout → Task 6 (CLAUDE.md) + spec.
- All spec sections covered. No gaps.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows full code. Task 4 Step 2 deliberately verifies the real override contract before asserting (and instructs fitting the test to the contract, not vice-versa).

**3. Type consistency:** `_reanchor_bracket(*, ref, entry_price, direction, stop_ref, target_ref) -> (stop, target)` is defined in Task 1 and called identically in Task 2. `fill_date` introduced in Task 2 and reused in the `simulate_trade` call and `entry_date` stamp. Test helpers `_make_mock_conn`/`_bars_from_closes`/`_run_capture`/`_stub_cls` defined in Task 2 Step 1 and reused in Tasks 3–4. Consistent.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-30-backtest-t-plus-1-execution.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, two-stage review (spec compliance then code quality) between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session via executing-plans, batch with checkpoints.

**Which approach?**
