# Oxfordstrat Strategy Library → Research Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, backtest, and register ~30 curated oxfordstrat.com systematic strategies as research candidates on `GET /api/strategies`, each with full backtest metrics (incl. sortino/calmar), sorted by Sharpe.

**Architecture:** A shared `OxfordBaseStrategy` (in `oxford_crabel.py`) lazily self-loads liquid-ETF-basket OHLC from the master parquet and caches it per instance; ~30 thin `oxf_*.py` subclasses emit per-ticker `Signal`s over that basket. Each is backtested via `unified_backtest.py --strategy-file` (which already computes sortino/calmar but drops them at the INSERT — migration 135 + an INSERT/page extension fix that for ALL strategies) and registered as a `state='candidate'` manifest entry + `strategy_registry` row. Candidates do not trade; `_IMPL_MAP` is untouched.

**Tech Stack:** Python 3 (pandas/numpy, psycopg2), PostgreSQL, Node/Express dashboard (`server.js`, `strategy_row.js`), pytest.

**Key facts established during design (do not re-litigate):**
- The backtest fills every signal at **close[t+1]**, overriding `entry_price` (`_per_bar_simulate`); exits are path-checked intraday. Faithful for "condition-at-close" rules; stop-entry rules are implemented as **day-t confirmed-breakout adaptations** (trigger checked against signal-day OHLC).
- `generate_signals(prices, regime, universe, aux_data)` receives a **close-only** wide panel. OHLC is self-loaded from `data/master/prices.parquet` (the accepted `S_fomc_presell_spy_long` pattern), cached on the instance (the backtest creates one instance per run at `unified_backtest.py:775`, then loops bars).
- `aggregate_metrics` (total) and `aggregate_per_regime` (per regime, NULL under 5 trades) already compute `sortino`/`calmar`/`avg_pnl_pct`. They are dropped at the `strategy_backtest_runs`/`_regimes` INSERT.
- Box is 2-core, 8GB, no swap (OOM history): **backtests run sequentially, `nice -n 19`**.

**Conventions:**
- Strategy ids are prefixed `oxf_`. Files in `src/strategies/implementations/`.
- Commit after every green step. Run pytest with `cd /root/openclaw && python3 -m pytest <path> -v`.
- Backtests: `cd /root/openclaw && POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-) nice -n 19 python3 -m backtest.unified_backtest --strategy-file <path>`.

---

## Phase 0 — Metrics plumbing (persist sortino/calmar/avg_pnl)

Independent of the strategies; benefits all strategies. Do first so the slice can verify full metrics end-to-end.

### Task 1: Migration 135 — additive sortino/calmar/avg_pnl columns

**Files:**
- Create: `src/database/migrations/135_backtest_sortino_calmar.sql`

- [ ] **Step 1: Write the migration (additive only — append-only invariant)**

```sql
-- 135_backtest_sortino_calmar.sql
-- Persist sortino/calmar/avg_pnl that aggregate_metrics already computes but
-- the strategy_backtest_runs/_regimes INSERTs drop. Additive columns only.
ALTER TABLE strategy_backtest_runs
  ADD COLUMN IF NOT EXISTS total_sortino     NUMERIC,
  ADD COLUMN IF NOT EXISTS total_calmar      NUMERIC,
  ADD COLUMN IF NOT EXISTS total_avg_pnl_pct NUMERIC;

ALTER TABLE strategy_backtest_regimes
  ADD COLUMN IF NOT EXISTS sortino NUMERIC,
  ADD COLUMN IF NOT EXISTS calmar  NUMERIC;
```

- [ ] **Step 2: Apply it to the live DB** (psql is not installed — use psycopg2)

Run:
```bash
cd /root/openclaw && POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-) python3 -c "
import os, psycopg2
sql = open('src/database/migrations/135_backtest_sortino_calmar.sql').read()
c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
cur.execute(sql); c.commit()
cur.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='strategy_backtest_runs' AND column_name IN ('total_sortino','total_calmar','total_avg_pnl_pct') ORDER BY 1\")
print('runs cols:', [r[0] for r in cur.fetchall()])
cur.execute(\"SELECT column_name FROM information_schema.columns WHERE table_name='strategy_backtest_regimes' AND column_name IN ('sortino','calmar') ORDER BY 1\")
print('regime cols:', [r[0] for r in cur.fetchall()])
c.close()"
```
Expected: `runs cols: ['total_avg_pnl_pct', 'total_calmar', 'total_sortino']` and `regime cols: ['calmar', 'sortino']`.

- [ ] **Step 3: Commit**

```bash
git add src/database/migrations/135_backtest_sortino_calmar.sql
git commit -m "feat(backtest): migration 135 — add sortino/calmar/avg_pnl columns"
```

### Task 2: Write the new metric columns in the unified_backtest INSERTs

**Files:**
- Modify: `src/backtest/unified_backtest.py:832-891` (the runs INSERT + the regime rows)
- Test: `tests/test_backtest_sortino_calmar_persist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_sortino_calmar_persist.py
"""Migration 135 wiring: a backtest run persists total_sortino/total_calmar/
total_avg_pnl_pct on strategy_backtest_runs and sortino/calmar on _regimes."""
import os, psycopg2, pytest

pytestmark = pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='needs DB')

def test_sortino_calmar_columns_exist_and_are_written():
    c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
    # The columns exist (migration 135 applied)
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='strategy_backtest_runs'
                   AND column_name IN ('total_sortino','total_calmar','total_avg_pnl_pct')""")
    assert {r[0] for r in cur.fetchall()} == {'total_sortino','total_calmar','total_avg_pnl_pct'}
    # The latest run for any strategy with >5 trades has a non-null sortino
    cur.execute("""SELECT total_sortino, total_calmar FROM strategy_backtest_runs
                   WHERE primary_window=TRUE AND total_trades > 20
                   ORDER BY run_at DESC LIMIT 1""")
    row = cur.fetchone()
    if row is not None:  # only assert once at least one post-135 run exists
        assert row[0] is not None, 'total_sortino should be populated for a >20-trade run'
    c.close()
```

- [ ] **Step 2: Run it — confirm it fails before the INSERT change**

Run: `cd /root/openclaw && POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-) python3 -m pytest tests/test_backtest_sortino_calmar_persist.py -v`
Expected: the columns-exist assert PASSES (migration applied), the value assert is skipped until a post-135 backtest runs (row is None). After the slice runs a backtest it must hold.

- [ ] **Step 3: Extend the runs INSERT**

In `src/backtest/unified_backtest.py`, change the runs INSERT (currently lines 833-857). Add the three columns + three values, sourcing from `total_metrics` (already has `sortino`/`calmar`/`avg_pnl_pct`):

