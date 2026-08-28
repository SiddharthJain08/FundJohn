# Per-bar Exit Hook — Phase 1 (backtest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an opt-in strategy decide, once per bar at the close, to flatten an open backtest position (`BaseStrategy.should_exit`), honor per-signal `hold_days`, guard promotion of such strategies until the live mirror exists, and give `S_coint_pairs_sector_v2` its |z| ≤ 0.5 reversion exit so X1 can be re-tested.

**Architecture:** `BaseStrategy` gains an `exit_hook` class flag and a `should_exit(position, prices, regime, aux_data)` method. `unified_backtest._per_bar_simulate` keeps today's `simulate_trade`-at-entry path for every non-hook strategy (byte-identical) and, for `exit_hook=True` strategies only, keeps trades in an open book that a new module `backtest/open_book.py` advances one bar per day in the fixed order bracket → hook → time. The per-bar bracket decision is extracted from `simulate_trade` into a pure helper so both paths share it. `promotion_service.js` refuses candidate→live for runs whose `config_json.exit_hook` is true unless `OPENCLAW_EXIT_HOOK_LIVE=1`.

**Tech Stack:** Python 3 / pandas / numpy / pytest (`PYTHONPATH=src`), Node 20 `node --test`, PostgreSQL (mocked in tests), systemd transient units for the X1 re-run.

**Spec:** `docs/specs/2026-08-28-per-bar-exit-hook-spec.md` (approved 2026-08-28). Read §1–§2 and §4–§6 before starting. Refinement recorded in Task 8: §4's "manifest `metadata.exit_hook`" becomes `strategy_backtest_runs.config_json.exit_hook` — `unified_backtest` never writes the manifest today and must not start.

## Global Constraints

- Non-hook strategies must produce byte-identical trade lists before/after (spec §2). Task 4's equivalence test is the gate; the determinism suite runs under `PYTHONHASHSEED=0`.
- Exit order on a bar is fixed: intra-bar bracket → hook at close → time (spec §2). Never reorder.
- Hook errors ⇒ HOLD, counted, first message logged (spec §1). Never exit on error.
- Hook exits never touch `run_stop_history` (stop cooldown keys on `exit_reason == 'stop'` only).
- Persisted reason format: `'strategy_exit:<reason>'` (exact prefix, colon, no spaces).
- `OPENCLAW_EXIT_HOOK_LIVE` default `'0'`; only `'1'` enables anything live-facing (Phase 1 only reads it in the promotion guard).
- Run Python tests with `PYTHONPATH=src python3 -m pytest <path> -q -p no:cacheprovider`. NEVER run the full suite (`npm test`) while the fleet runs; run only the files named in each task. Tests reach the real DB through `.env` at import — the fixtures below mock every DB call.
- No heavy compute 13:00–20:15 UTC; the X1 re-run (Task 8) is a transient systemd unit outside that window.
- Commit per task on `main` (`git push origin main` after each commit); commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Never `git reset --hard`.

## File structure

| file | responsibility |
|---|---|
| `src/strategies/base.py` (modify) | `exit_hook` flag, `should_exit` default, `__init_subclass__` opt-in validation |
| `src/backtest/unified_backtest.py` (modify) | `_bar_exit` extracted from `simulate_trade`; open-book branch in `_per_bar_simulate`; `exit_hook` in `config_json`; counters logged |
| `src/backtest/open_book.py` (create) | `OpenTrade` record + `advance_open_book(...)`: the per-bar bracket → hook → time stepper, position dict construction, error counting |
| `src/lib/promotion_service.js` (modify) | `config_json.exit_hook` read in `_latestPrimaryRun`; `exit_hook_live_disabled` gate |
| `src/strategies/implementations/S_coint_pairs_sector_v2.py` (modify) | `exit_hook = True`, `Z_EXIT`, `should_exit` (z_revert / pair_decohered) |
| `tests/strategies/test_exit_hook_interface.py` (create) | Task 1 |
| `tests/backtest/test_open_book.py` (create) | Tasks 2–5 |
| `tests/lib/promotion_exit_hook_guard.test.js` (create) | Task 6 |
| `tests/strategies/test_coint_pairs_v2.py` (modify) | Task 7 |
| `docs/specs/…exit-hook-spec.md`, `docs/archive/changelog.md` (modify) | Task 8 |

---

### Task 1: `BaseStrategy.exit_hook` + `should_exit` + opt-in validation

**Files:**
- Modify: `src/strategies/base.py` (class attrs block after `MAX_SIGNALS`, ~line 131; `__init_subclass__` ~line 133; new method after `position_scale` ~line 199)
- Test: `tests/strategies/test_exit_hook_interface.py` (create)

**Interfaces:**
- Produces: `BaseStrategy.exit_hook: bool = False`; `BaseStrategy.should_exit(self, position: dict, prices: pd.DataFrame, regime: dict, aux_data: dict | None = None) -> str | None` returning `None` by default; `TypeError` at class definition when `exit_hook = True` and `should_exit` is not overridden.

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_exit_hook_interface.py
"""Spec §1: exit_hook opt-in flag + should_exit default contract."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import BaseStrategy, CANONICAL_REGIMES


def _mk(**attrs):
    body = {'id': 'x', 'active_in_regimes': list(CANONICAL_REGIMES),
            'generate_signals': lambda self, prices, regime, universe, aux_data=None: []}
    body.update(attrs)
    return type('Dyn', (BaseStrategy,), body)


def test_default_flag_is_false_and_should_exit_returns_none():
    cls = _mk()
    assert cls.exit_hook is False
    inst = cls()
    assert inst.should_exit({'ticker': 'AAA'}, pd.DataFrame(), {'state': 'LOW_VOL'}) is None
    assert inst.should_exit({'ticker': 'AAA'}, pd.DataFrame(), {'state': 'LOW_VOL'}, None) is None


def test_exit_hook_true_without_override_is_a_class_definition_error():
    with pytest.raises(TypeError, match='exit_hook'):
        _mk(exit_hook=True)


def test_exit_hook_true_with_override_defines_fine():
    cls = _mk(exit_hook=True,
              should_exit=lambda self, position, prices, regime, aux_data=None: 'because')
    assert cls.exit_hook is True
    assert cls().should_exit({}, pd.DataFrame(), {}) == 'because'


def test_override_without_flag_is_allowed_but_flag_stays_false():
    cls = _mk(should_exit=lambda self, position, prices, regime, aux_data=None: 'x')
    assert cls.exit_hook is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/strategies/test_exit_hook_interface.py -q -p no:cacheprovider`
Expected: 3 failures — `AttributeError: type object 'Dyn' has no attribute 'exit_hook'` / `should_exit`, and the `pytest.raises(TypeError)` test fails with "DID NOT RAISE".

- [ ] **Step 3: Implement**

In `src/strategies/base.py`, after `MAX_SIGNALS:      int = 50` add:

```python
    # Per-bar exit hook (spec docs/specs/2026-08-28-per-bar-exit-hook-spec.md §1).
    # Explicit opt-in: the backtest open-book path and (Phase 2) live
    # update_pnl call should_exit() ONLY when this is True. Overriding
    # should_exit without setting the flag is inert by design.
    exit_hook:        bool = False
```

At the END of `__init_subclass__` (after the `cls.active_in_regimes = normalized or [...]` line) add:

```python
        # exit_hook=True with the base should_exit is a silent no-op that
        # would masquerade as a tested exit — refuse at class definition.
        if cls.__dict__.get('exit_hook', False) or getattr(cls, 'exit_hook', False):
            if cls.should_exit is BaseStrategy.should_exit:
                raise TypeError(
                    f'{cls.__name__}: exit_hook=True requires overriding should_exit()')
```

After `position_scale` add:

```python
    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Per-bar exit decision for ONE open position (spec §1).

        Called only when `exit_hook` is True, once per open position per bar,
        AFTER the intra-bar bracket check and BEFORE the time stop. Return a
        short snake_case reason token to flatten at TODAY's close, or None to
        keep holding. Must be a pure, look-ahead-safe function of its
        arguments: `prices` ends at the evaluation bar; `position` carries
        ticker, direction ('LONG'|'SHORT'), entry_price, entry_date,
        days_held, stop_loss, target_1 and the entry-time signal_params dict.
        Raising is caught by the caller and treated as None (hold)."""
        return None
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/strategies/test_exit_hook_interface.py tests/strategies/test_coint_pairs_v2.py -q -p no:cacheprovider`
Expected: all pass (the X1 file proves existing subclasses still define cleanly).

- [ ] **Step 5: Commit**

```bash
git add src/strategies/base.py tests/strategies/test_exit_hook_interface.py
git commit -m "strategies: BaseStrategy.exit_hook flag + should_exit() default (exit-hook spec §1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 2: Extract the per-bar bracket decision from `simulate_trade`

