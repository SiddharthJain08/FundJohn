# System-Wide Trading-Day Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the equity trading-day calendar to the strategy price panel in **both** the live engine and the backtest, controlled by one gate so they never diverge, fixing the distorted-lookback bug that affects cross-sectional strategies live and in backtest.

**Architecture:** Promote the Phase-1 backtest helpers into a shared `src/lib/price_panel.py`. Both `unified_backtest.load_prices_panels` (backtest) and `engine.py:run_strategies` (live, per-strategy) call the same `apply_equity_calendar`, dispatched by `calendar_for(instrument_class)` behind one gate `OPENCLAW_EQUITY_TRADING_CALENDAR`. Equity/etp/option → equity calendar; crypto → union. Default OFF → byte-identical everywhere.

**Tech Stack:** Python 3, pandas; pytest; `src/lib/`, `src/backtest/unified_backtest.py`, `src/execution/engine.py`.

**Spec:** `docs/superpowers/specs/2026-06-20-system-wide-trading-day-panel-design.md`

## Global Constraints

- **NEVER delete from the master database.** Filtering is **in-memory only** — no write to `prices.parquet` or any master table.
- **Gate-OFF byte-equivalence is mandatory in BOTH paths.** With `OPENCLAW_EQUITY_TRADING_CALENDAR` unset/`0`, `run_backtest` AND `run_strategies` must behave exactly as today (`calendar_for` returns `'union'` for every instrument class).
- **One gate, both paths.** The env var is exactly `OPENCLAW_EQUITY_TRADING_CALENDAR`. There is no separate backtest gate after this plan (the old `OPENCLAW_BACKTEST_EQUITY_CALENDAR` is retired).
- **Single shared definition.** `engine.py` and `unified_backtest.py` must import `is_equity_ticker`/`apply_equity_calendar`/`calendar_for` from `lib.price_panel` — no duplicated bodies.
- **Crypto keeps the union calendar** (`calendar_for('crypto') == 'union'`) — `S_btc_momentum` byte-identical.
- **Import path:** modules under `src/` import as top-level under `PYTHONPATH=src` (e.g. `from lib.price_panel import ...`). Test command: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest <path> -q`.
- **2-core/8GB no-swap:** any multi-strategy re-backtest runs one strategy per subprocess, sequentially, `nice -n 19`.
- **`.env` can't be `source`d** — load keys narrowly: `set -a; export $(grep -E '^(POSTGRES_URI)=' .env | xargs -d '\n'); set +a`.
- **Commit footer:** `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Branch:** `feat/intraday-regime-15min-prefetch`.

---

## File Structure

- **Create:** `src/lib/price_panel.py` — shared calendar helpers + gate.
- **Create:** `src/lib/__init__.py` — make `lib` a regular package (parity with `strategies`/`execution`/`regime`).
- **Create:** `tests/test_price_panel.py` — unit tests for the shared module.
- **Modify:** `src/backtest/unified_backtest.py` — replace the 4 Phase-1 helper bodies (lines 175-209) with aliased imports from `lib.price_panel`; `load_prices_panels`/`run_backtest` unchanged.
- **Modify:** `tests/test_trading_day_panel.py` — retarget gate-name references to `OPENCLAW_EQUITY_TRADING_CALENDAR`.
- **Modify:** `src/execution/engine.py` — import the shared helpers; apply the per-strategy calendar in `run_strategies`.
- **Create:** `tests/test_engine_equity_calendar.py` — live-path unit test.

---

## Phase 2 — Build (gate OFF, byte-identical in prod)

### Task 1: Shared module `src/lib/price_panel.py`

**Files:**
- Create: `src/lib/price_panel.py`, `src/lib/__init__.py`
- Test: `tests/test_price_panel.py`

**Interfaces:**
- Produces: `is_equity_ticker(str)->bool`; `apply_equity_calendar(pd.DataFrame)->pd.DataFrame`; `equity_calendar_enabled()->bool`; `calendar_for(str)->str`; `GATE='OPENCLAW_EQUITY_TRADING_CALENDAR'`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_price_panel.py
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from lib import price_panel as pp  # noqa: E402