```python
        cur.execute("""
            INSERT INTO strategy_backtest_runs
              (run_id, strategy_id, code_sha, window_kind, start_date, end_date,
               oos_days, total_sharpe, total_max_dd_pct, total_return_pct,
               total_trades, total_hit_rate, avg_holding_days, primary_window,
               config_json, notes,
               total_sortino, total_calmar, total_avg_pnl_pct)
            VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s,%s)
        """, (
            run_id, strategy_id, _code_sha(filepath), 'full_history',
            start_dt.date(), end_dt.date(), int((end_dt - start_dt).days + 1),
            total_metrics['sharpe'], total_metrics['max_dd_pct'],
            total_metrics['return_pct'], total_metrics['total_trades'],
            total_metrics['hit_rate'], total_metrics['avg_holding_days'],
            True,
            json.dumps({
                'max_hold_days':  max_hold_days,
                'min_lookback':   min_lookback,
                'start_date':     start_dt.date().isoformat(),
                'end_date':       end_dt.date().isoformat(),
                'universe_size':  (round(sum(universe_sizes) / len(universe_sizes), 2)
                                   if resolver is not None and universe_sizes
                                   else len(universe)),
                'methodology':    'discovery',
            }),
            None,
            total_metrics['sortino'], total_metrics['calmar'], total_metrics['avg_pnl_pct'],
        ))
```

- [ ] **Step 4: Extend the regime rows INSERT** (lines 867-891). Add `sortino`/`calmar` to both the zero-trade and non-zero branches + the column list:

```python
        regime_rows = []
        for regime in CANONICAL_REGIMES:
            agg = per_regime.get(regime, {})
            n_trades = int(agg.get('trade_count', 0) or 0)
            if n_trades == 0:
                regime_rows.append((
                    run_id, regime,
                    0, None, None, None, None, None, None,
                    int(agg.get('oos_days_in_regime') or 0),
                    None, None,
                ))
                continue
            regime_rows.append((
                run_id, regime,
                agg['trade_count'], agg['sharpe'], agg['max_dd_pct'],
                agg['return_pct'], agg['hit_rate'], agg['avg_pnl_pct'],
                agg['avg_holding_days'], agg['oos_days_in_regime'],
                agg.get('sortino'), agg.get('calmar'),
            ))
        if regime_rows:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO strategy_backtest_regimes
                  (run_id, regime_state, trade_count, sharpe, max_dd_pct,
                   return_pct, hit_rate, avg_pnl_pct, avg_holding_days,
                   oos_days_in_regime, sortino, calmar)
                VALUES %s
            """, regime_rows)
```

- [ ] **Step 5: Run the existing backtest regression to confirm no break**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py -v`
Expected: PASS (the INSERT change is additive; existing tests that don't assert the new columns stay green).

- [ ] **Step 6: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_backtest_sortino_calmar_persist.py
git commit -m "feat(backtest): persist sortino/calmar/avg_pnl in backtest run + regime rows"
```

### Task 3: Expose sortino/calmar on the candidates page + sort by Sharpe

**Files:**
- Modify: `src/channels/api/server.js` (the `unifiedBacktest` SELECT ~1143-1145, the per-sid object ~1164, the candidate row mapper ~1359-1362)
- Modify: `src/channels/api/strategy_row.js:25-28`

- [ ] **Step 1: Extend the unifiedBacktest SELECT** (server.js ~1143). Add the three new columns:

```js
      SELECT DISTINCT ON (strategy_id)
             strategy_id, total_sharpe, total_max_dd_pct, total_return_pct,
             total_trades, total_hit_rate, avg_holding_days, run_at,
             total_sortino, total_calmar, total_avg_pnl_pct
      FROM strategy_backtest_runs
      WHERE primary_window = TRUE
```
(Match the existing column-list formatting; keep the existing `ORDER BY ... run_at DESC` / DISTINCT ON.)

- [ ] **Step 2: Carry them onto the per-sid object** (server.js ~1164, where `sharpe: r.total_sharpe` is built). Add:

```js
        sharpe:      r.total_sharpe,
        sortino:     r.total_sortino,
        calmar:      r.total_calmar,
        avg_pnl_pct: r.total_avg_pnl_pct,
```

- [ ] **Step 3: Emit them on the candidate row** (server.js ~1359, next to `backtest_sharpe`). Add:

```js
        backtest_sharpe:           unifiedBacktest[sid]?.sharpe      ?? sr.backtest_sharpe      ?? null,
        backtest_sortino:          unifiedBacktest[sid]?.sortino     ?? null,
        backtest_calmar:           unifiedBacktest[sid]?.calmar      ?? null,
        backtest_avg_pnl_pct:      unifiedBacktest[sid]?.avg_pnl_pct ?? null,
        backtest_trade_count:      unifiedBacktest[sid]?.trade_count ?? sr.backtest_trade_count ?? null,
```

- [ ] **Step 4: Mirror in strategy_row.js** (lines 25-28). Add after `sharpe`:

```js
    sharpe: run.total_sharpe ?? null,
    sortino: run.total_sortino ?? null,
    calmar: run.total_calmar ?? null,
    backtest_return_pct: run.total_return_pct ?? null,
    backtest_max_dd_pct: run.total_max_dd_pct ?? null,
    backtest_avg_pnl_pct: run.total_avg_pnl_pct ?? null,
```

- [ ] **Step 5: Sort candidates by Sharpe.** Locate the candidate array the `_renderCandidates` path returns (search `_renderCandidates` / the array that holds the rows pushed at ~1359). Add a sort just before it is returned/rendered, nulls last:

```js
    candidates.sort((a, b) =>
      (b.backtest_sharpe ?? -Infinity) - (a.backtest_sharpe ?? -Infinity));
```
If the table is sorted client-side already (check the front-end `_renderCandidates`), confirm the sort key is `backtest_sharpe` desc and leave server order as-is; otherwise add the server sort above. Document which you did in the commit message.

- [ ] **Step 6: Verify the endpoint serves the new fields** (after the slice has at least one backtested oxf candidate; otherwise just confirm no 500):

Run:
```bash
cd /root/openclaw && curl -s localhost:3000/api/strategies | python3 -c "import sys,json; d=json.load(sys.stdin); rows=d if isinstance(d,list) else d.get('strategies',d); print('keys sample:', sorted(set().union(*[set(r.keys()) for r in rows[:5]])) if rows else 'no rows')" 2>/dev/null | tr ',' '\n' | grep -i "sortino\|calmar\|sharpe" || echo "restart johnbot to pick up server.js changes"
```
Expected: `backtest_sortino`, `backtest_calmar`, `backtest_sharpe` present. (johnbot is a root user-systemd service — restart per the runbook in the final task, not here.)

- [ ] **Step 7: Commit**

```bash
git add src/channels/api/server.js src/channels/api/strategy_row.js
git commit -m "feat(dashboard): expose backtest sortino/calmar/avg_pnl + sort candidates by Sharpe"
```

---

## Phase 1 — Shared helper `oxford_crabel.py`

### Task 4: OxfordBaseStrategy + core indicators (TDD)

**Files:**
- Create: `src/strategies/oxford_crabel.py`
- Test: `tests/test_oxford_crabel.py`

- [ ] **Step 1: Write failing tests for the indicators + OHLC cache**