**Files:**
- Modify: `src/backtest/unified_backtest.py` — `simulate_trade` body (~lines 426–446)
- Test: `tests/backtest/test_open_book.py` (create)

**Interfaces:**
- Produces: `unified_backtest._bar_exit(direction: int, high: float, low: float, stop_loss: float, target_1: float, dt_priority: str) -> tuple[float | None, str | None]` — `(exit_level, 'stop'|'target')` or `(None, None)`. Pure; `dt_priority` is the resolved `OPENCLAW_BT_DOUBLE_TOUCH` value (`'stop'` default, `'target'` legacy).

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_open_book.py
"""Exit-hook Phase 1 simulator tests (spec §2). Tasks 2–5 append here."""
from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

import backtest.unified_backtest as ub


class TestBarExit:
    def test_long_stop_only(self):
        assert ub._bar_exit(1, high=101.0, low=94.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (95.0, 'stop')

    def test_long_target_only(self):
        assert ub._bar_exit(1, high=109.0, low=99.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (108.0, 'target')

    def test_long_neither(self):
        assert ub._bar_exit(1, high=101.0, low=99.0, stop_loss=95.0, target_1=108.0, dt_priority='stop') == (None, None)

    def test_short_mirrors(self):
        assert ub._bar_exit(-1, high=106.0, low=99.0, stop_loss=105.0, target_1=92.0, dt_priority='stop') == (105.0, 'stop')
        assert ub._bar_exit(-1, high=101.0, low=91.0, stop_loss=105.0, target_1=92.0, dt_priority='stop') == (92.0, 'target')

    def test_double_touch_priority(self):
        both = dict(high=110.0, low=90.0, stop_loss=95.0, target_1=108.0)
        assert ub._bar_exit(1, dt_priority='stop', **both) == (95.0, 'stop')
        assert ub._bar_exit(1, dt_priority='target', **both) == (108.0, 'target')
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py -q -p no:cacheprovider`
Expected: 5 failures, `AttributeError: module ... has no attribute '_bar_exit'`.

- [ ] **Step 3: Implement — extract, do not change behaviour**

Add above `simulate_trade` in `src/backtest/unified_backtest.py`:

```python
def _bar_exit(direction: int, high: float, low: float,
              stop_loss: float, target_1: float, dt_priority: str):
    """Intra-bar bracket decision shared by simulate_trade and the exit-hook
    open-book stepper. Returns (exit_level, reason) or (None, None).
    Long: target when high >= target_1, stop when low <= stop_loss; short
    mirrored. Double-touch resolves by dt_priority ('stop' default)."""
    if direction > 0:
        t_hit = high >= target_1
        s_hit = low <= stop_loss
    else:
        t_hit = low <= target_1
        s_hit = high >= stop_loss
    if t_hit and s_hit:
        if dt_priority == 'target':
            return float(target_1), 'target'
        return float(stop_loss), 'stop'
    if t_hit:
        return float(target_1), 'target'
    if s_hit:
        return float(stop_loss), 'stop'
    return None, None
```

Inside `simulate_trade`, replace the block from `exit_level, reason = None, None` through the `elif s_hit:` branch (keep the `if exit_level is None and i == n:` line that follows) with:

```python
        exit_level, reason = _bar_exit(direction, high, low, stop_loss, target_1, _dt_priority)
```

- [ ] **Step 4: Run new + existing simulator tests**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py tests/backtest/test_backtest_fill_model.py tests/backtest/test_adverse_slippage.py tests/backtest/test_true_mtm_marks.py -q -p no:cacheprovider`
Expected: all pass (the three existing files pin `simulate_trade` geometry; any failure means the extraction changed behaviour — fix the extraction, not the tests).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/backtest/test_open_book.py
git commit -m "backtest: extract _bar_exit() from simulate_trade (pure, shared by the open-book stepper)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 3: `backtest/open_book.py` — the per-bar stepper

**Files:**
- Create: `src/backtest/open_book.py`
- Test: `tests/backtest/test_open_book.py` (append)

**Interfaces:**
- Consumes: `unified_backtest._bar_exit` (Task 2); `BaseStrategy.should_exit` (Task 1).
- Produces:
  ```python
  @dataclass
  class OpenTrade:
      ticker: str; direction: int; entry_date: pd.Timestamp; entry_price: float   # raw signal/fill level
      entry_fill: float; stop_loss: float; target_1: float; hold_cap: int
      entry_regime: str; signal_params: dict; slippage: float                    # fraction, one-way
      holding_days: int = 0; prev_mark: float = 0.0; daily_marks: list = field(default_factory=list)

  def resolve_hold_cap(signal_params: dict | None, max_hold_days: int) -> int
  def advance_open_book(open_book: list[OpenTrade], current_date, bars_by_ticker: dict,
                        prices_to_date: pd.DataFrame, regime_payload: dict, aux: dict,
                        instance, *, dt_priority: str, counters: dict) -> list[dict]
  ```
  `advance_open_book` mutates `open_book` (removes closed trades) and returns the closed trades as dicts in exactly the shape `_per_bar_simulate` appends (`ticker, direction, entry_date, entry_price, exit_date, exit_price, exit_reason, holding_days, pnl_pct, entry_regime, signal_stop, signal_target, daily_marks`). `counters` keys it increments: `hook_exits`, `hook_raised`, and sets `first_hook_raise` (str) once.

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_open_book.py`:

```python
from backtest.open_book import OpenTrade, advance_open_book, resolve_hold_cap


def _bars(rows, dates):
    return pd.DataFrame({'open': [r[0] for r in rows], 'high': [r[1] for r in rows],
                         'low': [r[2] for r in rows], 'close': [r[3] for r in rows]},
                        index=pd.DatetimeIndex(dates, name='date'))


class _Hook:
    """Stand-in strategy: exit_hook=True with a scripted decision."""
    exit_hook = True

    def __init__(self, decide):
        self._decide = decide
        self.calls = []

    def should_exit(self, position, prices, regime, aux_data=None):
        self.calls.append((position['ticker'], prices.index[-1], position['days_held']))
        return self._decide(position, prices)


def _trade(entry_date, hold_cap=21, slippage=0.0, **kw):
    base = dict(ticker='AAA', direction=1, entry_date=entry_date, entry_price=100.0,
                entry_fill=100.0, stop_loss=95.0, target_1=108.0, hold_cap=hold_cap,
                entry_regime='LOW_VOL', signal_params={'k': 1}, slippage=slippage,
                prev_mark=100.0)
    base.update(kw)
    return OpenTrade(**base)


DATES = pd.date_range('2024-01-01', periods=6, freq='B')
CLOSES = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
CALM = _bars([(c, c + 0.5, c - 0.5, c) for c in CLOSES], DATES)
PANEL = pd.DataFrame({'AAA': CLOSES}, index=DATES)


class TestResolveHoldCap:
    def test_missing_or_invalid_uses_max(self):
        assert resolve_hold_cap(None, 21) == 21
        assert resolve_hold_cap({}, 21) == 21
        assert resolve_hold_cap({'hold_days': 'x'}, 21) == 21
        assert resolve_hold_cap({'hold_days': 0}, 21) == 21

    def test_min_with_max(self):
        assert resolve_hold_cap({'hold_days': 7}, 21) == 7
        assert resolve_hold_cap({'hold_days': 40}, 21) == 21
        assert resolve_hold_cap({'hold_days': 7.9}, 21) == 7


class TestAdvanceOpenBook:
    def _run(self, trade, hook, bars=CALM, dt_priority='stop', dates=None):
        book = [trade]
        closed_all = []
        counters = {}
        for d in (dates or DATES):
            if d <= trade.entry_date:
                continue
            closed = advance_open_book(book, d, {'AAA': bars}, PANEL.loc[:d],
                                       {'state': 'LOW_VOL', 'date': d.date().isoformat()},
                                       {'options': {}}, hook,
                                       dt_priority=dt_priority, counters=counters)
            closed_all.extend(closed)
            if not book:
                break
        return closed_all, book, counters

    def test_hook_exit_at_close_with_adverse_slippage(self):
        hook = _Hook(lambda pos, prices: 'z_revert' if prices.index[-1] == DATES[3] else None)
        closed, book, counters = self._run(_trade(DATES[0], slippage=0.001), hook)
        assert book == [] and len(closed) == 1
        t = closed[0]
        assert t['exit_reason'] == 'strategy_exit:z_revert'
        assert t['exit_date'] == DATES[3].date()
        assert t['exit_price'] == pytest.approx(103.0 * (1 - 0.001))
        assert t['holding_days'] == 3
        assert t['pnl_pct'] == pytest.approx((103.0 * 0.999 - 100.0) / 100.0)
        assert len(t['daily_marks']) == 3
        assert counters['hook_exits'] == 1
        # hook saw days_held 1,2,3 and a panel ending at the evaluation bar
        assert [c[2] for c in hook.calls] == [1, 2, 3]
        assert all(c[1] <= DATES[3] for c in hook.calls)

    def test_bracket_beats_hook_on_same_bar(self):
        rows = [(c, c + 0.5, c - 0.5, c) for c in CLOSES]
        rows[2] = (102.0, 102.5, 94.0, 102.0)          # low pierces the 95 stop on bar 2
        bars = _bars(rows, DATES)
        hook = _Hook(lambda pos, prices: 'z_revert')    # would exit every bar
        closed, book, counters = self._run(_trade(DATES[0]), hook, bars=bars)
        # bar 1 (DATES[1]) has no bracket hit -> hook fires there first
        assert closed[0]['exit_reason'] == 'strategy_exit:z_revert'
        assert closed[0]['exit_date'] == DATES[1].date()
        # now a trade that only becomes hook-eligible on bar 2 loses to the stop
        hook2 = _Hook(lambda pos, prices: 'z_revert' if prices.index[-1] >= DATES[2] else None)
        closed2, _, _ = self._run(_trade(DATES[0]), hook2, bars=bars)
        assert closed2[0]['exit_reason'] == 'stop'
        assert closed2[0]['exit_price'] == pytest.approx(95.0)

    def test_time_cap_from_hold_days(self):
        hook = _Hook(lambda pos, prices: None)
        closed, book, _ = self._run(_trade(DATES[0], hold_cap=2), hook)
        assert closed[0]['exit_reason'] == 'max_hold'
        assert closed[0]['exit_date'] == DATES[2].date()
        assert closed[0]['holding_days'] == 2

    def test_end_of_data_when_bars_run_out(self):
        hook = _Hook(lambda pos, prices: None)
        closed, book, _ = self._run(_trade(DATES[3], hold_cap=21), hook)
        assert closed[0]['exit_reason'] == 'end_of_data'
        assert closed[0]['exit_date'] == DATES[5].date()

    def test_raising_hook_holds_and_counts(self):
        def boom(pos, prices):
            raise RuntimeError('kaboom')
        hook = _Hook(boom)
        closed, book, counters = self._run(_trade(DATES[0], hold_cap=3), hook)
        assert closed[0]['exit_reason'] == 'max_hold'
        assert counters['hook_raised'] == 3
        assert counters['first_hook_raise'].startswith('RuntimeError')

    def test_position_dict_contract(self):
        seen = {}
        def grab(pos, prices):
            seen.update(pos); return 'now'
        hook = _Hook(grab)
        self._run(_trade(DATES[0], signal_params={'pair': 'AAA/BBB'}), hook)
        assert seen == {'ticker': 'AAA', 'direction': 'LONG', 'entry_price': 100.0,
                        'entry_date': DATES[0], 'days_held': 1, 'stop_loss': 95.0,
                        'target_1': 108.0, 'signal_params': {'pair': 'AAA/BBB'}}

    def test_non_hook_instance_skips_hook_entirely(self):
        class NoHook:
            exit_hook = False
            def should_exit(self, *a, **k):
                raise AssertionError('must not be called')
        closed, book, counters = self._run(_trade(DATES[0], hold_cap=2), NoHook())
        assert closed[0]['exit_reason'] == 'max_hold'
        assert counters.get('hook_exits', 0) == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py -q -p no:cacheprovider`
Expected: `ModuleNotFoundError: No module named 'backtest.open_book'`.

- [ ] **Step 3: Implement `src/backtest/open_book.py`**

```python
"""open_book.py — per-bar stepper for exit-hook strategies (spec
docs/specs/2026-08-28-per-bar-exit-hook-spec.md §2).

Only strategies with `exit_hook=True` use this path; every other strategy
keeps unified_backtest.simulate_trade (walk-at-entry) byte-identical. For an
open trade on a bar the order is FIXED: intra-bar bracket (_bar_exit) →
strategy.should_exit at the close → time cap (hold_cap) / end_of_data.
Marks accumulate exactly as simulate_trade does (mark-to-close on interior
bars, adverse exit fill on the exit bar) so downstream MTM/tail stats need
no change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sys

import pandas as pd

from backtest.unified_backtest import _bar_exit

HOOK_REASON_PREFIX = 'strategy_exit:'


@dataclass
class OpenTrade:
    ticker: str
    direction: int                 # +1 long / -1 short
    entry_date: pd.Timestamp       # fill date; bars <= entry_date are never stepped
    entry_price: float             # raw fill level (persisted as entry_price)
    entry_fill: float              # entry_price after adverse slippage
    stop_loss: float
    target_1: float
    hold_cap: int                  # min(signal hold_days, max_hold_days)
    entry_regime: str
    signal_params: dict
    slippage: float                # one-way fraction (bps / 1e4)
    holding_days: int = 0
    prev_mark: float = 0.0
    daily_marks: list = field(default_factory=list)


def resolve_hold_cap(signal_params, max_hold_days: int) -> int:
    """Per-signal hold_days capped by the run's max_hold_days (spec §1)."""
    try:
        hd = int(float((signal_params or {}).get('hold_days')))
    except (TypeError, ValueError):
        return int(max_hold_days)
    if hd < 1:
        return int(max_hold_days)
    return min(hd, int(max_hold_days))


def _position_dict(t: OpenTrade) -> dict:
    return {
        'ticker':        t.ticker,
        'direction':     'LONG' if t.direction > 0 else 'SHORT',
        'entry_price':   t.entry_price,
        'entry_date':    t.entry_date,
        'days_held':     t.holding_days,
        'stop_loss':     t.stop_loss,
        'target_1':      t.target_1,
        'signal_params': t.signal_params,
    }


def _close(t: OpenTrade, dt, exit_level: float, reason: str) -> dict:
    exit_fill = exit_level * (1.0 - t.direction * t.slippage)
    t.daily_marks.append((dt, t.direction * (exit_fill / t.prev_mark - 1.0)))
    pnl = t.direction * (exit_fill - t.entry_fill) / t.entry_fill
    return {
        'ticker':        t.ticker,
        'direction':     'long' if t.direction > 0 else 'short',
        'entry_date':    t.entry_date.date() if hasattr(t.entry_date, 'date') else t.entry_date,
        'entry_price':   t.entry_price,
        'exit_date':     dt.date() if hasattr(dt, 'date') else dt,
        'exit_price':    exit_fill,
        'exit_reason':   reason,
        'holding_days':  t.holding_days,
        'pnl_pct':       pnl,
        'entry_regime':  t.entry_regime,
        'signal_stop':   t.stop_loss,
        'signal_target': t.target_1,
        'daily_marks':   list(t.daily_marks),
    }


def advance_open_book(open_book: list, current_date, bars_by_ticker: dict,
                      prices_to_date: pd.DataFrame, regime_payload: dict, aux: dict,
                      instance, *, dt_priority: str, counters: dict) -> list:
    """Step every open trade through `current_date`'s bar. Mutates
    `open_book` (closed trades removed) and returns the closed trade dicts."""
    closed: list = []
    use_hook = bool(getattr(instance, 'exit_hook', False))
    still_open: list = []
    for t in open_book:
        if current_date <= t.entry_date:
            still_open.append(t)
            continue
        bars = bars_by_ticker.get(t.ticker)
        if bars is None or current_date not in bars.index:
            # No bar for this ticker today. If no future bar exists either the
            # trade ends at its last mark (end_of_data at prev_mark).
            if bars is None or len(bars.index[bars.index > current_date]) == 0:
                closed.append(_close(t, current_date, t.prev_mark, 'end_of_data'))
            else:
                still_open.append(t)
            continue
        bar = bars.loc[current_date]
        high, low, close = float(bar['high']), float(bar['low']), float(bar['close'])
        t.holding_days += 1
        # 1. intra-bar bracket
        exit_level, reason = _bar_exit(t.direction, high, low, t.stop_loss, t.target_1, dt_priority)
        # 2. hook at the close
        if exit_level is None and use_hook:
            try:
                r = instance.should_exit(_position_dict(t), prices_to_date, regime_payload, aux)
            except Exception as e:  # spec §1: hold, count, log first
                r = None
                counters['hook_raised'] = counters.get('hook_raised', 0) + 1
                if 'first_hook_raise' not in counters:
                    counters['first_hook_raise'] = f'{type(e).__name__}: {e}'
                    print(f'[open_book] should_exit raised on {t.ticker} {current_date.date()}: '
                          f'{counters["first_hook_raise"]} — holding', file=sys.stderr)
            if r:
                exit_level, reason = close, f'{HOOK_REASON_PREFIX}{r}'
                counters['hook_exits'] = counters.get('hook_exits', 0) + 1
        # 3. time cap / end of data
        if exit_level is None:
            no_more_bars = len(bars.index[bars.index > current_date]) == 0
            if t.holding_days >= t.hold_cap:
                exit_level, reason = close, 'max_hold'
            elif no_more_bars:
                exit_level, reason = close, 'end_of_data'
        if exit_level is not None:
            closed.append(_close(t, current_date, exit_level, reason))
            continue
        t.daily_marks.append((current_date, t.direction * (close / t.prev_mark - 1.0)))
        t.prev_mark = close
        still_open.append(t)
    open_book[:] = still_open
    return closed
```

Note the `simulate_trade` parity detail: `holding_days` counts stepped bars (fill bar excluded), `max_hold` fires when `holding_days == hold_cap` at that bar's close, `end_of_data` when the ticker has no later bar — the same three outcomes `simulate_trade` produces.

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py -q -p no:cacheprovider`
Expected: all pass. If `test_bracket_beats_hook_on_same_bar` fails on the first assertion, check that the hook is evaluated only when `exit_level is None`.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/open_book.py tests/backtest/test_open_book.py
git commit -m "backtest: open_book stepper — bracket → should_exit → time cap per bar (exit-hook spec §2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 4: Wire the open-book path into `_per_bar_simulate`

**Files:**
- Modify: `src/backtest/unified_backtest.py` — `_per_bar_simulate` (~lines 691–970): setup block, top of the day loop, the entry block that calls `simulate_trade`, the post-loop, the return dict
- Test: `tests/backtest/test_open_book.py` (append)

**Interfaces:**
- Consumes: `open_book.OpenTrade`, `advance_open_book`, `resolve_hold_cap` (Task 3).
- Produces: `_per_bar_simulate` return dict gains `hook_exits: int`, `hook_raised: int`, `first_hook_raise: str | None`. Behaviour for `instance.exit_hook == False` is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_open_book.py` (reuses the fill-model fixtures by import):

```python
from tests.backtest.test_backtest_fill_model import (_bars_from_rows, _run_capture,
                                                    _trivial_dataset)
from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES


def _mk_hook_cls(decide, hold_days=None, stop_pct=0.07, target_pct=0.08):
    """One LONG signal on the first bar with len(prices) >= 10, then quiet."""
    class HookStub(BaseStrategy):
        id = 'stub_hook'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)
        exit_hook = True
        fired = False

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or HookStub.fired or not universe:
                return []
            HookStub.fired = True
            t = universe[0]
            ep = float(prices[t].iloc[-1])
            sp = {'hold_days': hold_days} if hold_days else {}
            return [Signal(ticker=t, direction='LONG', entry_price=ep,
                           stop_loss=ep * (1 - stop_pct), target_1=ep * (1 + target_pct),
                           target_2=0.0, target_3=0.0, position_size_pct=0.0,
                           confidence='MED', signal_params=sp)]

        def should_exit(self, position, prices, regime, aux_data=None):
            return decide(position, prices)
    return HookStub


def _mk_plain_cls(stop_pct=0.07, target_pct=0.08):
    class PlainStub(BaseStrategy):
        id = 'stub_plain'
        min_lookback = 5
        active_in_regimes = list(CANONICAL_REGIMES)
        fired = False

        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or PlainStub.fired or not universe:
                return []
            PlainStub.fired = True
            t = universe[0]
            ep = float(prices[t].iloc[-1])
            return [Signal(ticker=t, direction='LONG', entry_price=ep,
                           stop_loss=ep * (1 - stop_pct), target_1=ep * (1 + target_pct),
                           target_2=0.0, target_3=0.0, position_size_pct=0.0, confidence='MED')]
    return PlainStub


def _dataset(n=30):
    dates = pd.date_range('2024-01-01', periods=n, freq='B')
    closes = [100.0 + 0.5 * i for i in range(n)]
    close_wide = pd.DataFrame({'AAA': closes}, index=dates); close_wide.index.name = 'date'
    bars = _bars_from_rows({'AAA': [(c - 0.1, c + 0.2, c - 0.2, c) for c in closes]}, dates)
    regimes = pd.Series({d: 'LOW_VOL' for d in dates})
    return close_wide, bars, regimes, dates, closes


class TestPerBarSimulateOpenBook:
    def _strip(self, trades):
        return [{k: v for k, v in t.items() if k != 'daily_marks'} for t in trades]

    def test_hook_never_firing_equals_simulate_trade_path(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        plain = _run_capture(_mk_plain_cls(), close_wide, bars, regimes, fill_model='same_close')
        hook = _run_capture(_mk_hook_cls(lambda p, x: None), close_wide, bars, regimes, fill_model='same_close')
        assert plain and len(plain) == 1
        assert self._strip(hook) == self._strip(plain)
        assert [round(m[1], 12) for m in hook[0]['daily_marks']] == [round(m[1], 12) for m in plain[0]['daily_marks']]

    def test_hook_exit_lands_in_trade_list(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        entry_idx = 9                                   # first bar with len(prices) >= 10
        exit_day = dates[entry_idx + 4]
        cls = _mk_hook_cls(lambda p, prices: 'z_revert' if prices.index[-1] == exit_day else None)
        trades = _run_capture(cls, close_wide, bars, regimes, fill_model='same_close')
        assert len(trades) == 1
        t = trades[0]
        assert t['exit_reason'] == 'strategy_exit:z_revert'
        assert t['exit_date'] == exit_day.date()
        assert t['holding_days'] == 4
        # flat adverse slippage may still apply under _run_capture (spread costs off,
        # OPENCLAW_BACKTEST_SLIPPAGE default ON) -> compare within 30 bps of the close
        assert abs(t['exit_price'] / closes[entry_idx + 4] - 1.0) < 0.003

    def test_signal_hold_days_caps_hold(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        cls = _mk_hook_cls(lambda p, x: None, hold_days=3)
        trades = _run_capture(cls, close_wide, bars, regimes, fill_model='same_close')
        assert trades[0]['exit_reason'] == 'max_hold' and trades[0]['holding_days'] == 3

    def test_open_fill_model_rejected_for_hook_strategies(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        with pytest.raises(ValueError, match='exit_hook'):
            _run_capture(_mk_hook_cls(lambda p, x: None), close_wide, bars, regimes, fill_model='open')

    def test_trade_open_at_window_end_drains_past_end_dt(self):
        close_wide, bars, regimes, dates, closes = _dataset(n=40)
        cls = _mk_hook_cls(lambda p, x: None, hold_days=5)
        inst = cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
        # entry lands on dates[9]; end the OOS window on dates[11]: the trade
        # must still run to its 5-bar cap on dates[14] like simulate_trade would
        # (simulate_trade walks bars_by_ticker past end_dt; the open book drains).
        with patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0'}):
            out = ub._per_bar_simulate(inst, close_wide, bars, regimes, dates[0], dates[11],
                                       strategy_id='stub_hook', max_hold_days=21,
                                       fill_model='same_close')
        t = out['trades'][0]
        assert t['exit_reason'] == 'max_hold' and t['exit_date'] == dates[14].date()
        assert out['hook_exits'] == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py::TestPerBarSimulateOpenBook -q -p no:cacheprovider`
Expected: `test_hook_never_firing_equals_simulate_trade_path` passes already (both go through `simulate_trade` today — that is the point: it must STILL pass after the change); the other four fail (`exit_reason == 'max_hold'`/no ValueError/no drain).

- [ ] **Step 3: Implement in `_per_bar_simulate`**

(a) In the setup block, after `run_stop_history: dict = {}` add:

```python
    # Exit-hook open-book path (spec §2). Only for instance.exit_hook=True;
    # every other strategy keeps simulate_trade-at-entry byte-identical.
    _use_open_book = bool(getattr(instance, 'exit_hook', False))
    if _use_open_book and fill_model == 'open':
        raise ValueError('exit_hook strategies support fill_model close/same_close only '
                         '(the open-fill bar-inclusion rule is not modelled in the open book)')
    _dt_priority = os.environ.get('OPENCLAW_BT_DOUBLE_TOUCH', 'stop')
    open_book: list = []
    hook_counters: dict = {}
    if _use_open_book:
        from backtest.open_book import OpenTrade, advance_open_book, resolve_hold_cap
```

(b) At the top of the day loop, immediately after `prices_to_date = close_wide.loc[:current_date]` and BEFORE the `if len(prices_to_date) < min_lookback + 5: continue` gate, add:

```python
        if _use_open_book and open_book:
            _rs = regimes.get(current_date, None)
            _rp = {'state': (str(_rs) if _rs is not None and not pd.isna(_rs) else None),
                   'date': (current_date.date() if hasattr(current_date, 'date') else current_date).isoformat()}
            _aux_ob = {'options': {}}
            if load_aux_data is not None:
                try:
                    _aux_ob = load_aux_data(current_date, strategy_id=strategy_id,
                                            run_stop_history=run_stop_history)
                except Exception:
                    _aux_ob = {'options': {}}
            for _ct in advance_open_book(open_book, current_date, bars_by_ticker, prices_to_date,
                                         _rp, _aux_ob, instance,
                                         dt_priority=_dt_priority, counters=hook_counters):
                if _ct['exit_reason'] == 'stop':
                    _sd = pd.Timestamp(_ct['exit_date'])
                    if run_stop_history.get(_ct['ticker']) is None or _sd > run_stop_history[_ct['ticker']]:
                        run_stop_history[_ct['ticker']] = _sd
                trades.append(_ct if _true_mtm else {**_ct, 'daily_marks': []})
```

(c) In the entry block, replace the single line `exit_info = simulate_trade(ticker_bars, fill_date, direction, entry_price, stop_loss, target_1, max_hold_days, include_entry_bar=_include_fill_bar, slippage_bps=_tkr_bps)` and everything through `trades.append({...})` with:

```python
            if _use_open_book:
                _s = float(_tkr_bps) / 10000.0
                open_book.append(OpenTrade(
                    ticker=ticker, direction=direction, entry_date=fill_date,
                    entry_price=entry_price, entry_fill=entry_price * (1.0 + direction * _s),
                    stop_loss=stop_loss, target_1=target_1,
                    hold_cap=resolve_hold_cap(getattr(sig, 'signal_params', None), max_hold_days),
                    entry_regime=str(regime_state),
                    signal_params=dict(getattr(sig, 'signal_params', None) or {}),
                    slippage=_s, prev_mark=entry_price * (1.0 + direction * _s)))
                continue
            exit_info = simulate_trade(ticker_bars, fill_date, direction,
                                       entry_price, stop_loss, target_1, max_hold_days,
                                       include_entry_bar=_include_fill_bar,
                                       slippage_bps=_tkr_bps)
            # ... (existing run_stop_history update and trades.append UNCHANGED)
```

i.e. insert the `if _use_open_book:` block ABOVE the existing `exit_info = simulate_trade(...)` line and leave the existing code below it untouched.

(d) After the day loop ends (before `if bars_raised:`), drain trades still open past `end_dt` — `simulate_trade` walks beyond the OOS window up to `max_hold`, and the open book must match:

```python
    if _use_open_book and open_book:
        for _dt in close_wide.index[close_wide.index > end_dt]:
            if not open_book:
                break
            _rs = regimes.get(_dt, None)
            _rp = {'state': (str(_rs) if _rs is not None and not pd.isna(_rs) else None),
                   'date': _dt.date().isoformat()}
            for _ct in advance_open_book(open_book, _dt, bars_by_ticker, close_wide.loc[:_dt],
                                         _rp, {'options': {}}, instance,
                                         dt_priority=_dt_priority, counters=hook_counters):
                trades.append(_ct if _true_mtm else {**_ct, 'daily_marks': []})
        for _t in open_book:   # ticker has no bar at all after entry
            trades.append({'ticker': _t.ticker, 'direction': 'long' if _t.direction > 0 else 'short',
                           'entry_date': _t.entry_date.date(), 'entry_price': _t.entry_price,
                           'exit_date': _t.entry_date.date(), 'exit_price': _t.entry_price,
                           'exit_reason': 'end_of_data', 'holding_days': 0, 'pnl_pct': 0.0,
                           'entry_regime': _t.entry_regime, 'signal_stop': _t.stop_loss,
                           'signal_target': _t.target_1, 'daily_marks': []})
        open_book.clear()
    if hook_counters.get('hook_exits') or hook_counters.get('hook_raised'):
        _log(f'exit hook: {hook_counters.get("hook_exits", 0)} hook exits, '
             f'{hook_counters.get("hook_raised", 0)} hook errors'
             + (f' (first: {hook_counters["first_hook_raise"]})' if hook_counters.get('first_hook_raise') else ''))
```

(e) Add to the return dict:

```python
        'hook_exits':       hook_counters.get('hook_exits', 0),
        'hook_raised':      hook_counters.get('hook_raised', 0),
        'first_hook_raise': hook_counters.get('first_hook_raise'),
```

- [ ] **Step 4: Run new + guard tests**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py tests/backtest/test_backtest_fill_model.py tests/backtest/test_adverse_slippage.py tests/backtest/test_true_mtm_marks.py tests/backtest/test_backtest_max_hold_config.py -q -p no:cacheprovider`
Expected: all pass. `test_hook_never_firing_equals_simulate_trade_path` is the byte-identity gate between the two paths — if it fails, the open book's mark/holding/exit arithmetic diverged from `simulate_trade`; fix `open_book.py`, never the test.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/backtest/test_open_book.py
git commit -m "backtest: open-book simulation path for exit_hook strategies (bracket → hook → hold_days), non-hook path untouched

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 5: Persist `exit_hook` in the run's `config_json` and log hook counts

**Files:**
- Modify: `src/backtest/unified_backtest.py` — `run_backtest`: the `json.dumps({...})` config block (~line 1253, keys `max_hold_days`…`double_touch`) and the `_log(f'simulation: ...')` line that follows the simulate call
- Test: `tests/backtest/test_open_book.py` (append)

**Interfaces:**
- Produces: `strategy_backtest_runs.config_json.exit_hook` (bool) and `config_json.hook_exits` (int) on every run; log line `[unified_backtest] exit hook: N hook exits, M hook errors` when either is non-zero (already emitted by Task 4 inside `_per_bar_simulate`; nothing extra here).

- [ ] **Step 1: Write the failing test**

```python
class TestConfigJsonExitHook:
    def _config_json_of(self, strategy_cls):
        close_wide, bars, regimes, dates, closes = _dataset()
        import json
        from unittest.mock import MagicMock
        seen = {}
        mock_conn = MagicMock(); mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_conn.__enter__ = lambda s: s; mock_conn.__exit__ = MagicMock(return_value=False)
        def exec_spy(sql, params=None):
            if 'INSERT INTO strategy_backtest_runs' in str(sql):
                seen['params'] = params
        mock_cur.execute.side_effect = exec_spy
        with (
            patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0'}),
            patch('backtest.unified_backtest.load_prices_panels', return_value=(close_wide, bars)),
            patch('backtest.unified_backtest.load_regimes', return_value=regimes),
            patch('backtest.unified_backtest.load_strategy_class', return_value=strategy_cls),
            patch('backtest.unified_backtest.find_strategy_file', return_value='x.py'),
            patch('backtest.unified_backtest._code_sha', return_value='abc123'),
            patch('backtest.unified_backtest.psycopg2.extras.execute_values'),
        ):
            # commit=False: the runs INSERT is still executed on the cursor (then rolled
            # back) and the `if commit:` panel rebuild — which would touch the real DB —
            # is skipped.
            ub.run_backtest(strategy_cls.id, conn=mock_conn, commit=False, fill_model='same_close')
        cfg = next(p for p in seen['params'] if isinstance(p, str) and p.startswith('{') and 'max_hold_days' in p)
        return json.loads(cfg)

    def test_plain_strategy_records_false(self):
        cfg = self._config_json_of(_mk_plain_cls())
        assert cfg['exit_hook'] is False and cfg['hook_exits'] == 0

    def test_hook_strategy_records_true_and_count(self):
        close_wide, bars, regimes, dates, closes = _dataset()
        cls = _mk_hook_cls(lambda p, prices: 'z_revert' if p['days_held'] == 2 else None)
        cfg = self._config_json_of(cls)
        assert cfg['exit_hook'] is True and cfg['hook_exits'] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py::TestConfigJsonExitHook -q -p no:cacheprovider`
Expected: `KeyError: 'exit_hook'`. (If `seen['params']` is missing, the INSERT is issued through a different cursor call in `run_backtest` — read the persist block near `INSERT INTO strategy_backtest_runs` and adapt `exec_spy` to that call; the test's intent is the JSON content, not the call site.)

- [ ] **Step 3: Implement**

In `run_backtest`, locate the dict inside `json.dumps({ 'max_hold_days': max_hold_days, ...` and add two keys after `'double_touch': ...`:

```python
                'exit_hook':   bool(getattr(instance, 'exit_hook', False)),
                'hook_exits':  int(sim.get('hook_exits', 0)),
```

`sim` is the dict bound at `sim = _sim_fn(` (unified_backtest.py:1202) — the return of `_per_bar_simulate`; the keys `hook_exits`/`hook_raised` exist since Task 4; `options_backtest.simulate` does not return them, hence `.get`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py tests/backtest/test_backtest_sortino_calmar_persist.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/backtest/test_open_book.py
git commit -m "backtest: record exit_hook + hook_exits in strategy_backtest_runs.config_json

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 6: Promotion guard — `exit_hook_live_disabled`

**Files:**
- Modify: `src/lib/promotion_service.js` — `_latestPrimaryRun` (~line 123), `computeQualifyingRegimes` (~153), `evaluatePromotionGate` (~174)
- Test: `tests/lib/promotion_exit_hook_guard.test.js` (create; `node --test 'tests/**/*.test.js'` picks it up)

**Interfaces:**
- Consumes: `config_json.exit_hook` (Task 5).
- Produces: `_latestPrimaryRun` returns `exitHook: boolean`; `evaluatePromotionGate` returns `{ pass:false, failedGates:['exit_hook_live_disabled'], ... }` when the run has `exit_hook` and `process.env.OPENCLAW_EXIT_HOOK_LIVE !== '1'` (checked right after `no_backtest`, before sleeves; `force` still bypasses); `computeQualifyingRegimes` returns `qualifying: []` plus `exit_hook_live_disabled: true` in the same case so the Sunday sweep's `blocked` list names the reason.

- [ ] **Step 1: Write the failing test**

```js
// tests/lib/promotion_exit_hook_guard.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { evaluatePromotionGate, computeQualifyingRegimes } = require('../../src/lib/promotion_service');

function mkQuery(runRow, regimeRows) {
  return async (sql) => {
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_backtest_regimes/.test(sql)) return { rows: regimeRows || [] };
    throw new Error(`unexpected query: ${sql}`);
  };
}
const goodSleeves = [{ regime_state: 'LOW_VOL', sharpe: 1.2, trade_count: 150, max_dd_pct: 5, calmar: 2, benchmark_sharpe: null }];
const hookRun  = { run_id: 'r1', total_sharpe: 1.2, total_max_dd_pct: 5, total_trades: 150, config_json: { exit_hook: true, hook_exits: 40 } };
const plainRun = { run_id: 'r2', total_sharpe: 1.2, total_max_dd_pct: 5, total_trades: 150, config_json: { exit_hook: false } };

test('exit_hook run is refused while OPENCLAW_EXIT_HOOK_LIVE is unset', async () => {
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
  assert.strictEqual(r.pass, false);
  assert.deepStrictEqual(r.failedGates, ['exit_hook_live_disabled']);
  const q = await computeQualifyingRegimes({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
  assert.deepStrictEqual(q.qualifying, []);
  assert.strictEqual(q.exit_hook_live_disabled, true);
});

test('exit_hook run passes normally when OPENCLAW_EXIT_HOOK_LIVE=1', async () => {
  process.env.OPENCLAW_EXIT_HOOK_LIVE = '1';
  try {
    const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
    assert.strictEqual(r.pass, true);
    assert.deepStrictEqual(r.qualifyingRegimes, ['LOW_VOL']);
  } finally { delete process.env.OPENCLAW_EXIT_HOOK_LIVE; }
});

test('non-hook run is unaffected; config_json as a JSON string is tolerated', async () => {
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(plainRun, goodSleeves), sid: 'S_y', instrumentClass: 'equity' });
  assert.strictEqual(r.pass, true);
  const strRun = { ...hookRun, config_json: JSON.stringify(hookRun.config_json) };
  const r2 = await evaluatePromotionGate({ dbQuery: mkQuery(strRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity' });
  assert.deepStrictEqual(r2.failedGates, ['exit_hook_live_disabled']);
});

test('force bypasses the guard', async () => {
  delete process.env.OPENCLAW_EXIT_HOOK_LIVE;
  const r = await evaluatePromotionGate({ dbQuery: mkQuery(hookRun, goodSleeves), sid: 'S_x', instrumentClass: 'equity', force: true });
  assert.strictEqual(r.pass, true);
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && node --test tests/lib/promotion_exit_hook_guard.test.js`
Expected: first test fails (`r.pass` is `true`, `failedGates` `[]`).

- [ ] **Step 3: Implement**

In `_latestPrimaryRun`: add `config_json` to the SELECT list and after the `trades = ...` line:

```js
      let cfg = ubt.rows[0].config_json;
      if (typeof cfg === 'string') { try { cfg = JSON.parse(cfg); } catch (_) { cfg = null; } }
      exitHook = !!(cfg && cfg.exit_hook === true);
```

declare `let ... exitHook = false` in the first line of the function and return it: `return { hasRun, runId, sharpe, maxDd, trades, exitHook };`.

Add a module-level helper near `getMinExcessSharpeVsBenchmark`:

```js
// Exit-hook spec §4: a backtest that flattened on BaseStrategy.should_exit
// must not go live until the live mirror (Phase 2) is enabled.
function exitHookLiveEnabled() { return process.env.OPENCLAW_EXIT_HOOK_LIVE === '1'; }
```

In `evaluatePromotionGate`, immediately after the `if (!run.hasRun) { ... }` block:

```js
  if (run.exitHook && !exitHookLiveEnabled()) {
    return { pass: false, failedGates: ['exit_hook_live_disabled'], sharpe, maxDd, thresholds, qualifyingRegimes: [] };
  }
```

In `computeQualifyingRegimes`, immediately after `if (!run.hasRun) return out;`:

```js
  if (run.exitHook && !exitHookLiveEnabled()) { out.exit_hook_live_disabled = true; return out; }
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /root/openclaw && node --test tests/lib/promotion_exit_hook_guard.test.js && node tests/test_promotion_service_gate.js; echo "gate rc=$?"`
Expected: the new file passes. `tests/test_promotion_service_gate.js` has a PRE-EXISTING failure (ledgered 2026-08-24); confirm its failing assertion is the same one as before your change (`git stash; node tests/test_promotion_service_gate.js; git stash pop` to compare) — do not fix it here.

- [ ] **Step 5: Commit**

```bash
git add src/lib/promotion_service.js tests/lib/promotion_exit_hook_guard.test.js
git commit -m "promotion: refuse candidate→live for exit_hook runs until OPENCLAW_EXIT_HOOK_LIVE=1 (exit-hook spec §4)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 7: X1 — `S_coint_pairs_sector_v2.should_exit`

**Files:**
- Modify: `src/strategies/implementations/S_coint_pairs_sector_v2.py` — class attrs (after `TARGET_R`), new method after `_pair_leg_levels`
- Test: `tests/strategies/test_coint_pairs_v2.py` (append)

**Interfaces:**
- Consumes: `BaseStrategy.exit_hook`/`should_exit` (Task 1); module-level `_ledger_path()` and `_REQUIRED_LEDGER_COLUMNS` (existing). NOTE: `_load_approved_pairs` cannot be reused for decoherence — it returns an empty frame both when NO snapshot is readable (⇒ hold) and when the latest snapshot approves nothing (⇒ decohered); the new helper below tells them apart.
- Produces: `CointPairsSectorV2.exit_hook = True`, `Z_EXIT = 0.5`, `should_exit(position, prices, regime, aux_data=None) -> 'z_revert' | 'pair_decohered' | None`; module-level `_latest_snapshot_has_pair(as_of_date, ticker_a, ticker_b) -> bool | None` (`None` = no readable snapshot with `as_of <= as_of_date`).

Semantics (spec §5): recompute the log-spread z of the position's pair over the last `Z_WINDOW` bars of `prices` (both legs, entry-time `beta`/`alpha` from `signal_params`); `'z_revert'` when `|z_t| <= Z_EXIT` or `sign(z_t) != sign(signal_params['z'])`; `'pair_decohered'` when the latest ledger snapshot `as_of <= prices.index[-1]` does not contain the pair as approved (either ordering); `None` when a leg is missing from `prices`, the window is short/contains non-positive closes, or `signal_params` lacks `pair`/`beta`/`alpha`/`z`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/strategies/test_coint_pairs_v2.py`:

```python
# ─────────────────────────────────────────────────────────────────────────
# Exit hook (spec 2026-08-28 §5): z-reversion + decoherence exits
# ─────────────────────────────────────────────────────────────────────────
def _entered_pair(tmp_path, monkeypatch, tail_values=(0.0, 0.05), seed=123, beta=0.75, alpha=0.10):
    """Fire the ZZTAA/ZZTBB pair on the final bar and return (strategy, prices, signals, ledger_path)."""
    dates = _dates(120)
    last = dates[-1]
    spread = _tail_spread(seed=seed, tail_values=list(tail_values))
    frame = _pair_frame(dates, 'ZZTAA', 'ZZTBB', beta=beta, alpha=alpha, spread=spread, log_b_seed=555)
    ledger_path = _write_ledger(tmp_path, [
        _ledger_row(last, 'ZZTAA', 'ZZTBB', beta=beta, alpha=alpha, approved=True, half_life_days=6.0)])
    strat = CointPairsSectorV2()
    monkeypatch.setenv('OPENCLAW_PAIR_LEDGER', ledger_path)
    signals = strat.generate_signals(frame, {'state': 'LOW_VOL'}, list(frame.columns))
    assert len(signals) == 2
    return strat, frame, signals, ledger_path


def _position(sig, entry_date, days_held=1):
    return {'ticker': sig.ticker, 'direction': sig.direction, 'entry_price': sig.entry_price,
            'entry_date': entry_date, 'days_held': days_held, 'stop_loss': sig.stop_loss,
            'target_1': sig.target_1, 'signal_params': dict(sig.signal_params)}


def _extend(frame, log_spread_next, beta, alpha, log_b_step=0.0):
    """Append one bar so that the pair's log spread equals log_spread_next."""
    next_date = frame.index[-1] + pd.tseries.offsets.BDay(1)
    log_b = float(np.log(frame['ZZTBB'].iloc[-1])) + log_b_step
    log_a = log_spread_next + beta * log_b + alpha
    row = pd.DataFrame({'ZZTAA': [float(np.exp(log_a))], 'ZZTBB': [float(np.exp(log_b))]}, index=[next_date])
    return pd.concat([frame, row])


def test_should_exit_is_armed_and_holds_while_spread_stays_rich(tmp_path, monkeypatch):
    strat, frame, signals, _ = _entered_pair(tmp_path, monkeypatch)
    assert CointPairsSectorV2.exit_hook is True
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    # same panel as entry (z ~ 2.39): still rich -> hold
    assert strat.should_exit(_position(a_sig, frame.index[-1]), frame, {'state': 'LOW_VOL'}) is None


def test_should_exit_z_revert_when_spread_returns_to_mean(tmp_path, monkeypatch):
    strat, frame, signals, _ = _entered_pair(tmp_path, monkeypatch)
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    b_sig = next(s for s in signals if s.ticker == 'ZZTBB')
    # push the log spread back to the window mean -> |z| <= 0.5
    sp = a_sig.signal_params
    win = (np.log(frame['ZZTAA']) - sp['beta'] * np.log(frame['ZZTBB']) - sp['alpha']).iloc[-CointPairsSectorV2.Z_WINDOW:]
    frame2 = _extend(frame, float(win.mean()), sp['beta'], sp['alpha'])
    pos_a = _position(a_sig, frame.index[-1], days_held=1)
    pos_b = _position(b_sig, frame.index[-1], days_held=1)
    assert strat.should_exit(pos_a, frame2, {'state': 'LOW_VOL'}) == 'z_revert'
    assert strat.should_exit(pos_b, frame2, {'state': 'LOW_VOL'}) == 'z_revert'   # both legs agree


def test_should_exit_z_revert_on_sign_flip(tmp_path, monkeypatch):
    strat, frame, signals, _ = _entered_pair(tmp_path, monkeypatch)
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    sp = a_sig.signal_params
    win = (np.log(frame['ZZTAA']) - sp['beta'] * np.log(frame['ZZTBB']) - sp['alpha']).iloc[-CointPairsSectorV2.Z_WINDOW:]
    # overshoot far below the mean: |z| > 0.5 but sign flipped relative to entry (z_entry > 0)
    frame2 = _extend(frame, float(win.mean() - 3.0 * win.std(ddof=1)), sp['beta'], sp['alpha'])
    assert strat.should_exit(_position(a_sig, frame.index[-1]), frame2, {'state': 'LOW_VOL'}) == 'z_revert'


def test_should_exit_pair_decohered_when_dropped_from_ledger(tmp_path, monkeypatch):
    strat, frame, signals, ledger_path = _entered_pair(tmp_path, monkeypatch)
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    sp = a_sig.signal_params
    # a later scan (next bar's date) that no longer approves the pair
    next_date = frame.index[-1] + pd.tseries.offsets.BDay(1)
    _write_ledger(tmp_path, [
        _ledger_row(frame.index[-1], 'ZZTAA', 'ZZTBB', beta=sp['beta'], alpha=sp['alpha'], approved=True, half_life_days=6.0),
        _ledger_row(next_date, 'ZZTAA', 'ZZTBB', beta=sp['beta'], alpha=sp['alpha'], approved=False, half_life_days=6.0),
    ])
    frame2 = _extend(frame, 0.05, sp['beta'], sp['alpha'])   # spread still rich (no z_revert)
    assert strat.should_exit(_position(a_sig, frame.index[-1]), frame2, {'state': 'LOW_VOL'}) == 'pair_decohered'


def test_should_exit_ignores_future_ledger_rows(tmp_path, monkeypatch):
    strat, frame, signals, ledger_path = _entered_pair(tmp_path, monkeypatch)
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    sp = a_sig.signal_params
    future = frame.index[-1] + pd.tseries.offsets.BDay(5)
    _write_ledger(tmp_path, [
        _ledger_row(frame.index[-1], 'ZZTAA', 'ZZTBB', beta=sp['beta'], alpha=sp['alpha'], approved=True, half_life_days=6.0),
        _ledger_row(future, 'ZZTAA', 'ZZTBB', beta=sp['beta'], alpha=sp['alpha'], approved=False, half_life_days=6.0),
    ])
    frame2 = _extend(frame, 0.05, sp['beta'], sp['alpha'])
    assert strat.should_exit(_position(a_sig, frame.index[-1]), frame2, {'state': 'LOW_VOL'}) is None


def test_should_exit_none_when_leg_missing_or_params_incomplete(tmp_path, monkeypatch):
    strat, frame, signals, _ = _entered_pair(tmp_path, monkeypatch)
    a_sig = next(s for s in signals if s.ticker == 'ZZTAA')
    pos = _position(a_sig, frame.index[-1])
    assert strat.should_exit(pos, frame.drop(columns=['ZZTBB']), {'state': 'LOW_VOL'}) is None
    bad = dict(pos); bad['signal_params'] = {k: v for k, v in pos['signal_params'].items() if k != 'beta'}
    assert strat.should_exit(bad, frame, {'state': 'LOW_VOL'}) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/strategies/test_coint_pairs_v2.py -q -p no:cacheprovider -k should_exit`
Expected: `test_should_exit_is_armed...` fails on `exit_hook is True`; the others fail because the base `should_exit` returns `None` (assertions expecting `'z_revert'` / `'pair_decohered'`).

- [ ] **Step 3: Implement**

Class attributes, after `TARGET_R         = 2.0`:

```python
    # Per-bar exit hook (spec 2026-08-28 §5): flatten on reversion or decoherence.
    exit_hook        = True
    Z_EXIT           = 0.5
```

Method, after `_pair_leg_levels`:

```python
    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict = None):
        """Exit-hook: 'z_revert' when the pair's log-spread z (entry-time
        beta/alpha, rolling Z_WINDOW std) is within Z_EXIT of the mean or has
        flipped sign since entry; 'pair_decohered' when the latest ledger
        snapshot as_of <= today no longer approves the pair; None otherwise
        (including any missing leg / short window / incomplete params —
        hold, the bracket and hold_days still protect)."""
        sp = (position or {}).get('signal_params') or {}
        pair = sp.get('pair')
        try:
            beta = float(sp['beta']); alpha = float(sp['alpha']); z_entry = float(sp['z'])
            ticker_a, ticker_b = str(pair).split('/', 1)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        if prices is None or prices.empty or ticker_a not in prices.columns or ticker_b not in prices.columns:
            return None
        both = prices[[ticker_a, ticker_b]].dropna(how='any')
        if len(both) < self.Z_WINDOW:
            return None
        window = both.iloc[-self.Z_WINDOW:]
        if window.index[-1] != prices.index[-1]:
            return None                       # no aligned bar today
        wa = window[ticker_a].to_numpy(dtype=float)
        wb = window[ticker_b].to_numpy(dtype=float)
        if (wa <= 0.0).any() or (wb <= 0.0).any():
            return None
        spread = np.log(wa) - beta * np.log(wb) - alpha
        std = float(np.std(spread, ddof=1))
        if not np.isfinite(std) or std <= 0.0:
            return None
        z_t = float((spread[-1] - np.mean(spread)) / std)
        if abs(z_t) <= self.Z_EXIT or (z_t > 0.0) != (z_entry > 0.0):
            return 'z_revert'
        has = _latest_snapshot_has_pair(pd.Timestamp(prices.index[-1]), ticker_a, ticker_b)
        if has is False:
            return 'pair_decohered'
        return None                           # True (still approved) or None (no snapshot -> hold)
```

Module-level helper, placed directly after `_load_approved_pairs`:

```python
def _latest_snapshot_has_pair(as_of_date: pd.Timestamp, ticker_a: str, ticker_b: str):
    """True/False = the LATEST ledger snapshot with as_of <= as_of_date does /
    does not approve the (unordered) pair; None = no readable snapshot at all
    (missing file, read error, missing columns, no rows <= as_of_date) — the
    caller must HOLD on None, never treat it as decoherence."""
    path = _ledger_path()
    if not path.exists():
        return None
    try:
        import pyarrow.parquet as pq
        df = pq.read_table(str(path), filters=[('as_of', '<=', as_of_date)]).to_pandas()
    except Exception as e:
        print(f'[debug] pair_ledger read failed in should_exit ({path}): {e}', file=sys.stderr)
        return None
    if df.empty or any(c not in df.columns for c in ('as_of', 'ticker_a', 'ticker_b', 'approved')):
        return None
    latest = pd.to_datetime(df['as_of']).max()
    snap = df[(pd.to_datetime(df['as_of']) == latest) & (df['approved'] == True)]  # noqa: E712
    keys = set(zip(snap['ticker_a'].astype(str), snap['ticker_b'].astype(str)))
    return (ticker_a, ticker_b) in keys or (ticker_b, ticker_a) in keys
```

Note: the z here uses the last `Z_WINDOW` spread values ending TODAY (the entry path's `win_t`), so on the entry bar itself `should_exit` reproduces the entry z — the first test relies on that.

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/strategies/test_coint_pairs_v2.py tests/strategies/test_exit_hook_interface.py -q -p no:cacheprovider`
Expected: all pass (16 existing + 6 new + 4 interface). If `test_should_exit_pair_decohered...` returns `None`, check that `_write_ledger` overwrote the same `pair_ledger.parquet` path the env var points to.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/implementations/S_coint_pairs_sector_v2.py tests/strategies/test_coint_pairs_v2.py
git commit -m "S_coint_pairs_sector_v2: exit hook — z_revert (|z|<=0.5 or sign flip) and pair_decohered (ledger drop)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 8: Docs, spec refinement, and the X1 re-run

**Files:**
- Modify: `docs/specs/2026-08-28-per-bar-exit-hook-spec.md` (§4 refinement note), `docs/archive/changelog.md` (new entry under `## Recent Changes`), `src/strategies/implementations/S_coint_pairs_sector_v2.py` (STATUS banner), `docs/superpowers/plans/2026-08-24-five-repo-adoptions.md` (append result)
- No tests (documentation + one operational run).

- [ ] **Step 1: Spec refinement note**

Insert at the end of spec §4:

```markdown
**Refinement (Phase 1 plan, 2026-08-28):** `unified_backtest` only READS the manifest today; introducing a manifest write from the backtest would be a new pattern. The guard therefore reads `strategy_backtest_runs.config_json.exit_hook` (written by every run since Phase 1) via `_latestPrimaryRun` instead of manifest `metadata.exit_hook`. Behaviour is the same: the primary run that would be promoted declares whether it relied on the hook.
```

- [ ] **Step 2: X1 banner**

Replace the four STATUS lines at the top of the X1 module docstring with:

```
STATUS: PARKED 2026-08-28 pending live exit hook (Phase 2). Backtest exit hook
LANDED (Phase 1, docs/superpowers/plans/2026-08-28-exit-hook-phase1.md); promotion
is refused by `exit_hook_live_disabled` until OPENCLAW_EXIT_HOOK_LIVE=1.
```

- [ ] **Step 3: Re-run X1 as a transient unit (outside 13:00–20:15 UTC)**

```bash
cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py tests/strategies/test_coint_pairs_v2.py -q -p no:cacheprovider   # must be green first
systemd-run --unit=x1-backtest-3 --description='X1 backtest #3: exit hook (z_revert/pair_decohered), hold_days honored' \
  --property=Nice=19 --property=CPUQuota=100% --property=MemoryMax=3500M --property=RuntimeMaxSec=2h \
  --property=WorkingDirectory=/root/openclaw --property=EnvironmentFile=/root/openclaw/.env \
  --setenv=PYTHONPATH=/root/openclaw/src --setenv=PYTHONUNBUFFERED=1 \
  --setenv=OMP_NUM_THREADS=1 --setenv=OPENBLAS_NUM_THREADS=1 --setenv=MKL_NUM_THREADS=1 --setenv=NUMEXPR_MAX_THREADS=1 \
  /usr/bin/python3 -m backtest.unified_backtest --strategy-file /root/openclaw/src/strategies/implementations/S_coint_pairs_sector_v2.py --universe-cap tier_liquid --start-date 2023-09-04 --max-hold-days 30
```

`--max-hold-days 30` is the CAP; per-pair `hold_days = min(3·half_life, 30)` now governs. Expect ~10 min; the unit may OOM-kill AFTER `wrote run_id=` (known: in-process `backtest_panel.rebuild`, `~/.learnings/ERRORS.md`) — the run is committed; rebuild the panel with `PYTHONPATH=src python3 -c "from dotenv import load_dotenv; load_dotenv('/root/openclaw/.env'); from backtest.backtest_panel import rebuild; print(rebuild('S_coint_pairs_sector_v2'))"`.

Read out with `python3 /tmp/claude-0/-root/103744e0-b3a6-4124-8ba6-ab864694f0fe/scratchpad/x1_compare.py <run_id>` (or the equivalent SQL: `strategy_backtest_runs`, `strategy_backtest_regimes`, `strategy_backtest_trades` grouped by `exit_reason`). Success criteria for the FEATURE (not the strategy): `config_json.exit_hook = true`, `exit_reason` values include `strategy_exit:z_revert` (and possibly `strategy_exit:pair_decohered`), median `holding_days` well below 25.

- [ ] **Step 4: Changelog + plan-doc result**

Add under `## Recent Changes` in `docs/archive/changelog.md`:

```markdown
- **2026-08-28: per-bar exit hook — Phase 1 (backtest) landed.** `BaseStrategy.exit_hook` + `should_exit()`; `backtest/open_book.py` stepper (bracket → hook → time) used only by opt-in strategies, all others byte-identical (equivalence test); per-signal `hold_days` honored; `config_json.exit_hook/hook_exits`; `promotion_service` refuses `exit_hook` runs until `OPENCLAW_EXIT_HOOK_LIVE=1` (`exit_hook_live_disabled`). X1 got `z_revert`/`pair_decohered` exits; run #3 result: <fill from Task 8 step 3>. Spec `docs/specs/2026-08-28-per-bar-exit-hook-spec.md`; Phase 2 (live `update_pnl` mirror) still owed.
```

Append the run #3 table (same columns as the run 1/2 table) to the `2026-08-28` section of `docs/superpowers/plans/2026-08-24-five-repo-adoptions.md`.

- [ ] **Step 5: Commit**

```bash
git add docs/specs/2026-08-28-per-bar-exit-hook-spec.md docs/archive/changelog.md src/strategies/implementations/S_coint_pairs_sector_v2.py docs/superpowers/plans/2026-08-24-five-repo-adoptions.md
git commit -m "docs: exit-hook Phase 1 landed — spec §4 refinement, changelog, X1 run #3 result

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

## Self-review (done at authoring)

- **Spec coverage:** §1 → Task 1 (+ hold_days in Tasks 3/4); §2 → Tasks 2–4 (order, marks, drain past `end_dt`, fill-model restriction, counters, non-hook identity); §4 → Tasks 5–6 (with the config_json refinement); §5 Phase-1 items → Tasks 3, 4, 7; §6 Phase 1 done-criteria → Task 8. §3 and the parity/live tests are Phase 2 (separate plan).
- **Type consistency:** `advance_open_book(open_book, current_date, bars_by_ticker, prices_to_date, regime_payload, aux, instance, *, dt_priority, counters)` is used identically in Tasks 3 and 4; `OpenTrade` field names match between Task 3's dataclass and Task 4's constructor call; `resolve_hold_cap(signal_params, max_hold_days)` matches; reason prefix `'strategy_exit:'` is spelled the same in Tasks 3, 4, 7 and the spec.
- **Placeholders:** the single `<fill from Task 8 step 3>` in the changelog text is an explicit instruction to paste the run result, not an implementation gap.