class TestIsEquityTicker:
    def test_classifies(self):
        assert pp.is_equity_ticker('AAPL')
        assert pp.is_equity_ticker('SPY')
        assert not pp.is_equity_ticker('^VIX')
        assert not pp.is_equity_ticker('BTC-USD')
        assert not pp.is_equity_ticker('CL=F')
        assert not pp.is_equity_ticker('EURUSD=X')


class TestApplyEquityCalendar:
    def test_drops_weekend_only_rows(self):
        idx = pd.to_datetime(['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-08'])  # Fri Sat Sun Mon
        df = pd.DataFrame({'AAPL': [100.0, np.nan, np.nan, 101.0],
                           'BTC-USD': [4e4, 4.05e4, 4.02e4, 4.1e4]}, index=idx)
        out = pp.apply_equity_calendar(df)
        assert list(out.index) == [idx[0], idx[3]]
        assert list(out.columns) == ['AAPL', 'BTC-USD']

    def test_keeps_row_with_any_equity_obs(self):
        idx = pd.to_datetime(['2024-01-05', '2024-01-08'])
        df = pd.DataFrame({'AAPL': [np.nan, 50.0], 'MSFT': [100.0, np.nan]}, index=idx)
        assert pp.apply_equity_calendar(df).shape[0] == 2

    def test_no_equity_columns_noop(self):
        idx = pd.to_datetime(['2024-01-06', '2024-01-07'])
        df = pd.DataFrame({'BTC-USD': [1.0, 2.0]}, index=idx)
        assert pp.apply_equity_calendar(df).shape[0] == 2


class TestGate:
    def test_enabled(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
        assert pp.equity_calendar_enabled() is False
        monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
        assert pp.equity_calendar_enabled() is True
        monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '0')
        assert pp.equity_calendar_enabled() is False

    def test_calendar_for_off(self, monkeypatch):
        monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
        for ic in ('equity', 'etp', 'option', 'crypto'):
            assert pp.calendar_for(ic) == 'union'

    def test_calendar_for_on(self, monkeypatch):
        monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
        assert pp.calendar_for('equity') == 'equity'
        assert pp.calendar_for('etp') == 'equity'
        assert pp.calendar_for('option') == 'equity'
        assert pp.calendar_for('crypto') == 'union'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_price_panel.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'lib.price_panel'`.

- [ ] **Step 3: Write minimal implementation**

`src/lib/__init__.py`: empty file.

`src/lib/price_panel.py`:

```python
"""Shared strategy price-panel calendar — used by BOTH the live engine
(execution.engine.run_strategies) and the backtest (backtest.unified_backtest).
One definition, one gate, so live and backtest never diverge."""
from __future__ import annotations
import os
import pandas as pd

GATE = 'OPENCLAW_EQUITY_TRADING_CALENDAR'


def is_equity_ticker(ticker: str) -> bool:
    """True for cash-equity / ETF tickers; False for indices (^…), crypto
    (…-USD), futures (…=F) and forex (…=X). Defines the equity trading calendar."""
    t = str(ticker)
    return (not t.startswith('^')) and ('-USD' not in t) and ('=F' not in t) and ('=X' not in t)


def apply_equity_calendar(close_wide: pd.DataFrame) -> pd.DataFrame:
    """Restrict a (date × ticker) close panel to the equity trading calendar:
    keep only rows with ≥1 non-NaN equity observation. Drops weekend/holiday rows
    contributed solely by 7-day crypto/forex tickers. Rows only; columns untouched."""
    eq_cols = [c for c in close_wide.columns if is_equity_ticker(c)]
    if not eq_cols:
        return close_wide
    return close_wide.loc[close_wide[eq_cols].notna().any(axis=1).values]


def equity_calendar_enabled() -> bool:
    """One system-wide gate (default OFF) for the equity trading-day panel."""
    return os.environ.get(GATE, '0') == '1'