```python
# tests/test_oxford_crabel.py
import pandas as pd, numpy as np
from strategies.oxford_crabel import (
    atr, donchian_prev, sma, rsi_wilder, true_range_series,
    avg_noise, is_nrn, gap_dir, OXFORD_ETF_BASKET)

def _bars(highs, lows, closes, opens=None):
    n = len(closes)
    idx = pd.date_range('2020-01-01', periods=n, freq='B')
    opens = opens or closes
    return pd.DataFrame({'open':opens,'high':highs,'low':lows,'close':closes}, index=idx)

def test_atr_true_range():
    b = _bars([11,12,13,14,15,16],[9,10,11,12,13,14],[10,11,12,13,14,15])
    a = atr(b, n=3)
    assert a > 0 and np.isfinite(a)

def test_donchian_prev_excludes_current_bar():
    # Upper channel over prior n bars must NOT include the last (current) bar.
    b = _bars([10,10,10,99],[1,1,1,1],[5,5,5,50])
    up, lo = donchian_prev(b, n=3)
    assert up == 10.0 and lo == 1.0  # the 99 high on the current bar is excluded

def test_sma():
    s = pd.Series([1,2,3,4,5])
    assert sma(s, 5) == 3.0

def test_rsi_bounds():
    s = pd.Series(np.linspace(1, 2, 60))  # monotone up
    r = rsi_wilder(s, 14)
    assert 50 < r <= 100

def test_is_nrn_true_for_narrowest():
    # last bar range smallest of the prior n
    b = _bars([20,20,20,11],[10,10,10,10],[15,15,15,10.5])
    assert is_nrn(b, n=3) is True

def test_gap_dir():
    b = _bars([12,30],[8,25],[10,28],opens=[10,26])
    assert gap_dir(b) == 1  # today's low (25) > yesterday's high (12) → gap up

def test_basket_constant_is_present_tickers():
    assert 'SPY' in OXFORD_ETF_BASKET and 'GLD' in OXFORD_ETF_BASKET
    assert 'FXE' not in OXFORD_ETF_BASKET  # absent from panel, excluded
```

- [ ] **Step 2: Run — expect ImportError/fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_oxford_crabel.py -v`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement `oxford_crabel.py`**

```python
"""Shared Crabel/Oxford daily-bar helpers + OHLC-aware base class.

oxfordstrat.com strategies are single-instrument daily-OHLC rules. generate_signals
receives a CLOSE-ONLY wide panel, so OxfordBaseStrategy self-loads basket OHLC from
the master parquet (filtered to the basket) and caches it on the instance (the
backtest builds one instance per run, then loops bars). All indicator helpers are
pure functions over an OHLC DataFrame indexed by date with open/high/low/close.
"""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal

# Liquid ETF basket proxying Oxford's 4 futures sectors. Verified present in
# data/master/prices.parquet 2026-06-15. CORE = full ~10y history; EXT = ~5y.
OXFORD_ETF_BASKET_CORE = [
    'SPY','QQQ','IWM','DIA','EFA','EEM','VTI','TLT','IEF','SHY','LQD','HYG','AGG',
    'GLD','SLV','USO','UNG','XLE','XLF','XLK','XLV','XLI','XLP','XLU','XLY','XLB','GDX']
OXFORD_ETF_BASKET_EXT = [
    'DBC','DBA','DBB','CPER','PALL','PPLT','CORN','WEAT','SOYB','MDY','UUP','UDN','FXF']
OXFORD_ETF_BASKET = OXFORD_ETF_BASKET_CORE + OXFORD_ETF_BASKET_EXT


def _prices_parquet_path() -> Path:
    return Path(__file__).resolve().parents[2] / 'data' / 'master' / 'prices.parquet'


def true_range_series(bars: pd.DataFrame) -> pd.Series:
    h, l, c = bars['high'], bars['low'], bars['close']
    pc = c.shift(1)
    return pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(bars: pd.DataFrame, n: int = 20) -> float:
    if len(bars) < n + 1:
        return float('nan')
    return float(true_range_series(bars).rolling(n).mean().iloc[-1])


def donchian_prev(bars: pd.DataFrame, n: int) -> tuple[float, float]:
    """Upper/lower Donchian over the n bars BEFORE the current bar (no same-bar leak)."""
    if len(bars) < n + 1:
        return float('nan'), float('nan')
    up = float(bars['high'].iloc[-(n+1):-1].max())
    lo = float(bars['low'].iloc[-(n+1):-1].min())
    return up, lo


def sma(s: pd.Series, n: int) -> float:
    if len(s) < n:
        return float('nan')
    return float(s.rolling(n).mean().iloc[-1])


def rsi_wilder(s: pd.Series, n: int) -> float:
    if len(s) < n + 1:
        return float('nan')
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] > 1e-12 else float('inf')
    return float(100 - 100/(1+rs))


def avg_noise(bars: pd.DataFrame, n: int = 10) -> float:
    """Crabel stretch base: mean over n bars of min(open-low, high-open)."""
    if len(bars) < n:
        return float('nan')
    sub = bars.iloc[-n:]
    noise = np.minimum((sub['open']-sub['low']).abs(), (sub['high']-sub['open']).abs())
    return float(noise.mean())


def is_nrn(bars: pd.DataFrame, n: int) -> bool:
    """True if the current bar's range is the narrowest of the last n bars (NR-n / NR7)."""
    if len(bars) < n:
        return False
    rng = (bars['high'] - bars['low']).iloc[-n:]
    return bool(rng.iloc[-1] == rng.min() and rng.iloc[-1] < rng.iloc[:-1].min())


def gap_dir(bars: pd.DataFrame) -> int:
    """+1 if today gapped fully up (low > prior high), -1 if down (high < prior low), else 0."""
    if len(bars) < 2:
        return 0
    today, prev = bars.iloc[-1], bars.iloc[-2]
    if today['low'] > prev['high']:
        return 1
    if today['high'] < prev['low']:
        return -1
    return 0


