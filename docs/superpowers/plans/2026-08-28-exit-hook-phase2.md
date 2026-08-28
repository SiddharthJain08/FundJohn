# Per-bar Exit Hook — Phase 2 (live mirror) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evaluate `BaseStrategy.should_exit` and the per-signal time stop for open live positions of `exit_hook` strategies inside `engine.update_pnl`, behind `OPENCLAW_EXIT_HOOK_LIVE`, with observability and a parity proof against the Phase 1 backtest path.

**Architecture:** `update_pnl` gains optional `strategies/regime/aux_data` kwargs that `main()` already holds; for each open row of an `exit_hook` strategy, after the existing stop/target inference, it calls the hook and then the time stop, and reuses the existing close path (UPSERT + status + post-commit OUE). `days_held` for the hook is a bar count on the prices index (parity with the backtest's `holding_days`). A shared `configured_max_hold_days` helper serves both engines. A parity test drives the same fixture through `_per_bar_simulate` and a day-by-day `update_pnl` harness; a replay script exercises the live branch on real data with no side effects. The digest gains one line derived from `signal_pnl`.

**Tech Stack:** Python 3 / pandas / psycopg2 (RealDictCursor rows) / pytest; Node 22 `node --test`; systemd (pipeline units read `/root/openclaw/.env`).

**Spec:** `docs/specs/2026-08-28-per-bar-exit-hook-phase2-spec.md` (parent: `docs/specs/2026-08-28-per-bar-exit-hook-spec.md`). Read Phase 2 §0–§4 before starting; §6 lists the recorded decisions (D4 bars, D6 hook-strategies-only time stop, D11 flag `'1'` only, D14 verification).

## Global Constraints

- `OPENCLAW_EXIT_HOOK_LIVE` default `'0'`; only the exact string `'1'` enables. With any other value `update_pnl` must be byte-identical to today (existing tests are the guard: `tests/execution/test_engine_oue_ordering.py`, `test_engine_run_stats.py`, `test_dry_run_dataflow.py`).
- Order per open row: existing stop → existing target → hook → time stop; later steps run only while `close_reason is None`. Hook exceptions ⇒ HOLD + counted; never exit on error.
- Close reasons: exactly `'strategy_exit:<reason>'` and `'max_hold'`; closes reuse the existing `signal_pnl` UPSERT + `execution_signals` status update; ids join `newly_closed_ids`.
- `position['days_held']` = trading bars = `count(prices.index in (entry_date, run_date])`; `signal_pnl.days_held` (calendar) is untouched.
- Time stop applies to `exit_hook` strategies only (D6). Non-hook strategies: no behaviour change anywhere.
- `update_pnl`'s return stays `(n_updates, newly_closed_ids)`; counters go to `engine.LAST_EXIT_HOOK_STATS`.
- The backtest non-hook path stays byte-identical: `tests/backtest/test_open_book.py::test_plain_strategy_matches_pre_phase1_golden` and `tests/backtest/test_backtest_max_hold_config.py` must pass unchanged.
- Run tests with `PYTHONPATH=src python3 -m pytest <files> -q -p no:cacheprovider`; JS with `node --test <file>`. NEVER the full suite (`npm test` / bare `pytest`) — tests reach the real DB and a live fleet. Every new test that calls into `engine` must run with `OPENCLAW_BACKTEST_COUPLED_RECS=0` in `patch.dict(os.environ)` (the resolver opens its own DB connection when it is `'1'`).
- Never `source .env`; never modify `.env` in this plan (the flag flip is an operator action after §4 verification); no heavy compute 13:00–20:15 UTC (the replay script is read-only but loads the prices panel — run it outside the lane).
- Commit per task on `main` with the given message + trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`; `git push origin main` after each; never `git reset --hard`; never `git add` the pre-existing dirty `src/strategies/manifest.json` / `strategy_signatures.json`.

## File structure

| file | responsibility |
|---|---|
| `src/execution/regime_param_resolver.py` (modify) | `configured_max_hold_days(strategy_id) -> int` (shared helper) |
| `src/backtest/unified_backtest.py` (modify) | `_configured_max_hold_days` delegates; day loop builds the full `regime_payload` before the open-book advance |
| `src/execution/engine.py` (modify) | `update_pnl` kwargs/flag/`signal_params`/hook/time stop/`LAST_EXIT_HOOK_STATS`; `main()` wiring + summary log + `execution_runs.errors` entry; `_exit_hook_run_summary` helper |
| `src/strategies/base.py` (modify) | `should_exit` docstring: rely on `regime['state']` only |
| `src/engine/daily-health-digest.js` (modify) | `exitHookLine(rows)` pure helper + query + line in the digest |
| `scripts/exit_hook_live_replay.py` (create) | live-branch replay on real data (read-only) |
| `tests/execution/test_configured_max_hold_days.py` (create) | Task 1 |
| `tests/execution/test_update_pnl_exit_hook.py` (create) | Tasks 2–3 |
| `tests/backtest/test_open_book.py` (modify) | Task 4 (SHORT e2e, full regime payload) |
| `tests/execution/test_exit_hook_live_parity.py` (create) | Task 5 |
| `tests/engine/test_daily_health_digest_exit_hook.test.js` (create) | Task 6 |
| `tests/scripts/test_exit_hook_live_replay.py` (create) | Task 7 |
| spec §4/§6, changelog, `docs/superpowers/plans/2026-08-24-five-repo-adoptions.md` (modify) | Task 8 |

---

### Task 1: Shared `configured_max_hold_days` helper

**Files:**
- Modify: `src/execution/regime_param_resolver.py` (append after `max_hold_days_override`, ~line 150)
- Modify: `src/backtest/unified_backtest.py:1104-1124` (`_configured_max_hold_days`)
- Test: `tests/execution/test_configured_max_hold_days.py` (create)

**Interfaces:**
- Produces: `execution.regime_param_resolver.configured_max_hold_days(strategy_id: str, *, default: int = 21, log=None) -> int` — MAX over `CANONICAL_REGIMES` of non-null `max_hold_days_override(strategy_id, r)`; `default` when the coupling gate is off (`regime_param_override.gate_on()` false), when no value is set, or on any exception (logged via `log(msg)` when provided).
- Consumes: existing `max_hold_days_override`, `regime_param_override.gate_on`, `strategies.base.CANONICAL_REGIMES`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/execution/test_configured_max_hold_days.py
"""Phase 2 §2.6: one max-hold resolution shared by backtest and live."""
from __future__ import annotations

import os
from unittest.mock import patch

from execution import regime_param_resolver as rpr
import backtest.unified_backtest as ub


def test_gate_off_returns_default():
    with patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}):
        assert rpr.configured_max_hold_days('S_x') == 21
        assert rpr.configured_max_hold_days('S_x', default=30) == 30


def test_gate_on_takes_max_over_regimes():
    vals = {'LOW_VOL': 10, 'TRANSITIONING': None, 'HIGH_VOL': 25, 'CRISIS': 5}
    with (
        patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '1'}),
        patch.object(rpr, 'max_hold_days_override', side_effect=lambda sid, r: vals[r]),
    ):
        assert rpr.configured_max_hold_days('S_x') == 25


def test_gate_on_no_values_returns_default_and_failure_logs():
    with (
        patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '1'}),
        patch.object(rpr, 'max_hold_days_override', return_value=None),
    ):
        assert rpr.configured_max_hold_days('S_x') == 21
    seen = []
    with (
        patch.dict(os.environ, {'OPENCLAW_BACKTEST_COUPLED_RECS': '1'}),
        patch.object(rpr, 'max_hold_days_override', side_effect=RuntimeError('db down')),
    ):
        assert rpr.configured_max_hold_days('S_x', log=seen.append) == 21
    assert seen and 'db down' in seen[0]


def test_backtest_delegates_to_shared_helper():
    with patch.object(rpr, 'configured_max_hold_days', return_value=17) as m:
        assert ub._configured_max_hold_days('S_x') == 17
    m.assert_called_once()
    assert m.call_args.args[0] == 'S_x'
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_configured_max_hold_days.py -q -p no:cacheprovider`
Expected: `AttributeError: module ... has no attribute 'configured_max_hold_days'` (3 tests) and the delegate test fails.

- [ ] **Step 3: Implement**

Append to `src/execution/regime_param_resolver.py`:

```python
def configured_max_hold_days(strategy_id: str, *, default: int = 21, log=None) -> int:
    """Strategy-configured hold horizon shared by the backtest engine
    (unified_backtest._configured_max_hold_days) and the live exit-hook time
    stop (engine.update_pnl). MAX of the non-null per-regime
    strategy_regime_params.max_hold_days values; `default` when the coupling
    gate (OPENCLAW_BACKTEST_COUPLED_RECS) is off, when nothing is set, or on
    any lookup failure (reported through `log` when given)."""
    from execution import regime_param_override
    from strategies.base import CANONICAL_REGIMES
    if not regime_param_override.gate_on():
        return int(default)
    try:
        vals = [max_hold_days_override(strategy_id, r) for r in CANONICAL_REGIMES]
        vals = [int(v) for v in vals if v]
        return max(vals) if vals else int(default)
    except Exception as e:  # lookup plumbing only; never fail the caller
        if log is not None:
            log(f'{strategy_id}: configured max_hold lookup failed '
                f'({type(e).__name__}: {e}); using default {default}')
        return int(default)
```

Replace the body of `unified_backtest._configured_max_hold_days` (keep its docstring) with:

```python
    from execution import regime_param_resolver as rpr
    return rpr.configured_max_hold_days(strategy_id, default=DEFAULT_MAX_HOLD_DAYS, log=_log)
```

- [ ] **Step 4: Run new + guard tests**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_configured_max_hold_days.py tests/backtest/test_backtest_max_hold_config.py -q -p no:cacheprovider`
Expected: all pass (the guard file pins the backtest's max-hold behaviour).

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_param_resolver.py src/backtest/unified_backtest.py tests/execution/test_configured_max_hold_days.py
git commit -m "execution: configured_max_hold_days shared by backtest and live (exit-hook phase 2 §2.6)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 2: `update_pnl` — hook + time stop behind the flag

**Files:**
- Modify: `src/execution/engine.py` — `update_pnl` (:1896–2045): signature, SELECT, per-row block after the target inference (before `try:` at the UPSERT), module-level `LAST_EXIT_HOOK_STATS`
- Test: `tests/execution/test_update_pnl_exit_hook.py` (create)

**Interfaces:**
- Produces: `update_pnl(cur, prices, run_date, *, strategies=None, regime=None, aux_data=None) -> tuple[int, list]` (unchanged return); `engine.LAST_EXIT_HOOK_STATS: dict` with keys `enabled` (bool), `strategy_exit`, `max_hold`, `hook_raised`, `first_hook_raise`, `loaded_on_demand`, `hook_load_failed`, `rows_evaluated` — reset at the start of every call; `engine._bars_held(prices, entry_dt, run_date) -> int | None` (None when the index is not a DatetimeIndex); `engine._exit_hook_enabled() -> bool`.
- Consumes: Task 1 `configured_max_hold_days`; `strategies.registry.load_strategy_class`; `backtest.open_book.resolve_hold_cap` semantics re-implemented locally as `_hold_cap(signal_params, configured)` (no import from backtest into the engine).

- [ ] **Step 1: Write the failing tests**

```python
# tests/execution/test_update_pnl_exit_hook.py
"""Phase 2 §2.1–§2.6: exit hook + time stop inside engine.update_pnl."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from execution import engine  # noqa: E402
from strategies.base import BaseStrategy, CANONICAL_REGIMES  # noqa: E402

ENV = {'OPENCLAW_EXIT_HOOK_LIVE': '1', 'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}


class _FakeCursor:
    """RealDictCursor stand-in: canned open rows on the status='open' SELECT,
    records every execute with params, fetches [] otherwise."""
    def __init__(self, open_rows):
        self._open_rows = open_rows
        self._fetch = []
        self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._fetch = list(self._open_rows) if "status = 'open'" in sql else []
    def fetchall(self):
        return self._fetch


def _panel(n=10, px=100.0, ticker='AAPL', start='2026-05-04'):
    idx = pd.bdate_range(start, periods=n)          # trading days only
    return pd.DataFrame({ticker: [px] * n}, index=idx)


def _row(**kw):
    base = {'id': 'sig-1', 'strategy_id': 'S_hook', 'ticker': 'AAPL', 'direction': 'LONG',
            'entry_price': 100.0, 'mark_entry_price': 100.5, 'target_date': date(2026, 5, 6),
            'lifecycle_state': 'FILLED', 'stop_loss': 90.0, 'target_1': 120.0,
            'signal_date': date(2026, 5, 5), 'signal_params': {'hold_days': 4, 'k': 1}}
    base.update(kw)
    return base


def _mk(decide, exit_hook=True, sid='S_hook'):
    class H(BaseStrategy):
        id = sid
        active_in_regimes = list(CANONICAL_REGIMES)
        calls = []
        def generate_signals(self, prices, regime, universe, aux_data=None):
            return []
    if exit_hook:
        H.exit_hook = True
        def should_exit(self, position, prices, regime, aux_data=None):
            H.calls.append((position, prices.index[-1], regime))
            return decide(position, prices)
        H.should_exit = should_exit
    return H()


def _closes(cur):
    """(close_reason, close_status) pairs written by the signal_pnl UPSERTs."""
    out = []
    for sql, params in cur.executed:
        if 'INSERT INTO signal_pnl' in sql:
            out.append((params[10], params[7]))
    return out


def test_flag_off_never_calls_hook_and_is_byte_identical():
    strat = _mk(lambda p, x: 'boom')
    cur_on_env_off = _FakeCursor([_row()])
    with patch.dict(os.environ, {**ENV, 'OPENCLAW_EXIT_HOOK_LIVE': '0'}):
        n, closed = engine.update_pnl(cur_on_env_off, _panel(), date(2026, 5, 13),
                                      strategies=[strat], regime={'state': 'LOW_VOL'})
    assert closed == [] and type(strat).calls == []
    assert engine.LAST_EXIT_HOOK_STATS['enabled'] is False
    # kwargs omitted entirely (legacy callers) → same executes
    cur_legacy = _FakeCursor([_row()])
    with patch.dict(os.environ, {**ENV, 'OPENCLAW_EXIT_HOOK_LIVE': '0'}):
        engine.update_pnl(cur_legacy, _panel(), date(2026, 5, 13))
    assert [s for s, _ in cur_legacy.executed] == [s for s, _ in cur_on_env_off.executed]


def test_hook_reason_closes_with_prefixed_reason_and_position_contract():
    seen = {}
    def decide(position, prices):
        seen.update(position); return 'z_revert'
    strat = _mk(decide)
    cur = _FakeCursor([_row()])
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13),
                                      strategies=[strat], regime={'state': 'LOW_VOL'}, aux_data={'a': 1})
    assert closed == ['sig-1']
    assert _closes(cur) == [('strategy_exit:z_revert', 'closed')]
    assert any('UPDATE execution_signals SET status' in s for s, _ in cur.executed)
    # spec §1 contract: entry_price = mark, entry_date = target_date, days_held = bars
    assert seen['entry_price'] == 100.5 and seen['entry_date'] == date(2026, 5, 6)
    assert seen['direction'] == 'LONG' and seen['signal_params'] == {'hold_days': 4, 'k': 1}
    assert seen['stop_loss'] == 90.0 and seen['target_1'] == 120.0
    # bars in (2026-05-06, 2026-05-13]: 05-07,08,11,12,13 = 5 (weekend 09/10 excluded)
    assert seen['days_held'] == 5
    assert type(strat).calls[0][2] == {'state': 'LOW_VOL'}
    assert engine.LAST_EXIT_HOOK_STATS['strategy_exit'] == 1


def test_stop_inference_beats_hook():
    strat = _mk(lambda p, x: 'z_revert')
    cur = _FakeCursor([_row()])
    with patch.dict(os.environ, ENV):
        engine.update_pnl(cur, _panel(px=85.0), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert _closes(cur) == [('stop_loss', 'closed')]
    assert type(strat).calls == []


def test_raising_hook_holds_and_counts():
    def boom(p, x): raise RuntimeError('kaboom')
    strat = _mk(boom)
    cur = _FakeCursor([_row(signal_params={'hold_days': 30})])
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert closed == [] and _closes(cur) == [(None, 'open')]
    assert engine.LAST_EXIT_HOOK_STATS['hook_raised'] == 1
    assert engine.LAST_EXIT_HOOK_STATS['first_hook_raise'].startswith('RuntimeError')


def test_time_stop_from_signal_hold_days():
    strat = _mk(lambda p, x: None)
    cur = _FakeCursor([_row(signal_params={'hold_days': 4})])      # bars_held == 5 >= 4
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert closed == ['sig-1'] and _closes(cur) == [('max_hold', 'closed')]
    assert engine.LAST_EXIT_HOOK_STATS['max_hold'] == 1
    # one bar earlier (bars_held == 4) also fires (>=); two earlier (3) holds
    cur2 = _FakeCursor([_row(signal_params={'hold_days': 4})])
    with patch.dict(os.environ, ENV):
        engine.update_pnl(cur2, _panel(), date(2026, 5, 11), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert _closes(cur2) == [(None, 'open')]


def test_time_stop_capped_by_configured_max_hold():
    strat = _mk(lambda p, x: None)
    cur = _FakeCursor([_row(signal_params={'hold_days': 40})])
    with (patch.dict(os.environ, ENV),
          patch('execution.regime_param_resolver.configured_max_hold_days', return_value=5)):
        engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[strat], regime={'state': 'LOW_VOL'})
    assert _closes(cur) == [('max_hold', 'closed')]


def test_non_hook_strategy_and_null_signal_params_untouched():
    plain = _mk(lambda p, x: 'x', exit_hook=False, sid='S_plain')
    cur = _FakeCursor([_row(strategy_id='S_plain', signal_params=None)])
    with patch.dict(os.environ, ENV):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[plain], regime={'state': 'LOW_VOL'})
    assert closed == [] and _closes(cur) == [(None, 'open')]


def test_demoted_strategy_loaded_on_demand():
    hook = _mk(lambda p, x: 'z_revert', sid='S_gone')
    cur = _FakeCursor([_row(strategy_id='S_gone')])
    with (patch.dict(os.environ, ENV),
          patch('strategies.registry.load_strategy_class', return_value=type(hook)) as ld):
        n, closed = engine.update_pnl(cur, _panel(), date(2026, 5, 13), strategies=[], regime={'state': 'LOW_VOL'})
    assert closed == ['sig-1'] and ld.call_args.args[0] == 'S_gone'
    assert engine.LAST_EXIT_HOOK_STATS['loaded_on_demand'] == 1


def test_bars_held_helper():
    idx = pd.bdate_range('2026-05-04', periods=10)
    p = pd.DataFrame({'AAPL': range(10)}, index=idx)
    assert engine._bars_held(p, date(2026, 5, 6), date(2026, 5, 13)) == 5
    assert engine._bars_held(p, date(2026, 5, 13), date(2026, 5, 13)) == 0
    assert engine._bars_held(pd.DataFrame({'AAPL': [1.0]}), date(2026, 5, 6), date(2026, 5, 13)) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_update_pnl_exit_hook.py -q -p no:cacheprovider`
Expected: `TypeError: update_pnl() got an unexpected keyword argument 'strategies'` for most; `AttributeError` for `LAST_EXIT_HOOK_STATS` / `_bars_held`.

- [ ] **Step 3: Implement**

(a) Module level in `src/execution/engine.py` (near the other constants, e.g. after `DAYS_HELD_REPORT`):

```python
# Exit-hook live mirror (docs/specs/2026-08-28-per-bar-exit-hook-phase2-spec.md).
# Counters for the last update_pnl call; main() logs them and records hook
# errors in execution_runs.errors. Reset at the start of every call.
LAST_EXIT_HOOK_STATS: dict = {}


def _exit_hook_enabled() -> bool:
    return os.environ.get('OPENCLAW_EXIT_HOOK_LIVE', '0') == '1'


def _bars_held(prices: pd.DataFrame, entry_dt, run_date):
    """Trading bars strictly after entry_dt up to and including run_date on
    the prices index (parity with the backtest's holding_days). None when the
    index is not a DatetimeIndex (legacy callers / tests with a RangeIndex)."""
    if not isinstance(prices.index, pd.DatetimeIndex) or entry_dt is None:
        return None
    lo, hi = pd.Timestamp(entry_dt), pd.Timestamp(run_date)
    return int(((prices.index > lo) & (prices.index <= hi)).sum())


def _hold_cap(signal_params, configured: int) -> int:
    """Per-signal hold_days capped by the configured max (mirrors
    backtest.open_book.resolve_hold_cap; not imported to keep the engine free
    of backtest modules)."""
    try:
        hd = int(float((signal_params or {}).get('hold_days')))
    except (TypeError, ValueError):
        return int(configured)
    return min(hd, int(configured)) if hd >= 1 else int(configured)
```

(b) Signature + stats reset + SELECT: change the `def update_pnl(cur, prices: pd.DataFrame, run_date: date)` line to

```python
def update_pnl(cur, prices: pd.DataFrame, run_date: date, *,
               strategies=None, regime=None, aux_data=None) -> tuple[int, list]:
```

and add `signal_params` to the SELECT column list (after `signal_date`). Immediately after the SELECT/fetchall, add:

```python
    _hook_on = _exit_hook_enabled()
    LAST_EXIT_HOOK_STATS.clear()
    LAST_EXIT_HOOK_STATS.update({'enabled': _hook_on, 'strategy_exit': 0, 'max_hold': 0,
                                 'hook_raised': 0, 'first_hook_raise': None,
                                 'loaded_on_demand': 0, 'hook_load_failed': 0,
                                 'rows_evaluated': 0})
    _by_id = {getattr(s, 'id', None): s for s in (strategies or [])}
    _loaded: dict = {}
    if _hook_on:
        logger.info('[exit_hook] enabled (OPENCLAW_EXIT_HOOK_LIVE=1); %d strategy instances', len(_by_id))

    def _strategy_for(sid):
        s = _by_id.get(sid)
        if s is not None:
            return s
        if sid in _loaded:
            return _loaded[sid]
        try:
            from strategies.registry import load_strategy_class
            cls = load_strategy_class(sid)
            inst = cls() if cls is not None else None
            if inst is not None:
                LAST_EXIT_HOOK_STATS['loaded_on_demand'] += 1
            else:
                LAST_EXIT_HOOK_STATS['hook_load_failed'] += 1
        except Exception as _e:
            logger.warning('[exit_hook] could not load %s for hook evaluation: %s', sid, _e)
            LAST_EXIT_HOOK_STATS['hook_load_failed'] += 1
            inst = None
        _loaded[sid] = inst
        return inst
```

(c) Per-row block — insert immediately after the existing target-inference chain (the last `elif direction == 'SHORT' and current <= target_1 * (1 + TARGET1_TRIGGER_PCT):` branch) and before `try:`:

```python
        # Exit-hook live mirror (Phase 2 §2.4): hook, then time stop, only
        # while nothing above closed the row and only for exit_hook strategies.
        if _hook_on and close_reason is None:
            _strat = _strategy_for(strat_id)
            if _strat is not None and getattr(_strat, 'exit_hook', False):
                LAST_EXIT_HOOK_STATS['rows_evaluated'] += 1
                _entry_dt = _tgt_dt if (_tgt_dt is not None and isinstance(_tgt_dt, date)) else sig_date
                _bars = _bars_held(prices, _entry_dt, run_date)
                if _bars is None:
                    _bars = days_held
                _sp = row.get('signal_params') if isinstance(row.get('signal_params'), dict) else {}
                position = {
                    'ticker': ticker, 'direction': direction, 'entry_price': entry,
                    'entry_date': _entry_dt, 'days_held': _bars,
                    'stop_loss': stop_loss, 'target_1': target_1, 'signal_params': _sp,
                }
                try:
                    _reason = _strat.should_exit(position, prices, regime, aux_data)
                except Exception as _e:
                    _reason = None
                    LAST_EXIT_HOOK_STATS['hook_raised'] += 1
                    if LAST_EXIT_HOOK_STATS['first_hook_raise'] is None:
                        LAST_EXIT_HOOK_STATS['first_hook_raise'] = f'{type(_e).__name__}: {_e}'
                        logger.error('[exit_hook] should_exit raised for %s %s: %s — holding',
                                     strat_id, ticker, LAST_EXIT_HOOK_STATS['first_hook_raise'])
                if _reason:
                    close_reason = f'strategy_exit:{_reason}'
                    close_status = 'closed'
                    realized_pct = unrealized_pct
                    LAST_EXIT_HOOK_STATS['strategy_exit'] += 1
                    logger.info('[exit_hook] %s %s %s bars_held=%d', strat_id, ticker, close_reason, _bars)
                elif close_reason is None:
                    from execution import regime_param_resolver as _rpr
                    _cap = _hold_cap(_sp, _rpr.configured_max_hold_days(strat_id, log=logger.warning))
                    if _bars >= _cap:
                        close_reason = 'max_hold'
                        close_status = 'closed'
                        realized_pct = unrealized_pct
                        LAST_EXIT_HOOK_STATS['max_hold'] += 1
                        logger.info('[exit_hook] %s %s max_hold bars_held=%d cap=%d', strat_id, ticker, _bars, _cap)
```

`_tgt_dt`, `sig_date`, `entry`, `days_held`, `unrealized_pct` are the names the existing loop already binds above this point — reuse them, do not recompute.

- [ ] **Step 4: Run new + guard tests**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_update_pnl_exit_hook.py tests/execution/test_engine_oue_ordering.py tests/execution/test_engine_run_stats.py -q -p no:cacheprovider`
Expected: all pass. If `test_stop_inference_beats_hook` fails, the block was inserted above the stop/target chain — move it below.

- [ ] **Step 5: Commit**

```bash
git add src/execution/engine.py tests/execution/test_update_pnl_exit_hook.py
git commit -m "engine: exit-hook live mirror in update_pnl (hook + time stop) behind OPENCLAW_EXIT_HOOK_LIVE

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 3: `main()` wiring, run summary, `execution_runs.errors`, hook docstring contract

**Files:**
- Modify: `src/execution/engine.py` — `main()` call site (:2395 `update_pnl(cur, prices, run_date)`), the log line after it, the `errors` list (:2185); new helper `_exit_hook_run_summary`
- Modify: `src/strategies/base.py` — `should_exit` docstring
- Test: `tests/execution/test_update_pnl_exit_hook.py` (append)

**Interfaces:**
- Produces: `engine._exit_hook_run_summary(stats: dict) -> tuple[str | None, str | None]` — `(log_line, error_entry)`; both `None` when `stats.get('enabled')` is false; `error_entry` only when `hook_raised > 0`.

- [ ] **Step 1: Write the failing tests**

```python
def test_exit_hook_run_summary():
    assert engine._exit_hook_run_summary({'enabled': False}) == (None, None)
    line, err = engine._exit_hook_run_summary({'enabled': True, 'strategy_exit': 2, 'max_hold': 1,
                                                'hook_raised': 0, 'first_hook_raise': None,
                                                'loaded_on_demand': 1, 'rows_evaluated': 7})
    assert line == '[exit_hook] closes: 2 strategy_exit, 1 max_hold; hook errors 0; rows 7; instances loaded on demand 1'
    assert err is None
    line, err = engine._exit_hook_run_summary({'enabled': True, 'strategy_exit': 0, 'max_hold': 0,
                                                'hook_raised': 3, 'first_hook_raise': 'ValueError: x',
                                                'loaded_on_demand': 0, 'rows_evaluated': 3})
    assert err == 'exit_hook: 3 hook errors (first: ValueError: x)'


def test_main_passes_context_to_update_pnl():
    """main() must hand strategies/regime/aux_data to update_pnl (source-level
    contract check — main() itself needs a live DB)."""
    import inspect
    src = inspect.getsource(engine.main)
    assert 'update_pnl(cur, prices, run_date,' in src
    assert 'strategies=strategies' in src and 'regime=regime' in src and 'aux_data=aux_data' in src
    assert '_exit_hook_run_summary(' in src
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_update_pnl_exit_hook.py -q -p no:cacheprovider -k "run_summary or passes_context"`
Expected: `AttributeError: ... _exit_hook_run_summary`; the source assertion fails.

- [ ] **Step 3: Implement**

Helper (next to `_exit_hook_enabled`):

```python
def _exit_hook_run_summary(stats: dict):
    """(log_line, execution_runs.errors entry) for the last update_pnl call."""
    if not stats or not stats.get('enabled'):
        return None, None
    line = (f"[exit_hook] closes: {stats.get('strategy_exit', 0)} strategy_exit, "
            f"{stats.get('max_hold', 0)} max_hold; hook errors {stats.get('hook_raised', 0)}; "
            f"rows {stats.get('rows_evaluated', 0)}; instances loaded on demand {stats.get('loaded_on_demand', 0)}")
    err = None
    if stats.get('hook_raised', 0) > 0:
        err = f"exit_hook: {stats['hook_raised']} hook errors (first: {stats.get('first_hook_raise')})"
    return line, err
```

In `main()` replace

```python
        pnl_updates, newly_closed_ids = update_pnl(cur, prices, run_date)
        logger.info(f"P&L rows updated: {pnl_updates}")
```

with

```python
        pnl_updates, newly_closed_ids = update_pnl(cur, prices, run_date,
                                                   strategies=strategies, regime=regime,
                                                   aux_data=aux_data)
        logger.info(f"P&L rows updated: {pnl_updates}")
        _hook_line, _hook_err = _exit_hook_run_summary(LAST_EXIT_HOOK_STATS)
        if _hook_line:
            logger.info(_hook_line)
        if _hook_err:
            errors.append(_hook_err)
```

In `src/strategies/base.py`, append to the `should_exit` docstring:

```
        Regime contract: rely on regime['state'] ONLY. The backtest passes
        {'state','date','one_hot','transition_probs'}; live passes
        {'state','vix_level','vix_percentile','regime_data','updated_at'}.
```

- [ ] **Step 4: Run tests**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_update_pnl_exit_hook.py tests/execution/test_dry_run_dataflow.py tests/strategies/test_exit_hook_interface.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/execution/engine.py src/strategies/base.py tests/execution/test_update_pnl_exit_hook.py
git commit -m "engine: main() passes strategies/regime/aux to update_pnl; exit-hook run summary + execution_runs.errors entry

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 4: Backtest residuals — full regime payload to the hook, SHORT end-to-end

**Files:**
- Modify: `src/backtest/unified_backtest.py` — day loop (:809–850) and the drain (:1001–1010)
- Test: `tests/backtest/test_open_book.py` (append)

**Interfaces:**
- Produces: the open-book advance and the drain pass a regime payload with keys `state, date, one_hot, transition_probs` (identical construction to the one `generate_signals` receives; `state=None` and zero `one_hot` when the day has no regime).

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_open_book.py`:

```python
def _regime_payload_for(state, cur_d):
    return {'state': (str(state) if state is not None else None), 'date': cur_d.isoformat(),
            'one_hot': {r: (1.0 if r == state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                                 for r1 in CANONICAL_REGIMES}}


class TestPhase2Residuals:
    def test_hook_receives_full_regime_payload(self):
        close_wide, bars, regimes, dates, closes = _dataset(n=30)
        seen = []
        cls = _mk_hook_cls(lambda p, x: None, hold_days=3)
        orig = cls.should_exit
        def spy(self, position, prices, regime, aux_data=None):
            seen.append(regime); return orig(self, position, prices, regime, aux_data)
        cls.should_exit = spy
        inst = cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
        with patch.dict(os.environ, {'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0',
                                     'OPENCLAW_BACKTEST_COUPLED_RECS': '0'}):
            ub._per_bar_simulate(inst, close_wide, bars, regimes, dates[0], dates[-1],
                                 strategy_id='stub_hook', max_hold_days=21, fill_model='same_close')
        assert seen, 'hook never consulted'
        assert seen[0] == _regime_payload_for('LOW_VOL', dates[10].date())
        assert all(set(r) == {'state', 'date', 'one_hot', 'transition_probs'} for r in seen)

    def test_short_trade_end_to_end_matches_simulate_trade(self):
        close_wide, bars, regimes, dates, closes = _dataset(n=30)
        # a falling tape so a SHORT is the natural trade: reverse the closes
        rev = list(reversed(closes))
        close_wide = pd.DataFrame({'AAA': rev}, index=dates); close_wide.index.name = 'date'
        bars = _bars_from_rows({'AAA': [(c - 0.1, c + 0.2, c - 0.2, c) for c in rev]}, dates)

        def mk(hook):
            class S(BaseStrategy):
                id = 'stub_short'; min_lookback = 5; active_in_regimes = list(CANONICAL_REGIMES); fired = False
                exit_hook = hook
                def generate_signals(self, prices, regime, universe, aux_data=None):
                    if len(prices) < 10 or S.fired or not universe: return []
                    S.fired = True; t = universe[0]; ep = float(prices[t].iloc[-1])
                    return [Signal(ticker=t, direction='SHORT', entry_price=ep, stop_loss=ep * 1.07,
                                   target_1=ep * 0.92, target_2=0.0, target_3=0.0,
                                   position_size_pct=0.0, confidence='MED')]
                if hook:
                    def should_exit(self, position, prices, regime, aux_data=None): return None
            return S
        plain = _run_capture(mk(False), close_wide, bars, regimes, fill_model='same_close')
        hooked = _run_capture(mk(True), close_wide, bars, regimes, fill_model='same_close')
        assert plain and plain[0]['direction'] == 'short'
        strip = lambda ts: [{k: v for k, v in t.items() if k != 'daily_marks'} for t in ts]
        assert strip(hooked) == strip(plain)
        assert [round(m[1], 12) for m in hooked[0]['daily_marks']] == [round(m[1], 12) for m in plain[0]['daily_marks']]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py -q -p no:cacheprovider -k Phase2Residuals`
Expected: the payload test fails (`{'state','date'}` only); the SHORT test may already pass (it is the SHORT identity gate the final review asked for — keep it either way).

- [ ] **Step 3: Implement**

In `_per_bar_simulate` add a small local helper before the day loop:

```python
    def _regime_payload_full(state, cur_d):
        return {
            'state':            (str(state) if state is not None and not pd.isna(state) else None),
            'date':             cur_d.isoformat(),
            'one_hot':          {r: (1.0 if r == state else 0.0) for r in CANONICAL_REGIMES},
            'transition_probs': {r1: {r2: (1.0 if r1 == r2 else 0.0) for r2 in CANONICAL_REGIMES}
                                 for r1 in CANONICAL_REGIMES},
        }
```

In the in-loop advance replace the two-line `_rp = {...}` with `_rp = _regime_payload_full(_rs, current_date.date() if hasattr(current_date, 'date') else current_date)`; in the drain replace its `_rp = {...}` with `_rp = _regime_payload_full(_rs, _dt.date())`. Leave the existing `regime_payload = {...}` that feeds `generate_signals` textually untouched (non-hook path byte-identical).

- [ ] **Step 4: Run tests + goldens**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/backtest/test_open_book.py tests/backtest/test_backtest_fill_model.py -q -p no:cacheprovider`
Expected: all pass, including `test_plain_strategy_matches_pre_phase1_golden`.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/backtest/test_open_book.py
git commit -m "backtest: open-book hook receives the full regime payload; SHORT end-to-end identity test

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 5: Parity test — backtest open-book path ≡ live `update_pnl` branch

**Files:**
- Test: `tests/execution/test_exit_hook_live_parity.py` (create)

**Interfaces:**
- Consumes: Task 2 `update_pnl(..., strategies=, regime=)`, `engine.LAST_EXIT_HOOK_STATS`; Phase 1 `_per_bar_simulate`; `_FakeCursor` pattern (copy it — do not import from another test file).

- [ ] **Step 1: Write the test (it must FAIL until Tasks 2 and 4 are in; run it after them)**

```python
# tests/execution/test_exit_hook_live_parity.py
"""Phase 2 §4.1: the live update_pnl hook branch reproduces the backtest
open-book exits (hook exit AND time stop) for LONG and SHORT fixtures.
Backtest side is authoritative (operator ruling 2026-08-07)."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import engine                      # noqa: E402
import backtest.unified_backtest as ub            # noqa: E402
from strategies.base import BaseStrategy, Signal, CANONICAL_REGIMES  # noqa: E402

ENV = {'OPENCLAW_EXIT_HOOK_LIVE': '1', 'OPENCLAW_BACKTEST_COUPLED_RECS': '0',
       'OPENCLAW_BT_ASSET_GATE': 'off', 'OPENCLAW_BT_SPREAD_COSTS': '0', 'OPENCLAW_BACKTEST_SLIPPAGE': '0'}
DATES = pd.bdate_range('2026-03-02', periods=40)


def _panel(closes):
    p = pd.DataFrame({'AAA': closes}, index=DATES); p.index.name = 'date'; return p


def _bars(closes):
    return {'AAA': pd.DataFrame({'open': closes, 'high': [c + 0.05 for c in closes],
                                 'low': [c - 0.05 for c in closes], 'close': closes},
                                index=pd.DatetimeIndex(DATES, name='date'))}


def _fixture(direction, exit_level, hold_days):
    """Enters on the first bar with >= 10 prices; exits when the close crosses
    exit_level (long: >=, short: <=) or after hold_days bars."""
    class Fx(BaseStrategy):
        id = 'stub_parity'; min_lookback = 5; active_in_regimes = list(CANONICAL_REGIMES)
        exit_hook = True; fired = False
        def generate_signals(self, prices, regime, universe, aux_data=None):
            if len(prices) < 10 or Fx.fired or not universe: return []
            Fx.fired = True; ep = float(prices['AAA'].iloc[-1])
            sl, t1 = (ep * 0.5, ep * 3.0) if direction == 'LONG' else (ep * 3.0, ep * 0.5)  # brackets never hit
            return [Signal(ticker='AAA', direction=direction, entry_price=ep, stop_loss=sl, target_1=t1,
                           target_2=0.0, target_3=0.0, position_size_pct=0.0, confidence='MED',
                           signal_params={'hold_days': hold_days})]
        def should_exit(self, position, prices, regime, aux_data=None):
            c = float(prices['AAA'].iloc[-1])
            hit = c >= exit_level if position['direction'] == 'LONG' else c <= exit_level
            return 'level' if hit else None
    return Fx


class _FakeCursor:
    def __init__(self, rows): self.rows = rows; self._fetch = []; self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params)); self._fetch = list(self.rows) if "status = 'open'" in sql else []
    def fetchall(self): return self._fetch