def calendar_for(instrument_class: str) -> str:
    """Pick the panel calendar for a strategy's instrument class. Equity-like
    classes get the equity calendar when the gate is on; crypto ALWAYS gets the
    full union calendar (it trades 7 days a week)."""
    if equity_calendar_enabled() and instrument_class in ('equity', 'etp', 'option'):
        return 'equity'
    return 'union'
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_price_panel.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add src/lib/__init__.py src/lib/price_panel.py tests/test_price_panel.py
git commit -m "feat(lib): shared equity trading-day calendar module

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Repoint `unified_backtest` to the shared module + unify the gate

**Files:**
- Modify: `src/backtest/unified_backtest.py` (replace local helper bodies, lines 175-209)
- Modify: `tests/test_trading_day_panel.py` (retarget gate name)

**Interfaces:**
- Consumes: `lib.price_panel` (Task 1).
- Produces: no new symbol; `_is_equity_ticker`/`_apply_equity_calendar`/`_equity_calendar_enabled`/`_calendar_for` remain importable from `unified_backtest` as aliases (so `load_prices_panels`/`run_backtest` and existing tests are unchanged), now backed by the shared module + the unified gate.

- [ ] **Step 1: Update the gate-name references in the Phase-1 tests (failing first)**

In `tests/test_trading_day_panel.py`, replace every `OPENCLAW_BACKTEST_EQUITY_CALENDAR` with `OPENCLAW_EQUITY_TRADING_CALENDAR` (classes `TestGateAndDispatch` and `TestRunBacktestWiring`). No other change.

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py::TestGateAndDispatch -q`
Expected: FAIL — `_calendar_for('equity')` returns `'union'` because the code still reads the OLD gate name while the test now sets the NEW one.

- [ ] **Step 3: Replace the local helpers with aliased imports**

In `src/backtest/unified_backtest.py`, DELETE the four helper definitions (`_is_equity_ticker`, `_apply_equity_calendar`, `_equity_calendar_enabled`, `_calendar_for`, lines ~175-209) and add, in their place (still above `load_prices_panels`):

```python
# Equity trading-day calendar — shared with the live engine via lib.price_panel
# so backtest and live never diverge. Aliased to the historical underscore names
# used by load_prices_panels() and run_backtest() below.
from lib.price_panel import (
    is_equity_ticker as _is_equity_ticker,
    apply_equity_calendar as _apply_equity_calendar,
    equity_calendar_enabled as _equity_calendar_enabled,
    calendar_for as _calendar_for,
)
```

Leave `load_prices_panels(calendar=…)` (filter at line ~229) and the `run_backtest` call `load_prices_panels(calendar=_calendar_for(instrument_class))` (line ~816) unchanged.

- [ ] **Step 4: Run the updated + regression suites**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_trading_day_panel.py tests/test_price_panel.py tests/test_unified_backtest_t_plus_1.py tests/test_backtest_fill_model.py -q`
Expected: PASS (Phase-1 tests now pass against the unified gate; regression green).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_trading_day_panel.py
git commit -m "refactor(backtest): use shared lib.price_panel; unify gate to OPENCLAW_EQUITY_TRADING_CALENDAR

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Wire the live engine — `engine.py:run_strategies`

**Files:**
- Modify: `src/execution/engine.py` (import + per-strategy filter in `run_strategies`)
- Test: `tests/test_engine_equity_calendar.py`

**Interfaces:**
- Consumes: `lib.price_panel.apply_equity_calendar`, `lib.price_panel.calendar_for`; existing `instrument_class_for`.
- Produces: `run_strategies` applies the per-strategy equity calendar before `generate_signals`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_equity_calendar.py
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402


class _RecordStrat:
    id = 'S_fake'
    def __init__(self):
        self.seen_index = None
    def generate_signals(self, prices, regime, universe, aux):
        self.seen_index = list(prices.index)
        return []


def _panel():
    idx = pd.to_datetime(['2024-01-05', '2024-01-06', '2024-01-07', '2024-01-08'])  # Fri Sat Sun Mon
    return pd.DataFrame({'AAPL': [100.0, np.nan, np.nan, 101.0],
                         'BTC-USD': [4e4, 4.05e4, 4.02e4, 4.1e4]}, index=idx)


