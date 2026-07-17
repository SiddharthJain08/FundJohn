# Trading-Day Panel Harness Implementation Plan

> **⚠️ SUPERSEDED (2026-06-20):** Phase 1 (Tasks 1-3) is DONE and reused; Phases
> 2-3 (validate-then-flip the **backtest only**) are replaced by the system-wide
> plan, because the live engine uses the same union panel. See
> `docs/superpowers/specs/2026-06-20-system-wide-trading-day-panel-design.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give equity-class backtests a price panel built on the equity trading calendar (no crypto/forex weekend rows) so wide-frame `pct_change`/`rolling`, row-count windows, and period-start detection are correct — gated, validated, then flipped to default.

**Architecture:** The simulate loop is already trading-day-gated (regimes have no weekend rows), so the only defect is the *content* of `close_wide`: the union pivot interleaves 1059 weekend/holiday rows (all-equity-NaN, from 13 crypto/forex tickers). The entire fix filters `close_wide`'s rows to the equity calendar before the loop, for `instrument_class ∈ {equity, etp, option}`, behind a default-off gate. `bars_by_ticker` (fills) and crypto strategies are untouched.

**Tech Stack:** Python 3, pandas, psycopg2; pytest; existing `src/backtest/unified_backtest.py` harness.

**Spec:** `docs/superpowers/specs/2026-06-20-trading-day-panel-harness-design.md`

## Global Constraints

- **NEVER delete from the master database.** This change filters `close_wide` **in memory only** — no write to `prices.parquet` or any master table.
- **Gate-off byte-equivalence is mandatory.** With `OPENCLAW_BACKTEST_EQUITY_CALENDAR` unset/`0`, `run_backtest` must be identical to the current implementation.
- **Gate default in Phase 1 is OFF** (`os.environ.get('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '0')`). The flip is a deliberate Phase-3 step.
- **2-core VPS, 8GB no-swap:** any re-backtest of multiple strategies runs **one strategy per subprocess, sequentially**, `nice -n 19` (see `reference_vps_two_core_cpu`, `project_weekend_refresh_oom_recovery`). Never fan out CPU-heavy backtests concurrently.
- **`.env` cannot be `source`d** (unquoted parens). Load only the keys you need: `set -a; export $(grep -E '^(POSTGRES_URI)=' .env | xargs -d '\n'); set +a`. Backtests need `PYTHONPATH=src`.
- **Commit message footer:** end every commit with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Branch:** `feat/intraday-regime-15min-prefetch` (current live branch).

---

## File Structure

- **Modify:** `src/backtest/unified_backtest.py`
  - New pure helpers `_is_equity_ticker`, `_apply_equity_calendar` (data-loading section, ~line 173).
  - New gate/dispatch helpers `_equity_calendar_enabled`, `_calendar_for`.
  - `load_prices_panels(calendar='union')` — new kwarg; applies the row filter when `'equity'`.
  - `run_backtest` (~line 779) — one-line wiring: `load_prices_panels(calendar=_calendar_for(instrument_class))`.
- **Create:** `tests/test_trading_day_panel.py` — unit tests for all helpers + wiring.

---

## Phase 1 — Build (gate OFF, zero prod behavior change)

### Task 1: Pure calendar helpers

**Files:**
- Modify: `src/backtest/unified_backtest.py` (add two functions just above `def load_prices_panels` at ~line 175)
- Test: `tests/test_trading_day_panel.py`

**Interfaces:**
- Produces: `_is_equity_ticker(ticker: str) -> bool`; `_apply_equity_calendar(close_wide: pd.DataFrame) -> pd.DataFrame` (returns a row-filtered copy/view; columns unchanged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_trading_day_panel.py
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402


class TestIsEquityTicker:
    def test_classifies_tickers(self):
        assert ub._is_equity_ticker('AAPL')
        assert ub._is_equity_ticker('SPY')
        assert not ub._is_equity_ticker('^VIX')
        assert not ub._is_equity_ticker('BTC-USD')
        assert not ub._is_equity_ticker('CL=F')
        assert not ub._is_equity_ticker('EURUSD=X')


class TestApplyEquityCalendar:
    def test_drops_weekend_only_rows(self):
        # Fri, Sat, Sun, Mon. Equity trades Fri+Mon; crypto trades all four.
        idx = pd.to_datetime(['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-08'])
        df = pd.DataFrame({'AAPL': [100.0, np.nan, np.nan, 101.0],
                           'BTC-USD': [40000.0, 40500.0, 40250.0, 41000.0]}, index=idx)
        out = ub._apply_equity_calendar(df)
        assert list(out.index) == [idx[0], idx[3]]
        assert list(out.columns) == ['AAPL', 'BTC-USD']  # columns untouched

    def test_keeps_row_with_any_equity_obs(self):
        idx = pd.to_datetime(['2024-01-05', '2024-01-08'])
        df = pd.DataFrame({'AAPL': [np.nan, 50.0], 'MSFT': [100.0, np.nan],
                           'BTC-USD': [1.0, 2.0]}, index=idx)
        out = ub._apply_equity_calendar(df)
        assert out.shape[0] == 2  # both rows have >=1 equity obs

    def test_no_equity_columns_is_noop(self):
        idx = pd.to_datetime(['2024-01-06', '2024-01-07'])
        df = pd.DataFrame({'BTC-USD': [1.0, 2.0]}, index=idx)
        out = ub._apply_equity_calendar(df)
        assert out.shape[0] == 2  # nothing to anchor on -> return unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py -q`
Expected: FAIL — `AttributeError: module 'backtest.unified_backtest' has no attribute '_is_equity_ticker'`.

- [ ] **Step 3: Write minimal implementation**

In `src/backtest/unified_backtest.py`, immediately above `def load_prices_panels(...)` (currently ~line 175):

```python
def _is_equity_ticker(ticker: str) -> bool:
    """True for cash-equity / ETF tickers; False for indices (^…), crypto
    (…-USD), futures (…=F) and forex (…=X). Defines the equity trading
    calendar (rows on which the equity market was open)."""
    t = str(ticker)
    return (not t.startswith('^')) and ('-USD' not in t) and ('=F' not in t) and ('=X' not in t)


def _apply_equity_calendar(close_wide: pd.DataFrame) -> pd.DataFrame:
    """Restrict a (date × ticker) close panel to the equity trading calendar:
    keep only rows with ≥1 non-NaN equity observation. Drops weekend/holiday
    rows contributed solely by 7-day crypto/forex tickers (which poison
    wide-frame pct_change/rolling, row-count windows and period-start
    detection). Rows only — columns are untouched."""
    eq_cols = [c for c in close_wide.columns if _is_equity_ticker(c)]
    if not eq_cols:
        return close_wide
    equity_day = close_wide[eq_cols].notna().any(axis=1)
    return close_wide.loc[equity_day.values]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_trading_day_panel.py
git commit -m "feat(backtest): equity trading-day calendar helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Gate, dispatch decision, and `calendar` kwarg on `load_prices_panels`

**Files:**
- Modify: `src/backtest/unified_backtest.py` (add `_equity_calendar_enabled`, `_calendar_for`; add `calendar` kwarg to `load_prices_panels`)
- Test: `tests/test_trading_day_panel.py` (append)

**Interfaces:**
- Consumes: `_apply_equity_calendar` (Task 1).
- Produces: `_equity_calendar_enabled() -> bool`; `_calendar_for(instrument_class: str) -> str` (returns `'equity'` or `'union'`); `load_prices_panels(calendar: str = 'union')`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_trading_day_panel.py`)

```python
class TestGateAndDispatch:
    def test_gate_default_off(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', raising=False)
        assert ub._equity_calendar_enabled() is False

    def test_gate_on_off(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '1')
        assert ub._equity_calendar_enabled() is True
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '0')
        assert ub._equity_calendar_enabled() is False

    def test_calendar_for_gate_off(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', raising=False)
        for ic in ('equity', 'etp', 'option', 'crypto'):
            assert ub._calendar_for(ic) == 'union'

    def test_calendar_for_gate_on(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '1')
        assert ub._calendar_for('equity') == 'equity'
        assert ub._calendar_for('etp') == 'equity'
        assert ub._calendar_for('option') == 'equity'
        assert ub._calendar_for('crypto') == 'union'  # crypto NEVER aligned


class TestLoadPricesPanelsDispatch:
    def test_applies_filter_only_for_equity_calendar(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ub, '_apply_equity_calendar',
                            lambda cw: (calls.append('called'), cw)[1])
        # Stub the heavy read + quarantine so no parquet/DB is needed.
        raw = pd.DataFrame({'ticker': ['AAPL'], 'date': ['2024-01-05'],
                            'open': [1.0], 'high': [1.0], 'low': [1.0], 'close': [1.0]})
        monkeypatch.setattr(ub.pd, 'read_parquet', lambda *a, **k: raw.copy())
        import pipeline.quarantine_filter as qf
        monkeypatch.setattr(qf, 'filter_quarantined', lambda p, name: p)

        ub.load_prices_panels(calendar='union')
        assert calls == []
        ub.load_prices_panels(calendar='equity')
        assert calls == ['called']
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py::TestGateAndDispatch -q`
Expected: FAIL — `AttributeError: ... has no attribute '_equity_calendar_enabled'`.

- [ ] **Step 3: Write minimal implementation**

Add (above `load_prices_panels`, alongside the Task-1 helpers):

```python
def _equity_calendar_enabled() -> bool:
    """Gate for the equity trading-day panel. Phase 1: default OFF (union
    panel, byte-identical to legacy). Phase 3 flip: change default to '1'."""
    return os.environ.get('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '0') == '1'


def _calendar_for(instrument_class: str) -> str:
    """Pick the price-panel calendar for a strategy's instrument class.
    Equity-like classes get the equity trading calendar when the gate is on;
    crypto ALWAYS gets the full union calendar (it trades 7 days a week)."""
    if _equity_calendar_enabled() and instrument_class in ('equity', 'etp', 'option'):
        return 'equity'
    return 'union'
```

Then change `load_prices_panels`'s signature and body. Signature (line ~175):

```python
def load_prices_panels(calendar: str = 'union') -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
```

After the existing two lines that build `close_wide` (`close_wide = p.pivot(...)` / `close_wide.index.name = 'date'`), and BEFORE `bars_by_ticker = {...}`, insert:

```python
    if calendar == 'equity':
        close_wide = _apply_equity_calendar(close_wide)
```

(Leave `bars_by_ticker = {t: g.set_index('date')[...] for t, g in p.groupby('ticker')}` unchanged — it is built from `p`, not `close_wide`, so fills are unaffected.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py -q`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_trading_day_panel.py
git commit -m "feat(backtest): gated equity-calendar dispatch + load_prices_panels(calendar=)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire `run_backtest` + full regression

**Files:**
- Modify: `src/backtest/unified_backtest.py` (the `load_prices_panels()` call inside `run_backtest`, ~line 779)
- Test: `tests/test_trading_day_panel.py` (append)

**Interfaces:**
- Consumes: `_calendar_for` (Task 2), `load_prices_panels(calendar=…)` (Task 2).
- Produces: no new public symbol; `run_backtest` now requests the calendar via `_calendar_for(instrument_class)`.

- [ ] **Step 1: Write the failing test** (append)

```python
class TestRunBacktestWiring:
    def test_requests_calendar_by_instrument_class(self, monkeypatch):
        captured = {}

        def fake_load(calendar='union'):
            captured['calendar'] = calendar
            raise RuntimeError('stop-after-load')  # short-circuit the heavy path

        monkeypatch.setattr(ub, 'load_prices_panels', fake_load)
        monkeypatch.setattr(ub, 'find_strategy_file', lambda sid: 'x.py')
        monkeypatch.setattr(ub, 'load_strategy_class',
                            lambda fp: type('S', (), {'__name__': 'S', 'active_in_regimes': []}))
        monkeypatch.setenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '1')

        with pytest.raises(RuntimeError, match='stop-after-load'):
            ub.run_backtest('S_x', instrument_class='equity', commit=False)
        assert captured['calendar'] == 'equity'

        with pytest.raises(RuntimeError, match='stop-after-load'):
            ub.run_backtest('S_x', instrument_class='crypto', commit=False)
        assert captured['calendar'] == 'union'  # crypto stays union even with gate on

    def test_gate_off_requests_union(self, monkeypatch):
        captured = {}

        def fake_load(calendar='union'):
            captured['calendar'] = calendar
            raise RuntimeError('stop-after-load')

        monkeypatch.setattr(ub, 'load_prices_panels', fake_load)
        monkeypatch.setattr(ub, 'find_strategy_file', lambda sid: 'x.py')
        monkeypatch.setattr(ub, 'load_strategy_class',
                            lambda fp: type('S', (), {'__name__': 'S', 'active_in_regimes': []}))
        monkeypatch.delenv('OPENCLAW_BACKTEST_EQUITY_CALENDAR', raising=False)

        with pytest.raises(RuntimeError, match='stop-after-load'):
            ub.run_backtest('S_x', instrument_class='equity', commit=False)
        assert captured['calendar'] == 'union'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py::TestRunBacktestWiring -q`
Expected: FAIL — `captured['calendar'] == 'union'` while expecting `'equity'` (the call still hardcodes `load_prices_panels()`).

- [ ] **Step 3: Write minimal implementation**

In `run_backtest`, replace the data-load line (currently `close_wide, bars_by_ticker = load_prices_panels()`, ~line 779):

```python
    close_wide, bars_by_ticker = load_prices_panels(calendar=_calendar_for(instrument_class))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py -q`
Expected: PASS (all classes).

- [ ] **Step 5: Run the backtest regression suite (gate-off byte-equivalence)**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_unified_backtest_t_plus_1.py tests/test_backtest_fill_model.py tests/test_backtest_instrument_class_dispatch.py -q`
Expected: PASS (no regressions — gate defaults off, so `_calendar_for` returns `'union'` everywhere and the load is identical).

- [ ] **Step 6: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_trading_day_panel.py
git commit -m "feat(backtest): run_backtest selects panel calendar by instrument class

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 2 — Validate (gate-acceptance; no default flip yet)

### Task 4: Both-ways diff of the ~7 wide-frame live strategies + byte-equivalence spot-check

**Files:**
- Create (scratch, not committed): `/tmp/panel_validate.py`

**Goal of this task:** Produce a decision table. Acceptance = (a) the ~7 wide-frame strategies' shifts are explainable (more real trading days per window / recovered period-starts), no strategy silently breaks; (b) ≥5 per-ticker strategies are byte-identical gate-on vs gate-off; (c) IAM goes 0 → >0 trades.

The two strategy sets:
- **Wide-frame (expect shift):** `S_intl_momentum_attention_regime`, `S_3d_pca_characteristic_factors`, `S_markov_frontier_regimes`, `S_epistemic_rank_gate`, `S_tr_02_hurst_regime_flip`, `S_price_path_convexity`, `S_nonstationarity_adaptive_selection`
- **Per-ticker (expect byte-identical):** pick 5 live strategies that use only `prices[ticker].dropna()` (e.g. confirm via `grep -L "axis=1\|\.tail(\|\.iloc\[-" src/strategies/implementations/<id>.py`).
- **Unblock proof:** `S_investor_attention_market_timing`.

- [ ] **Step 1: Write the validation harness**

```python
# /tmp/panel_validate.py  — run one strategy, both calendars, ephemeral (commit=False)
import os, sys
from backtest import unified_backtest as ub

def run(sid):
    out = {}
    for gate in ('0', '1'):
        os.environ['OPENCLAW_BACKTEST_EQUITY_CALENDAR'] = gate
        ic = ub._resolve_instrument_class(sid)
        try:
            _, m = ub.run_backtest(sid, commit=False, return_metrics=True,
                                   instrument_class=ic)
            out[gate] = (m.get('total_trades'), m.get('sharpe'), m.get('max_dd_pct'))
        except Exception as e:
            out[gate] = ('ERR', type(e).__name__, str(e)[:80])
    print(f"{sid}\n  gate=off union : {out['0']}\n  gate=on  equity: {out['1']}", flush=True)

if __name__ == '__main__':
    run(sys.argv[1])
```

- [ ] **Step 2: Run each strategy SEQUENTIALLY (2-core/OOM-safe), capture the table**

```bash
cd /root/openclaw
set -a; export $(grep -E '^(POSTGRES_URI)=' .env | xargs -d '\n'); set +a
for sid in \
  S_intl_momentum_attention_regime S_3d_pca_characteristic_factors \
  S_markov_frontier_regimes S_epistemic_rank_gate S_tr_02_hurst_regime_flip \
  S_price_path_convexity S_nonstationarity_adaptive_selection \
  S_investor_attention_market_timing ; do
    nice -n 19 env PYTHONPATH=src POSTGRES_URI="$POSTGRES_URI" \
      python3 /tmp/panel_validate.py "$sid" 2>/dev/null
done
```
Expected: a printed off/on pair per strategy. IAM shows `gate=off (0, …)` → `gate=on (~1481, ~-0.30)`.

- [ ] **Step 3: Byte-equivalence spot-check (5 per-ticker strategies)**

Run the same loop for the 5 chosen per-ticker strategies.
Expected: `gate=off` and `gate=on` rows **identical** (same `total_trades`, same `sharpe`). Any difference here is a bug — investigate before proceeding (it would mean a per-ticker strategy is secretly row-window-sensitive).

- [ ] **Step 4: Record the decision table in this plan file**

Append a `## Phase 2 results` section to this plan with the off/on table and a one-line acceptance verdict per strategy. Commit:

```bash
git add docs/superpowers/plans/2026-06-20-trading-day-panel-harness.md
git commit -m "docs(plan): trading-day-panel Phase 2 validation results

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

**STOP — operator gate.** Do not proceed to Phase 3 until the operator reviews the Phase-2 table and approves the flip (it will move live regime weights for the wide-frame strategies).

---

## Phase 3 — Flip default-on + re-backtest live book

### Task 5: Flip the gate default, re-backtest, verify

**Files:**
- Modify: `src/backtest/unified_backtest.py` (`_equity_calendar_enabled` default)

- [ ] **Step 1: Flip the default**

Change the default in `_equity_calendar_enabled`:

```python
    return os.environ.get('OPENCLAW_BACKTEST_EQUITY_CALENDAR', '1') == '1'
```

Update the unit test `TestGateAndDispatch::test_gate_default_off` → rename to `test_gate_default_on` and assert `_equity_calendar_enabled() is True` when the env var is unset; keep the explicit `'0'` test asserting `False` (the env var can still force-disable).

- [ ] **Step 2: Run the unit suite**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py -q`
Expected: PASS.

- [ ] **Step 3: Re-backtest the live book (chunked, sequential, OOM-safe)**

```bash
cd /root/openclaw
set -a; export $(grep -E '^(POSTGRES_URI)=' .env | xargs -d '\n'); set +a
# one strategy per subprocess; RSS frees between runs
PYTHONPATH=src python3 - <<'PY' | while read sid; do
import json
m=json.loads(open('src/strategies/manifest.json').read())
print('\n'.join(s for s,e in (m['strategies'] or {}).items() if e.get('state') in ('live','monitoring')))
PY
  nice -n 19 env PYTHONPATH=src POSTGRES_URI="$POSTGRES_URI" \
    python3 -m backtest.unified_backtest --strategy-id "$sid" || echo "FAIL $sid"
done
```
Expected: each strategy writes a fresh `primary_window=true` run; no OOM.

- [ ] **Step 4: Verify panel + weights moved as expected**

```bash
set -a; export $(grep -E '^(POSTGRES_URI)=' .env | xargs -d '\n'); set +a
nice -n 19 env PYTHONPATH=src python3 -m system_checks --tag strategies --json | python3 -m json.tool | head -40
```
Expected: `live_strategies_have_weights` not newly-failing; the ~7 wide-frame strategies' Sharpe/weights match the Phase-2 table; the ~60 per-ticker strategies unchanged.

- [ ] **Step 5: Commit + restart johnbot**

```bash
git add src/backtest/unified_backtest.py tests/test_trading_day_panel.py
git commit -m "feat(backtest): flip equity trading-day panel to default-on

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
Set `OPENCLAW_BACKTEST_EQUITY_CALENDAR=1` in `.env` (explicit, matches default) and note in the activation log. (johnbot restart only if a running process caches the gate; the backtest CLI reads env per-invocation, so a restart is typically unnecessary — confirm before doing it.)

---

## Out of scope (follow-ups, separate plans)

- `quick_backtest._load_prices`, `regime_blended_backtest`, `intraday_regime_backtest`: confirm whether they share the union-index bug; fix separately if so.
- **BAB:** commit a real re-backtest (Sharpe 0.60, 7266 trades) + promotion decision — independent of this harness.
- **IAM:** Sharpe −0.30 = no edge as implemented → keep candidate / deprecate; optional follow-up = resolver-fed cross-section instead of the alphabetical 600-cap.
- `=X` forex tickers leaking into `static_universe` (tiny; left as-is for gate-off byte-equivalence).

---

## Self-Review

- **Spec coverage:** §4 design → Tasks 1-3; §6 rollout Phase 1/2/3 → Tasks 1-3 / Task 4 / Task 5; §5 blast radius → Task 4 strategy lists; §7 testing → Task 1-3 tests + Task 3 Step 5 regression + Task 4 unblock/byte-equiv; §9 acceptance criteria 1-6 → Tasks 3/4/5 steps. Covered.
- **Placeholder scan:** no TBD/TODO; all code shown; the 5 per-ticker strategies for the byte-equivalence check are selected by an explicit `grep -L` rule in Task 4 (chosen at execution time from the live set, by design).
- **Type consistency:** `_is_equity_ticker(str)->bool`, `_apply_equity_calendar(DataFrame)->DataFrame`, `_equity_calendar_enabled()->bool`, `_calendar_for(str)->str`, `load_prices_panels(calendar='union')`. Names identical across all tasks and the spec.