def _backtest_exits(fx_cls, closes):
    inst = fx_cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
    regimes = pd.Series({d: 'LOW_VOL' for d in DATES})
    with patch.dict(os.environ, ENV):
        out = ub._per_bar_simulate(inst, _panel(closes), _bars(closes), regimes, DATES[0], DATES[-1],
                                   strategy_id='stub_parity', max_hold_days=21, fill_model='same_close')
    t = out['trades'][0]
    return {(t['ticker'], t['exit_date'], t['exit_reason'], t['holding_days'])}, t


def _live_exits(fx_cls, closes, entry_trade):
    """Replay the live branch day by day after the backtest's fill date."""
    fx_cls.fired = True                       # live harness never re-enters
    inst = fx_cls(); panel = _panel(closes)
    entry_date = entry_trade['entry_date']
    row = {'id': 'sig-1', 'strategy_id': 'stub_parity', 'ticker': 'AAA', 'direction': entry_trade['direction'].upper(),
           'entry_price': entry_trade['entry_price'], 'mark_entry_price': entry_trade['entry_price'],
           'target_date': entry_date, 'lifecycle_state': 'FILLED', 'stop_loss': entry_trade['signal_stop'],
           'target_1': entry_trade['signal_target'], 'signal_date': entry_date,
           'signal_params': {'hold_days': 6}}
    for d in DATES[DATES > pd.Timestamp(entry_date)]:
        cur = _FakeCursor([row])
        with patch.dict(os.environ, ENV):
            n, closed = engine.update_pnl(cur, panel.loc[:d], d.date(), strategies=[inst], regime={'state': 'LOW_VOL'})
        if closed:
            reason = next(p[10] for s, p in cur.executed if 'INSERT INTO signal_pnl' in s)
            bars = engine._bars_held(panel.loc[:d], entry_date, d.date())
            return {('AAA', d.date(), reason, bars)}
    return set()