def _wire(monkeypatch, instrument_class):
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: instrument_class)
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_apply_regime_overrides_to_signals', lambda *a, **k: None)


def test_gate_off_keeps_weekend_rows(monkeypatch):
    monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
    _wire(monkeypatch, 'equity')
    s = _RecordStrat()
    engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['AAPL'], {})
    assert len(s.seen_index) == 4  # weekend rows present (byte-identical)


def test_gate_on_equity_drops_weekend_rows(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
    _wire(monkeypatch, 'equity')
    s = _RecordStrat()
    engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['AAPL'], {})
    assert len(s.seen_index) == 2  # Sat/Sun dropped


def test_gate_on_crypto_keeps_weekend_rows(monkeypatch):
    monkeypatch.setenv('OPENCLAW_EQUITY_TRADING_CALENDAR', '1')
    _wire(monkeypatch, 'crypto')
    # crypto path needs a regime; stub the loader to a usable state
    monkeypatch.setattr(engine, 'load_crypto_regime_state', lambda: {'state': 'LOW_VOL'})
    s = _RecordStrat()
    engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['BTC-USD'], {})
    assert len(s.seen_index) == 4  # crypto keeps the union calendar
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_engine_equity_calendar.py -q`
Expected: FAIL — `test_gate_on_equity_drops_weekend_rows` sees 4 rows (no filter wired yet).

- [ ] **Step 3: Implement the wiring**

In `src/execution/engine.py`, add to the import block (after line ~40):

```python
from lib.price_panel import apply_equity_calendar, calendar_for
```

In `run_strategies`, compute the instrument class once at the top of the loop body and reuse it for both the existing crypto branch and the new calendar filter. Change the crypto check (line ~869) from `if instrument_class_for(strat.id) == 'crypto':` to:

```python
            ic = instrument_class_for(strat.id)
            if ic == 'crypto':
```

Then, immediately before the `signals = strat.generate_signals(...)` call (line ~893), insert:

```python
            if calendar_for(ic) == 'equity':
                strat_prices = apply_equity_calendar(strat_prices)
```

(`strat_prices` has already been assigned in the `if strategy_universes …/else` block above.)

- [ ] **Step 4: Run the test + an engine regression**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_engine_equity_calendar.py -q`
Expected: PASS (3 tests).

Then a focused engine regression (gate-off byte-equivalence in the live path):
Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/ -q -k "engine and not equity_calendar"`
Expected: PASS (no behavior change with the gate off).

- [ ] **Step 5: Commit**

```bash
git add src/execution/engine.py tests/test_engine_equity_calendar.py
git commit -m "feat(engine): apply equity trading-day calendar per-strategy in run_strategies (gated)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 3 — Validate (operational, informational)

### Task 4: Review the full-book backtest diff

**Files:** none (reads `/tmp/panel_full.log` from the running sweep).

- [ ] **Step 1: Wait for the sweep to finish**

Check: `grep -c '^RESULT|' /tmp/panel_full.log` reaches 134, or `grep 'SWEEP DONE' /tmp/panel_full.log`.

- [ ] **Step 2: Build the off→on table + flag pathologies**

```bash
cd /root/openclaw && nice -n 19 python3 - <<'PY'
rows={}
for ln in open('/tmp/panel_full.log'):
    if ln.startswith('RESULT|'):
        _,sid,g,t,s,d=ln.strip().split('|')[:6]
        rows.setdefault(sid,{})[g]=(t,s,d)
path=[]
for sid,gg in rows.items():
    if '0' in gg and '1' in gg:
        off,on=gg['0'],gg['1']
        if on[0]=='ERR' or on[1]=='None':   # crash / NaN / 0-trades under the fix
            path.append((sid,off,on))
        print(sid, 'off',off,'on',on)
print('\nPATHOLOGIES (ERR/None under gate-on):', path)
PY
```
Expected: a per-strategy off→on table. **Acceptance = no pathologies** (no strategy that crashes, NaNs, or drops to 0 trades *that traded before*). A merely-lower Sharpe is the intended de-inflation, NOT a blocker.