class OxfordBaseStrategy(BaseStrategy):
    """BaseStrategy + lazy, cached, point-in-time basket OHLC self-load."""
    instrument_class = 'etp'

    def __init__(self, parameters: dict = None):
        super().__init__(parameters)
        self._ohlc_cache: Optional[Dict[str, pd.DataFrame]] = None

    def _load_basket_ohlc(self) -> Dict[str, pd.DataFrame]:
        if self._ohlc_cache is not None:
            return self._ohlc_cache
        cache: Dict[str, pd.DataFrame] = {}
        try:
            df = pd.read_parquet(
                _prices_parquet_path(),
                columns=['ticker','date','open','high','low','close'],
                filters=[('ticker','in',OXFORD_ETF_BASKET)])
            df['date'] = pd.to_datetime(df['date'])
            for t, g in df.groupby('ticker'):
                cache[t] = g.set_index('date')[['open','high','low','close']].sort_index()
        except Exception:
            cache = {}
        self._ohlc_cache = cache
        return cache

    def basket_ohlc(self, prices: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """Point-in-time OHLC per basket ticker, sliced to the signal date (no look-ahead)."""
        if prices is None or prices.empty:
            return {}
        asof = prices.index[-1]
        out: Dict[str, pd.DataFrame] = {}
        for t, bars in self._load_basket_ohlc().items():
            b = bars.loc[:asof]
            if len(b) >= self.min_lookback:
                out[t] = b
        return out

    # NOTE: brackets are NOT computed here. All oxf_* strategies use the house
    # BaseStrategy.compute_stops_and_targets (regime-scaled ATR(14)×2 stop +
    # 5/10/20% targets) — the SAME risk management every other candidate on the
    # page uses, so metrics are comparable. The Oxford contribution is the entry
    # SIGNAL; sizing/brackets are house-standard. (A custom ATR×6 Oxford bracket
    # was considered and rejected: it deviates from the book and, with the 21-day
    # house max_hold, would just make these strategies incomparable scalps.)
```

- [ ] **Step 4: Run the tests — expect PASS**

Run: `cd /root/openclaw && python3 -m pytest tests/test_oxford_crabel.py -v`
Expected: PASS (8 tests). (The OHLC-cache self-load is covered indirectly here; it is exercised live in the slice, Task 7.)

- [ ] **Step 5: Commit**

```bash
git add src/strategies/oxford_crabel.py tests/test_oxford_crabel.py
git commit -m "feat(strategies): oxford_crabel shared helper — OHLC base + Crabel indicators"
```

---

## Phase 2 — Vertical slice (2 strategies, validate end-to-end)

Builds one faithful trend rule + one stop-entry adaptation, then takes BOTH fully through backtest → trade inspection → registration → page render. **Do not start Phase 3 until Task 7 passes.**

### Task 5: `oxf_donchian_breakout` (faithful trend) + contract test

**Files:**
- Create: `src/strategies/implementations/oxf_donchian_breakout.py`
- Create: `tests/test_oxf_contract.py` (generic — reused by every oxf strategy)
- Test: `tests/test_oxf_donchian_breakout.py`

- [ ] **Step 1: Write the generic contract test + a Donchian-specific test**

```python
# tests/test_oxf_contract.py
"""Generic contract every oxf_* strategy must satisfy on a synthetic panel."""
import importlib, pkgutil, pandas as pd, numpy as np, pytest
import strategies.implementations as impl
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, OXFORD_ETF_BASKET

def _oxf_classes():
    out = []
    for m in pkgutil.iter_modules(impl.__path__):
        if not m.name.startswith('oxf_'):
            continue
        mod = importlib.import_module(f'strategies.implementations.{m.name}')
        for obj in vars(mod).values():
            if (isinstance(obj, type) and issubclass(obj, OxfordBaseStrategy)
                    and obj is not OxfordBaseStrategy):
                out.append(obj)
    return out

def _fake_close_panel(days=400):
    idx = pd.date_range('2021-01-04', periods=days, freq='B')
    rng = np.random.default_rng(0)
    data = {t: 100*np.cumprod(1+rng.normal(0.0003,0.012,days)) for t in OXFORD_ETF_BASKET}
    return pd.DataFrame(data, index=idx)

@pytest.mark.parametrize('cls', _oxf_classes(), ids=lambda c: c.id)
def test_contract(cls):
    s = cls()
    panel = _fake_close_panel()
    for regime in ('LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'):
        sigs = s.generate_signals(panel, {'state': regime}, list(panel.columns))
        assert isinstance(sigs, list)
        assert len(sigs) <= s.MAX_SIGNALS
        for sg in sigs:
            assert isinstance(sg, Signal)
            assert sg.direction in ('LONG','SHORT')
            assert sg.ticker in OXFORD_ETF_BASKET
            assert sg.entry_price > 0
            if sg.direction == 'LONG':
                assert sg.stop_loss < sg.entry_price < sg.target_1
            else:
                assert sg.target_1 < sg.entry_price < sg.stop_loss

@pytest.mark.parametrize('cls', _oxf_classes(), ids=lambda c: c.id)
def test_does_not_depend_on_universe_arg(cls):
    """Strategies must iterate the self-loaded basket, NOT the `universe` arg
    (the backtest may pass an sp500-scoped list that excludes ETFs). Passing an
    EMPTY universe must yield the same emitted tickers as a full one."""
    s = cls(); panel = _fake_close_panel(); reg = {'state': 'LOW_VOL'}
    empty = {sg.ticker for sg in s.generate_signals(panel, reg, [])}
    full  = {sg.ticker for sg in cls().generate_signals(panel, reg, list(panel.columns))}
    assert empty == full, f'{cls.id} changed output based on the universe arg'
```

```python
# tests/test_oxf_donchian_breakout.py
import pandas as pd
from strategies.implementations.oxf_donchian_breakout import OxfDonchianBreakout

def test_instantiates_and_declares_id():
    s = OxfDonchianBreakout()
    assert s.id == 'oxf_donchian_breakout'
    assert s.instrument_class == 'etp'
    assert set(s.active_in_regimes) <= {'LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'}
```

- [ ] **Step 2: Run — expect collection error / fail (module missing)**

Run: `cd /root/openclaw && python3 -m pytest tests/test_oxf_donchian_breakout.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the strategy** (faithful: long when close breaks above the prior 20-day Donchian upper channel; short on lower break; condition true at close[t], engine fills close[t+1])

```python
"""Donchian channel breakout (oxfordstrat.com/trading-strategies/donchian-channel-2/).
Faithful daily-bar rule: enter when the close pierces the prior N-day Donchian
channel. Condition is evaluated at close[t]; the engine fills at close[t+1].
ATR(20)-multiple brackets (Oxford ATR_Stop default). ETF-basket cross-section.
"""
from __future__ import annotations
import sys
from typing import List
import pandas as pd
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, donchian_prev, atr

__all__ = ['OxfDonchianBreakout']


class OxfDonchianBreakout(OxfordBaseStrategy):
    id                = 'oxf_donchian_breakout'
    name              = 'Oxford Donchian Channel Breakout'
    description       = 'Donchian N-day channel breakout on liquid ETFs (oxfordstrat donchian-channel-2). Daily-bar, close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 60
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'channel_length': 40}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        n = int(p['channel_length'])
        # IGNORE the `universe` arg (the backtest may pass an sp500-scoped list
        # that excludes ETFs). Iterate the self-loaded basket OHLC directly —
        # the proven S_commodity_etp_momentum pattern. Fills only need the
        # ticker in the full panel's bars_by_ticker, which the basket is.
        ohlc = self.basket_ohlc(prices)
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < n + 2:
                continue
            up, lo = donchian_prev(bars, n)
            close = float(bars['close'].iloc[-1])
            a = atr(bars, 20)
            if a is None or a != a or a <= 0:
                continue
            if close > up:
                direction, dist = 'LONG', (close - up) / a
            elif close < lo:
                direction, dist = 'SHORT', (lo - close) / a
            else:
                continue
            ranked.append((dist, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        signals: List[Signal] = []
        keep = ranked[:self.MAX_SIGNALS]
        for dist, t, direction, close, bars in keep:
            # House brackets (same as every candidate): regime-scaled ATR stop + 5/10/20% targets.
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            conf = 'HIGH' if dist >= 1.0 else 'MED' if dist >= 0.3 else 'LOW'
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0/max(len(keep),1))*0.18*scale, 4),
                confidence=conf,
                signal_params={'channel_length': n, 'breakout_atr': round(float(dist),3),
                               'regime': regime_state, 'source': 'oxfordstrat:donchian-channel-2'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
```

- [ ] **Step 4: Run both tests — expect PASS**

Run: `cd /root/openclaw && python3 -m pytest tests/test_oxf_donchian_breakout.py tests/test_oxf_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/implementations/oxf_donchian_breakout.py tests/test_oxf_donchian_breakout.py tests/test_oxf_contract.py
git commit -m "feat(strategies): oxf_donchian_breakout + generic oxf contract test"
```

### Task 6: `oxf_nr7` (stop-entry → confirmed-breakout adaptation)

**Files:**
- Create: `src/strategies/implementations/oxf_nr7.py`
- Test: `tests/test_oxf_nr7.py`

- [ ] **Step 1: Write the test**

```python
# tests/test_oxf_nr7.py
from strategies.implementations.oxf_nr7 import OxfNR7
def test_id_and_adaptation_note():
    s = OxfNR7()
    assert s.id == 'oxf_nr7'
    assert 'adaptation' in s.description.lower()  # honesty: documented as adaptation
```

- [ ] **Step 2: Run — expect fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_oxf_nr7.py -v`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement** (NR7 narrow-range; the Crabel rule is an intraday stop at Open+Stretch. Adaptation: on a confirmed NR7 day t whose CLOSE pierced Open±Stretch — i.e. the stop WOULD have triggered intraday — emit a directional signal; engine fills at close[t+1])

```python
"""NR7 narrow-range breakout (oxfordstrat.com/trading-strategies/nr7/).
DAILY-BAR ADAPTATION: the Crabel rule rests an intraday stop at Open±Stretch on
the bar after an NR7. The backtest engine fills at close[t+1] and cannot honor an
intraday stop, so we model a CONFIRMED breakout: on an NR7 bar t whose close
pierced Open±Stretch (the stop would have triggered intraday), emit a directional
signal; the engine enters at close[t+1]. This is a confirmed-breakout adaptation,
NOT a tick-exact replica of the intraday stop fill.
"""
from __future__ import annotations
import sys
from typing import List
from strategies.base import Signal
from strategies.oxford_crabel import OxfordBaseStrategy, is_nrn, avg_noise

__all__ = ['OxfNR7']


class OxfNR7(OxfordBaseStrategy):
    id                = 'oxf_nr7'
    name              = 'Oxford NR7 Confirmed Breakout (adaptation)'
    description       = 'NR7 narrow-range breakout on liquid ETFs — daily-bar confirmed-breakout adaptation (oxfordstrat nr7). close[t+1] fill.'
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    MAX_SIGNALS       = 25

    def default_parameters(self) -> dict:
        return {'nr_length': 7, 'stretch_lookback': 10, 'stretch_mult': 1.0}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        p = self.parameters
        nrlen = int(p['nr_length'])
        ohlc = self.basket_ohlc(prices)  # ignore `universe` arg — iterate self-loaded basket
        ranked = []
        for t, bars in ohlc.items():
            if len(bars) < nrlen + 2:
                continue
            if not is_nrn(bars, nrlen):
                continue
            stretch = avg_noise(bars, int(p['stretch_lookback'])) * float(p['stretch_mult'])
            if stretch != stretch or stretch <= 0:
                continue
            today = bars.iloc[-1]
            up_trig = float(today['open']) + stretch
            dn_trig = float(today['open']) - stretch
            close = float(today['close'])
            # Confirmed intraday trigger: the day's high/low pierced the stop level.
            if float(today['high']) >= up_trig and close >= float(today['open']):
                direction, edge = 'LONG', (float(today['high']) - up_trig) / stretch
            elif float(today['low']) <= dn_trig and close <= float(today['open']):
                direction, edge = 'SHORT', (dn_trig - float(today['low'])) / stretch
            else:
                continue
            ranked.append((edge, t, direction, close, bars))
        ranked.sort(reverse=True)
        scale = self.position_scale(regime_state)
        keep = ranked[:self.MAX_SIGNALS]
        signals: List[Signal] = []
        for edge, t, direction, close, bars in keep:
            st = self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction=direction, entry_price=close,
                stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'],
                position_size_pct=round((1.0/max(len(keep),1))*0.18*scale, 4),
                confidence='MED',
                signal_params={'nr_length': nrlen, 'trigger_edge': round(float(edge),3),
                               'regime': regime_state, 'source': 'oxfordstrat:nr7',
                               'note': 'confirmed-breakout daily-bar adaptation'},
            ))
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
```

- [ ] **Step 4: Run the strategy test + the generic contract (now covers 2 strategies)**

Run: `cd /root/openclaw && python3 -m pytest tests/test_oxf_nr7.py tests/test_oxf_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/implementations/oxf_nr7.py tests/test_oxf_nr7.py
git commit -m "feat(strategies): oxf_nr7 confirmed-breakout adaptation"
```

### Task 7: Backtest + register the slice, inspect trades, confirm page render

**Files:**
- Create: `scripts/register_oxford_strategy.py` (manifest + registry upsert; reused for all 30)

- [ ] **Step 1: Write the registration helper**

```python
#!/usr/bin/env python3
"""Register an oxf_* strategy as a research candidate: manifest state=candidate +
strategy_registry row (pending_approval). Idempotent. Usage:
  python3 scripts/register_oxford_strategy.py <strategy_id> <ClassName> "<name>" "<description>"
"""
import os, sys, json, psycopg2
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from strategies.lifecycle import LifecycleStateMachine, StrategyState  # noqa