def _check(direction, closes, exit_level, expect_reason):
    fx = _fixture(direction, exit_level, hold_days=6)
    bt, trade = _backtest_exits(fx, closes)
    live = _live_exits(fx, closes, trade)
    assert live == bt, f'{direction}: live {live} != backtest {bt}'
    assert next(iter(bt))[2] == expect_reason


def test_long_hook_exit_parity():
    closes = [100.0 + i for i in range(40)]                    # rising: LONG hits level 113 on bar 13
    _check('LONG', closes, exit_level=113.0, expect_reason='strategy_exit:level')


def test_short_hook_exit_parity():
    closes = [140.0 - i for i in range(40)]                    # falling: SHORT hits level 127 on bar 13
    _check('SHORT', closes, exit_level=127.0, expect_reason='strategy_exit:level')


def test_time_stop_parity_when_level_never_hit():
    closes = [100.0 + 0.1 * i for i in range(40)]              # never reaches 200 → hold_days=6 time stop
    _check('LONG', closes, exit_level=200.0, expect_reason='max_hold')


def test_flag_off_live_records_no_close():
    fx = _fixture('LONG', 113.0, 6); bt, trade = _backtest_exits(fx, [100.0 + i for i in range(40)])
    with patch.dict(os.environ, {'OPENCLAW_EXIT_HOOK_LIVE': '0'}):
        assert _live_exits(fx, [100.0 + i for i in range(40)], trade) == set()