- [ ] **Step 3: Record the table + verdict in this plan, commit**

Append a `## Phase 3 results` section (the table + "no pathologies / list of pathologies"). Commit:
```bash
git add docs/superpowers/plans/2026-06-20-system-wide-trading-day-panel.md
git commit -m "docs(plan): system-wide panel Phase 3 validation table

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

**If pathologies exist:** STOP and surface to the operator before Phase 4 — a strategy that crashes/NaNs under the correct panel is a real bug to fix first.

---

## Phase 4 — Flip + re-backtest + monitor (operational)

### Task 5: Activate the gate system-wide

**Files:** `.env` (operational; not committed).

- [ ] **Step 1: Set the gate in `.env`**

Add `OPENCLAW_EQUITY_TRADING_CALENDAR=1` to `/root/openclaw/.env` (so johnbot + the backtest/refresh services inherit it). Confirm it's present:
```bash
grep OPENCLAW_EQUITY_TRADING_CALENDAR /root/openclaw/.env
```

- [ ] **Step 2: Re-backtest the live/monitoring book on the equity panel (chunked, sequential, OOM-safe)**

```bash
cd /root/openclaw
set -a; export $(grep -E '^(POSTGRES_URI|OPENCLAW_EQUITY_TRADING_CALENDAR)=' .env | xargs -d '\n'); set +a
PYTHONPATH=src python3 - <<'PY' > /tmp/live_strats.txt
import json
m=json.loads(open('src/strategies/manifest.json').read())
print('\n'.join(s for s,e in (m['strategies'] or {}).items() if e.get('state') in ('live','monitoring')))
PY
while read sid; do
  nice -n 19 env PYTHONPATH=src POSTGRES_URI="$POSTGRES_URI" OPENCLAW_EQUITY_TRADING_CALENDAR=1 \
    python3 -m backtest.unified_backtest --strategy-id "$sid" || echo "FAIL $sid"
done < /tmp/live_strats.txt
```
Expected: each writes a fresh `primary_window=true` run on the equity panel; no OOM.

- [ ] **Step 3: Refresh weights from the re-backtested metrics**

Run: `cd /root/openclaw && set -a; export $(grep -E '^(POSTGRES_URI)=' .env | xargs -d '\n'); set +a; nice -n 19 env PYTHONPATH=src POSTGRES_URI="$POSTGRES_URI" python3 -m execution.strategy_weights --rebuild`
Expected: weights rebuilt from the corrected Sharpes.

- [ ] **Step 4: Restart johnbot so live picks up the gate**

Restart the johnbot user service (confirm the exact unit before acting — it is a `systemctl --user` service under root's `/run/user/0`; do NOT start the colliding system unit). Verify `:3000` healthy.

- [ ] **Step 5: Monitor the first live EOD compute (16:15 ET)**

Watch the next EOD compute log: confirm cross-sectional strategies emit the expected (lower, per the Phase-3 table) signal counts, the delta-based sizer nets positions (no blow-out), and no errors. The gate (`OPENCLAW_EQUITY_TRADING_CALENDAR=0`) is the instant revert if anything looks wrong.

---

## Self-Review

- **Spec coverage:** §4.1 shared module → Task 1; §4.2 backtest repoint → Task 2; §4.3 live wiring → Task 3; §4.4 one gate → Tasks 1-3 (shared `calendar_for`); §5 validation → Task 4; §6 rollout P1(reused)/P2/P3/P4 → Tasks 1-3 / Task 4 / Task 5; §8 acceptance 1-7 → Task tests + Tasks 4-5. Covered.
- **Placeholder scan:** no TBD/TODO; all code shown; the Phase-3 acceptance ("no pathologies") and the Phase-4 johnbot restart name a concrete verification, not a vague directive.
- **Type consistency:** `is_equity_ticker(str)->bool`, `apply_equity_calendar(DataFrame)->DataFrame`, `equity_calendar_enabled()->bool`, `calendar_for(str)->str`, `GATE='OPENCLAW_EQUITY_TRADING_CALENDAR'` — identical across Task 1 (definition), Task 2 (aliases), Task 3 (live use), and the spec.