def main():
    sid, cls, name, desc = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    root = os.path.join(os.path.dirname(__file__), '..')
    manifest_path = os.path.join(root, 'src', 'strategies', 'manifest.json')
    lsm = LifecycleStateMachine.load(manifest_path) if hasattr(LifecycleStateMachine,'load') \
          else LifecycleStateMachine(manifest_path)
    # Register at candidate (no-op if present). Confirm the exact register()
    # signature in src/strategies/lifecycle.py:657 before running.
    if sid not in lsm.strategies:
        lsm.register(sid, initial_state=StrategyState.CANDIDATE, metadata={
            'canonical_file': f'{sid}.py', 'class': cls, 'description': desc,
            'eligible_regimes': ['LOW_VOL','TRANSITIONING']})
        lsm.save_manifest(manifest_path)
        print(f'manifest: registered {sid} as candidate')
    else:
        print(f'manifest: {sid} already present ({lsm.strategies[sid].state})')
    # strategy_registry row (pending_approval) so the page join + status are present.
    c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
    cur.execute("""
        INSERT INTO strategy_registry (id, name, description, tier, status,
            implementation_path, parameters, regime_conditions, universe)
        VALUES (%s,%s,%s,%s,'pending_approval',%s,'{}'::jsonb,'{}'::jsonb,%s)
        ON CONFLICT (id) DO NOTHING
    """, (sid, name, desc, 3, f'src/strategies/implementations/{sid}.py',
          ['SPY']))  # universe[] documentation only; strategy filters internally
    c.commit(); print('registry:', cur.rowcount, 'row(s) upserted'); c.close()