```

- [ ] **Step 2: Run**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/execution/test_exit_hook_live_parity.py -q -p no:cacheprovider`
Expected: 4 passed. If a `holding_days`/`bars` mismatch appears, the discrepancy is on the LIVE side by ruling — fix `_bars_held`/entry-date precedence in `engine.py`, not the backtest. (Note the backtest fills at the signal bar's close and steps from the next bar; the live harness starts the day after `entry_date`, so both count bars strictly after entry.)

- [ ] **Step 3: Commit**

```bash
git add tests/execution/test_exit_hook_live_parity.py
git commit -m "tests: exit-hook live ≡ backtest parity (LONG/SHORT hook exits, time stop, flag off)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 6: Health-digest line

**Files:**
- Modify: `src/engine/daily-health-digest.js` — `buildDigest` (:47–123): add a query to the `Promise.all`, build `hookLine`, add it to the joined list; export a pure `exitHookLine(rows)`
- Test: `tests/engine/test_daily_health_digest_exit_hook.test.js` (create)

**Interfaces:**
- Produces: `exitHookLine(rows: Array<{close_reason: string, n: string|number}>) -> string | null` — `null` when no rows/all zero; otherwise `🪝 Exit hook: <N> strategy exits (<reason>=<n>, …), <M> max_hold` where reasons drop the `strategy_exit:` prefix, sorted by count desc.

- [ ] **Step 1: Write the failing test**

```js
// tests/engine/test_daily_health_digest_exit_hook.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert');
// The digest module requires the DB client at load; stub it before requiring.
require.cache[require.resolve('../../src/database/postgres')] = { exports: { query: async () => ({ rows: [] }) } };
const { exitHookLine } = require('../../src/engine/daily-health-digest');

test('no rows → null', () => { assert.strictEqual(exitHookLine([]), null); assert.strictEqual(exitHookLine(null), null); });

test('formats strategy exits by reason and max_hold', () => {
  const rows = [{ close_reason: 'strategy_exit:pair_decohered', n: '7' },
                { close_reason: 'strategy_exit:z_revert', n: 9 },
                { close_reason: 'max_hold', n: '2' }];
  assert.strictEqual(exitHookLine(rows), '🪝 Exit hook: 16 strategy exits (z_revert=9, pair_decohered=7), 2 max_hold');
});

test('only max_hold', () => {
  assert.strictEqual(exitHookLine([{ close_reason: 'max_hold', n: 3 }]), '🪝 Exit hook: 0 strategy exits, 3 max_hold');
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && node --test tests/engine/test_daily_health_digest_exit_hook.test.js`
Expected: `TypeError: exitHookLine is not a function`.

- [ ] **Step 3: Implement**

In `src/engine/daily-health-digest.js`, above `buildDigest`:

```js
// Exit-hook live mirror (Phase 2 §2.8): today's hook/time-stop closes from
// signal_pnl. Pure; null when there is nothing to say.
function exitHookLine(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return null;
  let maxHold = 0; const reasons = [];
  for (const r of rows) {
    const n = parseInt(r.n, 10) || 0;
    if (r.close_reason === 'max_hold') maxHold += n;
    else if (String(r.close_reason).startsWith('strategy_exit:')) reasons.push([String(r.close_reason).slice('strategy_exit:'.length), n]);
  }
  const total = reasons.reduce((a, [, n]) => a + n, 0);
  if (total === 0 && maxHold === 0) return null;
  reasons.sort((a, b) => b[1] - a[1]);
  const detail = reasons.length ? ` (${reasons.map(([k, n]) => `${k}=${n}`).join(', ')})` : '';
  return `🪝 Exit hook: ${total} strategy exits${detail}, ${maxHold} max_hold`;
}
```

In `buildDigest`: add a 7th element to the `Promise.all` array —

```js
    dbQuery(`SELECT close_reason, COUNT(*) AS n FROM signal_pnl
              WHERE status='closed' AND closed_at::date = CURRENT_DATE
                AND (close_reason LIKE 'strategy_exit:%' OR close_reason = 'max_hold')
              GROUP BY close_reason`).catch(() => ({ rows: [] })),
```

destructure it as `hookRows`, compute `const hookLine = exitHookLine(hookRows.rows);`, and insert `hookLine` into the returned list right after `pnlLine`. Add `exitHookLine` to `module.exports` (keep `buildDigest`, `register`).

- [ ] **Step 4: Run**

Run: `cd /root/openclaw && node --test tests/engine/test_daily_health_digest_exit_hook.test.js && node --check src/pipeline/daily_health_digest.js`
Expected: 3 pass; syntax OK.

- [ ] **Step 5: Commit**

```bash
git add src/engine/daily-health-digest.js tests/engine/test_daily_health_digest_exit_hook.test.js
git commit -m "digest: exit-hook closes line (strategy_exit by reason, max_hold) from signal_pnl

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 7: Live-code replay on real data (`scripts/exit_hook_live_replay.py`)

**Files:**
- Create: `scripts/exit_hook_live_replay.py`
- Test: `tests/scripts/test_exit_hook_live_replay.py` (create; the pure parts only)

**Interfaces:**
- Produces: CLI `python3 scripts/exit_hook_live_replay.py --strategy S_coint_pairs_sector_v2 --run-id <uuid> --dates 2026-06-02,2026-06-03,... [--universe-cap tier_liquid]` printing, per date, `open=<n> live_closes=<k> backtest_closes=<m> agree=<a> disagree=<d>` and a final `AGREEMENT k/m` line; exit 0 always (it is a report). Pure helpers: `open_trades_on(trades: list[dict], d: date) -> list[dict]` (entry_date < d ≤ exit_date), `rows_from_trades(open_trades, signals_by_entry: dict[(date, ticker), Signal]) -> list[dict]` (execution_signals-shaped rows with `signal_params` from the recovered `Signal`), `compare(live_closes: dict[str, str], bt_closes: dict[str, str]) -> tuple[int, int]` (agree, disagree by ticker → reason).

- [ ] **Step 1: Write the failing tests (pure helpers)**

```python
# tests/scripts/test_exit_hook_live_replay.py
from __future__ import annotations
import importlib.util, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('replay', ROOT / 'scripts' / 'exit_hook_live_replay.py')
replay = importlib.util.module_from_spec(spec); spec.loader.exec_module(replay)
from strategies.base import Signal


def test_open_trades_on():
    t = [{'ticker': 'A', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5)},
         {'ticker': 'B', 'entry_date': date(2026, 6, 3), 'exit_date': date(2026, 6, 4)}]
    assert [x['ticker'] for x in replay.open_trades_on(t, date(2026, 6, 2))] == ['A']
    assert [x['ticker'] for x in replay.open_trades_on(t, date(2026, 6, 4))] == ['A', 'B']
    assert replay.open_trades_on(t, date(2026, 6, 6)) == []


def test_rows_from_trades_uses_recovered_signal_params():
    t = [{'ticker': 'A', 'direction': 'long', 'entry_date': date(2026, 6, 1), 'exit_date': date(2026, 6, 5),
          'entry_price': 10.0, 'signal_stop': 9.0, 'signal_target': 12.0}]
    sig = Signal(ticker='A', direction='LONG', entry_price=10.0, stop_loss=9.0, target_1=12.0, target_2=0.0,
                 target_3=0.0, position_size_pct=0.0, confidence='MED', signal_params={'pair': 'A/B', 'z': 2.2})
    rows = replay.rows_from_trades(t, {(date(2026, 6, 1), 'A'): sig})
    assert rows[0]['signal_params'] == {'pair': 'A/B', 'z': 2.2} and rows[0]['direction'] == 'LONG'
    assert rows[0]['target_date'] == date(2026, 6, 1) and rows[0]['mark_entry_price'] == 10.0
    assert replay.rows_from_trades(t, {}) == []          # unrecoverable → skipped, not fabricated


def test_compare():
    assert replay.compare({'A': 'strategy_exit:z_revert', 'B': 'max_hold'},
                          {'A': 'strategy_exit:z_revert', 'B': 'strategy_exit:pair_decohered', 'C': 'max_hold'}) == (1, 2)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/scripts/test_exit_hook_live_replay.py -q -p no:cacheprovider`
Expected: `FileNotFoundError` / attribute errors (script missing).

- [ ] **Step 3: Implement `scripts/exit_hook_live_replay.py`**

```python
#!/usr/bin/env python3
"""exit_hook_live_replay.py — exercise engine.update_pnl's exit-hook branch on
REAL data with zero side effects (Phase 2 spec §4.2).

For each --dates entry d: take the backtest run's trades open on d, recover
their entry-time signal_params by re-running the strategy's generate_signals
on the live prices panel truncated to each trade's entry date (backtests are
deterministic — 2026-08-07 ruling), build execution_signals-shaped rows, call
update_pnl with a fake cursor + the real panel truncated to d, and compare the
closes the live branch would issue against the backtest's recorded exits.
Read-only: no DB writes, no broker calls. Run outside 13:00–20:15 UTC.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))


def open_trades_on(trades, d):
    return [t for t in trades if t['entry_date'] < d <= t['exit_date']]


def rows_from_trades(open_trades, signals_by_entry):
    rows = []
    for i, t in enumerate(open_trades):
        sig = signals_by_entry.get((t['entry_date'], t['ticker']))
        if sig is None:
            continue
        rows.append({'id': f'replay-{i}', 'strategy_id': None, 'ticker': t['ticker'],
                     'direction': str(t['direction']).upper(), 'entry_price': float(t['entry_price']),
                     'mark_entry_price': float(t['entry_price']), 'target_date': t['entry_date'],
                     'lifecycle_state': 'FILLED', 'stop_loss': float(t['signal_stop']),
                     'target_1': float(t['signal_target']), 'signal_date': t['entry_date'],
                     'signal_params': dict(sig.signal_params or {})})
    return rows


def compare(live_closes, bt_closes):
    agree = sum(1 for k, v in bt_closes.items() if live_closes.get(k) == v)
    disagree = len(set(bt_closes) | set(live_closes)) - agree
    return agree, disagree


class _FakeCursor:
    def __init__(self, rows): self.rows = rows; self._fetch = []; self.executed = []
    def execute(self, sql, params=None):
        self.executed.append((sql, params)); self._fetch = list(self.rows) if "status = 'open'" in sql else []
    def fetchall(self): return self._fetch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy', required=True); ap.add_argument('--run-id', required=True)
    ap.add_argument('--dates', required=True, help='comma-separated YYYY-MM-DD')
    args = ap.parse_args()
    from dotenv import load_dotenv; load_dotenv(str(ROOT / '.env'))
    os.environ['OPENCLAW_EXIT_HOOK_LIVE'] = '1'
    import psycopg2, psycopg2.extras, pandas as pd
    from execution import engine
    from strategies.registry import load_strategy_class
    from strategies.base import CANONICAL_REGIMES

    dates = [datetime.strptime(s.strip(), '%Y-%m-%d').date() for s in args.dates.split(',') if s.strip()]
    with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT ticker, direction, entry_date, exit_date, entry_price, exit_reason,
                              signal_stop, signal_target FROM strategy_backtest_trades WHERE run_id=%s""", (args.run_id,))
        trades = [dict(r) for r in cur.fetchall()]
    cls = load_strategy_class(args.strategy); inst = cls(); inst.active_in_regimes = list(CANONICAL_REGIMES)
    for r in trades: r['strategy_id'] = args.strategy
    panel = engine.load_prices(sorted({t['ticker'] for t in trades}))       # real panel, all needed tickers
    universe = list(panel.columns)
    sig_cache: dict = {}
    total_agree = total_bt = 0
    for d in dates:
        opens = open_trades_on(trades, d)
        for t in opens:
            key = (t['entry_date'], t['ticker'])
            if key in sig_cache: continue
            sub = panel.loc[:pd.Timestamp(t['entry_date'])]
            try:
                sigs = inst.generate_signals(sub, {'state': 'LOW_VOL'}, universe)
            except Exception as e:
                print(f'[replay] generate_signals failed on {t["entry_date"]}: {e}', file=sys.stderr); sigs = []
            for s in sigs: sig_cache[(t['entry_date'], s.ticker)] = s
            sig_cache.setdefault(key, None)
        rows = rows_from_trades(opens, sig_cache)
        for r in rows: r['strategy_id'] = args.strategy
        cur = _FakeCursor(rows)
        engine.update_pnl(cur, panel.loc[:pd.Timestamp(d)], d, strategies=[inst], regime={'state': 'LOW_VOL'})
        live = {}
        for sql, p in cur.executed:
            if 'INSERT INTO signal_pnl' in sql and p[7] == 'closed':
                live[[r for r in rows if r['id'] == p[0]][0]['ticker']] = p[10]
        bt = {t['ticker']: t['exit_reason'] for t in opens if t['exit_date'] == d
              and str(t['exit_reason']).startswith(('strategy_exit:', 'max_hold'))}
        agree, disagree = compare(live, bt)
        total_agree += agree; total_bt += len(bt)
        print(f'{d} open={len(opens)} rows={len(rows)} live_closes={len(live)} backtest_closes={len(bt)} '
              f'agree={agree} disagree={disagree} stats={engine.LAST_EXIT_HOOK_STATS}')
    print(f'AGREEMENT {total_agree}/{total_bt}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

Caveats to print in the script's header comment and the report: the replay uses today's universe/prices panel (not the point-in-time panel the backtest used), a fixed `LOW_VOL` regime, and no `aux_data` — X1's hook reads none of these, so for X1 the comparison is exact; for other strategies the agreement number is indicative only.

- [ ] **Step 4: Run the unit tests, then the replay (outside 13:00–20:15 UTC, `nice -n 19`)**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/scripts/test_exit_hook_live_replay.py -q -p no:cacheprovider`
Then: `cd /root/openclaw && PYTHONPATH=src nice -n 19 python3 scripts/exit_hook_live_replay.py --strategy S_coint_pairs_sector_v2 --run-id 3a470001-0405-46e5-aaa3-b65775ec6640 --dates 2026-06-02,2026-06-09,2026-06-16,2026-06-23,2026-07-07,2026-07-14,2026-07-21,2026-08-04,2026-08-11,2026-08-18 2>&1 | tail -15`
Expected: unit tests pass; the replay prints per-date lines and `AGREEMENT k/m`. Record the output in the task report. Disagreements caused by universe/panel drift (a ticker missing from today's panel) are expected and must be listed; disagreements on tickers present in both are live-side defects to fix in Task 2's code (backtest authoritative).

- [ ] **Step 5: Commit**

```bash
git add scripts/exit_hook_live_replay.py tests/scripts/test_exit_hook_live_replay.py
git commit -m "scripts: exit_hook_live_replay — exercise the live hook branch on real data, read-only

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

### Task 8: Docs, verification record, flip runbook

**Files:**
- Modify: `docs/specs/2026-08-28-per-bar-exit-hook-phase2-spec.md` (Status line → LANDED; §4 gets the replay result), `docs/specs/2026-08-28-per-bar-exit-hook-spec.md` (§6 row 2 status), `docs/archive/changelog.md`, `docs/superpowers/plans/2026-08-24-five-repo-adoptions.md` (X1 section: Phase 2 landed, flag still 0)

- [ ] **Step 1: Record verification** — paste the parity test summary (4 passed) and the replay `AGREEMENT k/m` line with the per-date disagreement list into Phase 2 spec §4 under a `**Result (date)**` paragraph; set the Status line to `LANDED <commits>; OPENCLAW_EXIT_HOOK_LIVE still 0 — flip per §4 runbook`.

- [ ] **Step 2: Changelog** — under `## Recent Changes`:

```markdown
- **2026-08-28: per-bar exit hook — Phase 2 (live mirror) landed, flag OFF.** `engine.update_pnl` evaluates `should_exit` + the per-signal time stop for `exit_hook` strategies (hook-strategies only, D6) after stop/target inference, behind `OPENCLAW_EXIT_HOOK_LIVE` (default 0); `days_held` for the hook is a bar count (parity); closes reuse the existing signal_pnl path so the 15:55 sizer emits orphan_close. Shared `configured_max_hold_days`; digest line; parity test (LONG/SHORT/time stop) green; live replay on run `3a470001`: <AGREEMENT k/m>. Flip runbook in the Phase 2 spec §4; first hook strategy's activation carries the one-cycle watch.
```

- [ ] **Step 3: Flip runbook (docs only — the flip itself is the operator's action)** — ensure Phase 2 spec §4 "Flip runbook" reads exactly: set `OPENCLAW_EXIT_HOOK_LIVE=1` in `/root/openclaw/.env`; `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service` (API process re-reads `.env`; NEVER the system unit); verify JS: `cd /root/openclaw && node -e "require('dotenv').config(); console.log(process.env.OPENCLAW_EXIT_HOOK_LIVE)"` prints `1`; verify engine: next 15:00 ET cycle log shows `[exit_hook] enabled`; the first promoted `exit_hook` strategy gets the one-cycle watch (`signal_pnl` `strategy_exit:*` closes → sizer `orphan_close` → fills → next-morning `alpaca position list --symbols …` flat → digest line).

- [ ] **Step 4: Commit**

```bash
git add docs/specs/2026-08-28-per-bar-exit-hook-phase2-spec.md docs/specs/2026-08-28-per-bar-exit-hook-spec.md docs/archive/changelog.md docs/superpowers/plans/2026-08-24-five-repo-adoptions.md
git commit -m "docs: exit-hook Phase 2 landed (flag off) — verification record, flip runbook

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push origin main
```

---

## Self-review (done at authoring)

- **Spec coverage:** §2.1 → T2 (kwargs, flag); §2.2 → T2 (SELECT); §2.3 → T2 (`_strategy_for`); §2.4 → T2 (order, error handling, counters); §2.5 → T2 (`position`, `_bars_held`); §2.6 → T1 + T2 (`_hold_cap`); §2.7 → T3 (docstring) + T4 (backtest payload); §2.8 → T3 (log, errors) + T6 (digest); §2.9 → T8 (runbook; no `.env` edit in-plan); §3 residuals → T4 + T1; §4.1 → T5; §4.2 → T7; §4.3 → T8 (checklist text); §5 matrix → T1–T7 test files as named.
- **Type consistency:** `update_pnl(cur, prices, run_date, *, strategies=None, regime=None, aux_data=None)` identical in T2/T3/T5/T7; `LAST_EXIT_HOOK_STATS` keys identical in T2/T3; `_bars_held(prices, entry_dt, run_date)` in T2/T5; `configured_max_hold_days(strategy_id, *, default=21, log=None)` in T1/T2; `exitHookLine(rows)` in T6; replay helpers `open_trades_on/rows_from_trades/compare` in T7 test and script.
- **Placeholders:** the changelog's `<AGREEMENT k/m>` is an explicit instruction to paste Task 7's output.
- **Plan-level risk noted for the executor:** T2's `_FakeCursor` rows are plain dicts — `row.get(...)` works; the real `RealDictCursor` rows also support `.get`. The `signal_params` SELECT addition changes the tuple the OUE test's `_FakeCursor` ignores (it matches on SQL text), so that test stays green.