if __name__ == '__main__':
    main()
```

- [ ] **Step 2: Backtest both slice strategies (sequential, nice)**

Run:
```bash
cd /root/openclaw && export POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-)
for s in oxf_donchian_breakout oxf_nr7; do
  echo "=== $s ==="; nice -n 19 python3 -m backtest.unified_backtest \
    --strategy-file src/strategies/implementations/$s.py
done
```
Expected: each prints `wrote run_id=... total_sharpe=... trades=N` with N reasonably > 0 (basket has ~40 tradeable ETFs over ~10y). Watch RSS in a second shell (`while true; do ps -o rss= -C python3 | sort -n | tail -1; sleep 5; done`) — confirm it stays well under ~6GB.

- [ ] **Step 3: Inspect `strategy_backtest_trades` — confirm entries/exits are sane**

Run:
```bash
cd /root/openclaw && POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-) python3 -c "
import os, psycopg2
c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
for s in ('oxf_donchian_breakout','oxf_nr7'):
    cur.execute(\"\"\"SELECT ticker,direction,entry_date,entry_price,exit_date,exit_price,exit_reason,pnl_pct
        FROM strategy_backtest_trades t JOIN strategy_backtest_runs r ON t.run_id=r.run_id
        WHERE r.primary_window AND t.strategy_id=%s ORDER BY entry_date LIMIT 5\"\"\",(s,))
    print('---',s,'---'); [print(r) for r in cur.fetchall()]
    cur.execute(\"\"\"SELECT total_sharpe,total_sortino,total_calmar,total_trades,total_avg_pnl_pct
        FROM strategy_backtest_runs WHERE primary_window AND strategy_id=%s\"\"\",(s,))
    print('metrics:', cur.fetchone())
c.close()"
```
Expected: trades only on basket ETFs; entry_price = a real close; exit_reason ∈ {stop,target,max_hold,end_of_data}; **total_sortino/total_calmar are non-NULL** (proves Phase 0 wiring). If trades land on non-basket tickers or metrics are NULL, STOP and fix before fan-out.

- [ ] **Step 3b: Fidelity check — exit_reason distribution + holding period** (advisor flag: a fixed target can define the strategy instead of the rule)

Run:
```bash
cd /root/openclaw && POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-) python3 -c "
import os, psycopg2
c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
for s in ('oxf_donchian_breakout','oxf_nr7'):
    cur.execute(\"\"\"SELECT exit_reason, COUNT(*), ROUND(AVG(holding_days),1)
        FROM strategy_backtest_trades t JOIN strategy_backtest_runs r ON t.run_id=r.run_id
        WHERE r.primary_window AND t.strategy_id=%s GROUP BY exit_reason ORDER BY 2 DESC\"\"\",(s,))
    print('---',s,'---'); [print(r) for r in cur.fetchall()]
c.close()"
```
Expected: a MIX of exit reasons. If `target` dominates (>~70%) at very low `avg holding_days`, the 5% house target is truncating the trend edge — note it for the operator; the metrics are still the house-standard view every candidate gets (max_hold=21 is universal), so this is comparable, not wrong. Record the distribution in the slice report.

- [ ] **Step 4: Register both + confirm they render on the page**

Run:
```bash
cd /root/openclaw && export POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-)
python3 scripts/register_oxford_strategy.py oxf_donchian_breakout OxfDonchianBreakout "Oxford Donchian Channel Breakout" "Donchian breakout on liquid ETFs"
python3 scripts/register_oxford_strategy.py oxf_nr7 OxfNR7 "Oxford NR7 Confirmed Breakout" "NR7 daily-bar adaptation on liquid ETFs"
# Restart johnbot to load server.js changes (root user-systemd):
systemctl --user restart johnbot.service 2>/dev/null || systemctl restart johnbot.service
sleep 5
curl -s localhost:3000/api/strategies | python3 -c "
import sys,json; d=json.load(sys.stdin); rows=d if isinstance(d,list) else d.get('strategies',d)
oxf=[r for r in rows if str(r.get('id','')).startswith('oxf_')]
[print(r['id'], 'sharpe=',r.get('backtest_sharpe'),'sortino=',r.get('backtest_sortino'),'calmar=',r.get('backtest_calmar'),'trades=',r.get('backtest_trade_count')) for r in oxf]"
```
Expected: both `oxf_*` rows present with non-null sharpe/sortino/calmar/trades. **This validates fill-model handling, OHLC self-load, the metrics pipeline, registration, and page render together.**

- [ ] **Step 5: Commit**

```bash
git add scripts/register_oxford_strategy.py
git commit -m "feat(strategies): oxford registration helper + slice validated end-to-end"
```

> **Checkpoint:** Confirm with the operator that the slice looks right (trades sane, metrics populated, page renders) before fanning out the remaining 28.

---

## Phase 3 — Fan-out the remaining 28 strategies

Each of the 28 below is built by the SAME template as the slice: subclass `OxfordBaseStrategy`, declare attrs + `default_parameters`, implement `generate_signals` over `self.basket_ohlc(prices)` (**never reference the `universe` arg** — the universe-independence contract test enforces this), emit `Signal`s with the house `self.compute_stops_and_targets(bars['close'], direction, close, regime_state=regime_state)`, cap at `MAX_SIGNALS`, must pass `tests/test_oxf_contract.py`. **Faithful** rules evaluate a condition at close[t]. **Adaptation** rules check the intraday trigger against signal-day OHLC (like `oxf_nr7`) and MUST say "adaptation" in their `description`. Add any missing indicator to `oxford_crabel.py` with a unit test first (TDD). Each strategy: write test → implement → contract+unit green → commit. Backtest + register happen in the batch driver (Task 9).

> **Indicator fidelity (important):** FRAMA, Kaufman AMA, Vortex, Aroon, swing-pivot detection, and TD Sequential setup/countdown have SPECIFIC forms that are easy to get subtly wrong. Each fan-out worker MUST fetch its exact formula from the cited oxfordstrat.com page (firecrawl-scrape / WebFetch), not a generic reference, and the golden unit test's expected values must be derived from THAT formula. SMA/EMA/RSI/MACD/ROC/Bollinger/Keltner/Heikin-Ashi are standard.

For each, `active_in_regimes`: trend/breakout → `['LOW_VOL','TRANSITIONING']`; mean-reversion → `['TRANSITIONING','HIGH_VOL']`.

**Faithful (condition at close[t]):**

| # | id / Class | Oxford URL slug | Entry rule (long; short = mirror) | Key params | New indicator |
|---|---|---|---|---|---|
| 1 | `oxf_sma_filter` / OxfSmaFilter | simple-moving-average | close > SMA(slow) and SMA(fast) > SMA(slow) | slow=250, fast=63 | sma ✓ |
| 2 | `oxf_adaptive_ma` / OxfAdaptiveMa | adaptive-moving-average-1 | AMA rising and (AMA − min(AMA,n)) > filter·ATR | er_len=20, fast=2, slow=30, filter=0.01 | kaufman_ama |
| 3 | `oxf_frama` / OxfFrama | fractal-adaptive-moving-average | close > FRAMA + band·ATR | frama_len=40, band=1.0 | frama |
| 4 | `oxf_hull_ma` / OxfHullMa | hull-moving-average | close > HMA(slow) and HMA(fast) > HMA(slow) | slow=400, fast=100 | hma |
| 5 | `oxf_zero_lag_ma` / OxfZeroLagMa | zero-lag-moving-average | ZLMA > EMA and (100·err/ATR) > thr | lookback=200, thr=50 | zlma, ema |
| 6 | `oxf_linreg_slope` / OxfLinregSlope | linear-regression | LRS(n) > 0 and LRS(n2) > 0 | lb=100, lb2=50 | linreg_slope |
| 7 | `oxf_macd_zero` / OxfMacdZero | macd-part-1 | MACD line > 0 (12/26) | ema_fast=12, ema_slow=26 | macd |
| 8 | `oxf_rsi2_meanrev` / OxfRsi2Meanrev | relative-strength-index-1 | close > SMA(200) and RSI(2) < 5 → long (mean-rev); exit close > SMA(5) | rsi_len=2, trend=200, thr=5 | rsi_wilder ✓ |
| 9 | `oxf_price_momentum` / OxfPriceMomentum | price-momentum-model | mom(n1) > 0 and mom(n2) > 0 (mom=close−close[lag]) | n1=100, n2=50 | (close math) |
| 10 | `oxf_dual_momentum_roc` / OxfDualMomentumRoc | dual-momentum-rate-of-change | ROC(n1) > 0 and ROC(n2)=0.5·n1 > 0 | n1=100 | roc |
| 11 | `oxf_vortex` / OxfVortex | vortex-indicator-1 | +VI(n) > −VI(n) (crossover) | lb=110 | vortex |
| 12 | `oxf_aroon_breakout` / OxfAroonBreakout | aroon-indicator-breakout-1 | AroonUp(n) > AroonDown(n) and AroonUp ≥ min | aroon_len=25, min=70 | aroon |
| 13 | `oxf_bollinger_momentum` / OxfBollingerMomentum | bollinger-bands | close > upper Bollinger (MA + k·σ) | ma=80, k=2.0 | bollinger |
| 14 | `oxf_keltner` / OxfKeltner | keltner-channels-1 | close > TypicalPrice MA + mult·range(ATR) | lb=10, mult=1.0 | keltner (atr ✓) |
| 15 | `oxf_heikin_ashi` / OxfHeikinAshi | heikin-ashi-1 | HA close > HA open for the latest bar (trend up) | lb=20 | heikin_ashi |
| 16 | `oxf_livermore` / OxfLivermore | livermore-system-1 | close penetrates the 2 most-recent swing-pivot highs by ≥ noise | swing_filter=4.0, penet=0.5 | swing_pivots |
| 17 | `oxf_dow_theory` / OxfDowTheory | dow-theory-trend | close > most-recent swing-pivot high (higher-high structure) | pivot_size=10 | swing_pivots |
| 18 | `oxf_wyckoff_meanrev` / OxfWyckoffMeanrev | richard-wyckoff-mean-reversion-1 | after a down leg, close reclaims entry_level off swing low (mean-rev) | pattern_size=10, entry_idx=0.5 | swing_pivots |
| 19 | `oxf_td_sequential` / OxfTdSequential | td-sequential-1 | TD setup(9) complete and close > close[i-4] | setup=9 | td_setup_count |

**Confirmed-breakout adaptations (intraday trigger checked on day-t OHLC; description MUST say "adaptation"):**

| # | id / Class | Oxford URL slug | Trigger (long; short = mirror), confirmed on day t | Key params | New indicator |
|---|---|---|---|---|---|
| 20 | `oxf_orbp_momentum` / OxfOrbpMomentum | orbp-trend | trend-up (MA filter) AND high ≥ open+stretch AND close ≥ open | filter_lb=20, stretch_mult=1.0 | avg_noise ✓ |
| 21 | `oxf_smash_day_b` / OxfSmashDayB | smash-day-pattern-b1 | close[i-1] < low[i-2] then high ≥ prior high (reversal up) | trend_idx=40 | (OHLC math) |
| 22 | `oxf_gap_a` / OxfGapA | gap-pattern | full gap (low > prior high) in trend direction | filter_lb=20 | gap_dir ✓ |
| 23 | `oxf_greatest_swing_value` / OxfGreatestSwingValue | greatest-swing-value-trend | trend-up AND high ≥ open + GSV(avg of down-day swings)·mult | gsv_lb=10, mult=2.0 | gsv |
| 24 | `oxf_welles_wilder_breakout` / OxfWellesWilderBreakout | welles-wilder-1 | close > SIC + ARC where ARC=ATR·const (volatility breakout) | lb=20, const=3.0 | atr ✓ |
| 25 | `oxf_hook` / OxfHook | pattern-hook | Crabel hook (open beyond prior extreme, narrowing range) AND high ≥ open+stretch | stretch_mult=2.0 | avg_noise ✓ |
| 26 | `oxf_bull_oops` / OxfBullOops | bull-oops-pattern | open < prior low AND high ≥ prior low (recovers → buy stop at prior low triggered) | atr_idx=1.0 | (OHLC math) |
| 27 | `oxf_false_breakout` / OxfFalseBreakout | false-breakout-1 | prior N-day high broken ≥3 bars ago, then close back inside (failed breakout fade) | ch1=40, ch2=10 | donchian_prev ✓ |
| 28 | `oxf_ross_hook` / OxfRossHook | ross-hook-filter-2 | after a 1-2-3 trend break, hook forms; high ≥ hook extreme (breakout) | min_setup=10, max_hook=1.0 | swing_pivots |

> Indicator notes: `kaufman_ama`, `frama`, `hma`, `zlma`, `ema`, `linreg_slope`, `macd`, `roc`, `vortex`, `aroon`, `bollinger`, `keltner`, `heikin_ashi`, `swing_pivots`, `td_setup_count`, `gsv` are added to `oxford_crabel.py` as each strategy needs them — each with a unit test in `tests/test_oxford_crabel.py` first (TDD). Formulas are standard; cite the Oxford slug in the docstring. If a rule proves unfit for daily bars during implementation, drop it and log the substitution (target stays ~30; the spec permits ±1-2).

### Task 8 (×28): Implement each strategy — repeat the slice template

For each row above, in order:
- [ ] Write `tests/test_<id>.py` (id + `instrument_class=='etp'` + for adaptations, `'adaptation' in description.lower()`); run → fail.
- [ ] If it needs a new indicator: add a failing unit test in `tests/test_oxford_crabel.py`, implement the indicator in `oxford_crabel.py`, green.
- [ ] Implement `src/strategies/implementations/<id>.py` from the template (copy `oxf_donchian_breakout.py` for faithful, `oxf_nr7.py` for adaptation); set attrs/params/rule per the table.
- [ ] Run `python3 -m pytest tests/test_<id>.py tests/test_oxf_contract.py -v` → PASS (contract now covers the new class automatically).
- [ ] Commit: `git add ... && git commit -m "feat(strategies): <id> (oxfordstrat)"`.

> **Parallelization (ultracode):** the 28 files are independent — code-gen may fan out across agents/worktrees. But `oxford_crabel.py` and `tests/test_oxford_crabel.py` are shared: serialize indicator additions (or have one agent own the helper and others depend on it) to avoid merge conflicts. The contract test auto-discovers all `oxf_*` classes, so it must pass on the merged tree.

### Task 9: Batch backtest + register all 30 (sequential)

**Files:**
- Create: `scripts/build_all_oxford.sh`

- [ ] **Step 1: Write the driver** (sequential — 2-core/OOM constraint)

```bash
#!/usr/bin/env bash
# Backtest + register every oxf_* strategy. Sequential, nice. Resumable.
set -uo pipefail
cd "$(dirname "$0")/.."
export POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-)
for f in src/strategies/implementations/oxf_*.py; do
  sid=$(basename "$f" .py)
  echo "=== backtest $sid ==="
  nice -n 19 python3 -m backtest.unified_backtest --strategy-file "$f" \
    || { echo "FAILED: $sid"; continue; }
done
echo "=== registration (uses each class's id/name/description) ==="
nice -n 19 python3 scripts/register_all_oxford.py
```

- [ ] **Step 2: Write `scripts/register_all_oxford.py`** — discovers every `oxf_*` class and calls the same manifest+registry logic as `register_oxford_strategy.py`, reading `id`/`name`/`description`/`__class__.__name__` off each class (no hand-typed args). Reuse the body of `register_oxford_strategy.py:main`, looping over the auto-discovered classes from `tests/test_oxf_contract.py:_oxf_classes` (move that discovery helper into `oxford_crabel.py` as `discover_oxford_classes()` and import it in both places — DRY).

- [ ] **Step 3: Run it** (after all 30 files + Phase 0 are merged)

Run: `cd /root/openclaw && bash scripts/build_all_oxford.sh 2>&1 | tee /tmp/oxford_build.log`
Expected: 30 `wrote run_id=...` lines; registration reports 30 manifest + 30 registry rows. Watch RSS; if any backtest OOMs, re-run that one alone (the loop is resumable — re-running re-backtests idempotently, demoting prior primary_window rows).

- [ ] **Step 4: Commit**

```bash
git add scripts/build_all_oxford.sh scripts/register_all_oxford.py
git commit -m "feat(strategies): batch backtest + register all oxford candidates"
```

### Task 10: Final verification

- [ ] **Step 1: All 30 render with full metrics, sorted by Sharpe**

Run:
```bash
cd /root/openclaw && (systemctl --user restart johnbot.service 2>/dev/null || systemctl restart johnbot.service); sleep 6
curl -s localhost:3000/api/strategies | python3 -c "
import sys,json; d=json.load(sys.stdin); rows=d if isinstance(d,list) else d.get('strategies',d)
oxf=[r for r in rows if str(r.get('id','')).startswith('oxf_')]
print('oxf candidates:', len(oxf))
oxf.sort(key=lambda r:(r.get('backtest_sharpe') or -9))
for r in oxf: print(f\"  {r['id']:32s} sharpe={r.get('backtest_sharpe')} sortino={r.get('backtest_sortino')} calmar={r.get('backtest_calmar')} trades={r.get('backtest_trade_count')} state={r.get('state')}\")
assert len(oxf) >= 28, 'expected ~30 candidates'
assert all(r.get('state')=='candidate' for r in oxf), 'all must be candidates (none live)'
assert all(r.get('backtest_trade_count') is not None for r in oxf), 'all must have metrics'
print('OK')"
```
Expected: ~30 rows, all `state=candidate`, all with non-null sharpe/sortino/calmar/trades, printed Sharpe-ascending. None in `_IMPL_MAP` / none live.

- [ ] **Step 2: Regression — nothing else broke**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_t_plus_1.py tests/test_oxford_crabel.py tests/test_oxf_contract.py -v`
Expected: PASS.

- [ ] **Step 3: Confirm `_IMPL_MAP` untouched** (candidates must not be executable)

Run: `cd /root/openclaw && grep -c "oxf_" src/strategies/registry.py`
Expected: `0`.

- [ ] **Step 4: Update CLAUDE.md Recent Changes + memory** with a one-line summary (ids, basket, migration 135, candidate-only).

- [ ] **Step 5: Commit + report to operator** (do NOT promote anything; surface the ranked candidate list on #general / the dashboard for the operator to triage). **Flag the recurring cost:** `unified_backtest --all-live` (the weekend `refresh_backtests.sh`) re-backtests live **+ candidate + staging** strategies, so these ~30 candidates add ~30 full-history backtests to every weekend refresh on the 2-core/8GB/no-swap box. Per-strategy chunking should prevent a worse OOM, but the operator is implicitly signing up for the added window time — confirm that's acceptable, or gate the Oxford candidates out of the weekend refresh until triaged.

---

## Self-review checklist (completed during authoring)

- **Spec coverage:** curation (Phase 2-3 list = the spec's 30), ETF basket (Task 4 constant, verified), close[t+1]/stop-entry adaptation (Task 6 + Phase 3 adaptation column), full metrics incl. sortino/calmar (Phase 0), vertical-slice-first (Task 7 checkpoint), sequential/OOM (Task 9), sort-by-Sharpe (Task 3), candidates-only/no `_IMPL_MAP` (Task 10 step 3). All covered.
- **Placeholders:** indicator formulas for the 28 are specified by name + standard definition + Oxford slug; each is TDD'd into the helper. The one deferred detail (exact `register()` signature) is an existing function pointed to at `lifecycle.py:657` — confirmed to exist, not invented.
- **Type consistency:** `OxfordBaseStrategy`, `basket_ohlc`, `donchian_prev`, `avg_noise`, `is_nrn`, `gap_dir` names match across Tasks 4-7 and the fan-out table. Brackets use the house `BaseStrategy.compute_stops_and_targets` everywhere (no custom bracket helper). No strategy references the `universe` arg (enforced by `test_does_not_depend_on_universe_arg`).
