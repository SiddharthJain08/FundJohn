# Backtest-Sourced Strategy Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every per-strategy dashboard metric backtest-derived (Sharpe, effective Sharpe, closed/win/ARR/ADR/ACT, OUE, equity-vs-SP500 chart, closed-per-regime bar), leaving only `last_signal` and `status` live, and remove open-position metrics.

**Architecture:** Precompute a per-strategy `strategy_backtest_panel` row (effective Sharpe, GBM-σ OUE counts, weekly-downsampled equity curve vs SP500 with per-point regime) via a new Python builder over `strategy_backtest_trades` + `prices.parquet`. The Express dashboard (`server.js`) reads backtest tables + the panel; the frontend renders backtest columns and two new Chart.js charts.

**Tech Stack:** Python 3.13 (pandas/numpy/psycopg2), Postgres, Node/Express (`pg`), Chart.js 4.4.0 (CDN), pytest.

**Reference spec:** `docs/superpowers/specs/2026-05-29-dashboard-backtest-metrics-design.md`

**Grounding facts (verified 2026-05-29):**
- Highest migration = `123_*`; new = `124_*`. Runner: `src/database/postgres.js:migrate()` reads `migrations/*.sql` sorted — make the migration idempotent (`IF NOT EXISTS`).
- `src/backtest/unified_backtest.py`: CLI `--strategy-id` / `--all-live`; `run_backtest(strategy_id, ...)` persists `strategy_backtest_runs/_regimes/_trades` and demotes prior `primary_window`; reusable `_portfolio_daily_returns(trades) -> (np.ndarray, list[pd.Timestamp])` (line 273); `PRICES_PARQUET = ROOT/'data'/'master'/'prices.parquet'` (line 71).
- `src/execution/oue_classifier.py:classify(realized_pct, days_held, ev_gbm, hv21, sigma_gate=2.0) -> (kind, sigma_delta)`.
- `src/strategies/historical_regimes.py:regime_series(dates) -> pd.Series` (regime label per date, ffilled).
- `strategy_backtest_trades` cols: `run_id, trade_seq, strategy_id, ticker, direction, entry_date, entry_price, exit_date, exit_price, exit_reason, pnl_pct, holding_days, entry_regime, signal_stop, signal_target`.
- `strategy_backtest_runs` cols incl: `total_sharpe, total_return_pct, total_max_dd_pct, total_trades, total_hit_rate, avg_holding_days, primary_window, run_at, run_id`.
- `strategy_backtest_regimes` cols: `run_id, regime_state, trade_count, sharpe, max_dd_pct, return_pct, hit_rate, avg_pnl_pct, avg_holding_days`.
- `pipeline_config.sigma_gate = 2.0`. `^GSPC` in prices.parquet (2548 daily bars).
- Dashboard DB helper: `const { query: dbQuery } = require('../../database/postgres')`; pattern `(await dbQuery(sql, params)).rows`.
- Frontend is inline template strings in `server.js`; Chart.js + a registered `regimeBands` plugin (colors `REGIME_BAND_COLORS`) already exist and are reusable for the equity chart.

---

## Task 0: Populate backtest trades for the 3 uncovered live strategies

**Files:** none (data/ops step).

- [ ] **Step 1: Confirm the 3 are still uncovered**

Run:
```bash
cd /root/openclaw && export POSTGRES_URI=$(grep -E "^POSTGRES_URI=" .env | cut -d= -f2-)
python3 - <<'PY'
import os,psycopg2
c=psycopg2.connect(os.environ['POSTGRES_URI']).cursor()
for s in ('S_HV16_gex_regime','S_idiosyncratic_vol_puzzle','S_price_path_convexity'):
    c.execute("SELECT count(*) FROM strategy_backtest_trades WHERE strategy_id=%s",(s,))
    print(s, c.fetchone()[0])
PY
```
Expected: each prints `0` (or a small number if a backtest already ran).

- [ ] **Step 2: Run the backtests**

Run (each writes `strategy_backtest_runs/_regimes/_trades`, `primary_window=TRUE`):
```bash
cd /root/openclaw && export $(grep -vE '^\s*#' .env | grep -E '^[A-Z_]+=' | xargs -d '\n')
for s in S_HV16_gex_regime S_idiosyncratic_vol_puzzle S_price_path_convexity; do
  PYTHONPATH=src python3 -m backtest.unified_backtest --strategy-id "$s" || echo "FAILED $s"
done
```
Expected: each prints a run summary (sharpe/trades) and exits 0. If a strategy legitimately produces zero trades, note it — its panel will be a graceful placeholder.

- [ ] **Step 3: Verify coverage = 51/51 (or note legit-empty)**

Run the Step-1 snippet again. Expected: non-zero trade counts (or a documented zero for a strategy that generates no signals in-sample).

- [ ] **Step 4: Commit (no code; record the ops note)**

```bash
git commit --allow-empty -m "chore(backtest): populate trades for 3 uncovered live strategies (dashboard backtest panel)"
```

---

## Task 1: Migration — `strategy_backtest_panel`

**Files:**
- Create: `src/database/migrations/124_strategy_backtest_panel.sql`
- Test: `tests/test_migration_124.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_migration_124.py
import os, psycopg2, pytest
DSN = os.environ.get("POSTGRES_URI")

@pytest.mark.integration
def test_panel_table_and_columns_exist():
    assert DSN, "POSTGRES_URI required"
    conn = psycopg2.connect(DSN); cur = conn.cursor()
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='strategy_backtest_panel'""")
    cols = {r[0] for r in cur.fetchall()}
    conn.close()
    expected = {'strategy_id','run_id','effective_sharpe','cadence_days',
                'oue_over','oue_under','oue_expected','oue_by_regime',
                'oue_sigma_gate','equity_curve','n_trades','computed_at'}
    assert expected <= cols, f"missing: {expected - cols}"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd /root/openclaw && POSTGRES_URI=$(grep -E "^POSTGRES_URI=" .env|cut -d= -f2-) python3 -m pytest tests/test_migration_124.py -q`
Expected: FAIL (table does not exist).

- [ ] **Step 3: Write the migration**

```sql
-- src/database/migrations/124_strategy_backtest_panel.sql
-- Per-strategy precomputed backtest dashboard panel (additive; never-delete-safe).
CREATE TABLE IF NOT EXISTS strategy_backtest_panel (
    strategy_id        TEXT PRIMARY KEY,
    run_id             UUID,
    effective_sharpe   DOUBLE PRECISION,
    cadence_days       DOUBLE PRECISION,
    oue_over           INTEGER,
    oue_under          INTEGER,
    oue_expected       INTEGER,
    oue_by_regime      JSONB,
    oue_sigma_gate     DOUBLE PRECISION,
    equity_curve       JSONB,
    n_trades           INTEGER,
    computed_at        TIMESTAMPTZ DEFAULT NOW()
);
```

- [ ] **Step 4: Apply the migration**

Run: `cd /root/openclaw && node -e "require('./src/database/postgres').migrate().then(()=>process.exit(0))"`
Expected: `[postgres] All migrations complete.`

- [ ] **Step 5: Run the test, verify it passes**

Run: `POSTGRES_URI=$(grep -E "^POSTGRES_URI=" .env|cut -d= -f2-) python3 -m pytest tests/test_migration_124.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/database/migrations/124_strategy_backtest_panel.sql tests/test_migration_124.py
git commit -m "feat(db): migration 124 strategy_backtest_panel"
```

---

## Task 2: Builder pure function — GBM-σ OUE over backtest trades

**Files:**
- Create: `src/backtest/backtest_panel.py`
- Test: `tests/test_backtest_panel.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_panel.py
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backtest.backtest_panel import classify_trades_oue


def test_oue_counts_invariant_and_classification():
    # hv21 lookup: ticker AAA, flat vol 0.20 annualized.
    def hv(ticker, entry_date):
        return 0.20
    # holding 21 trading days -> sigma_holding = 0.20*sqrt(21/252)=~0.0577
    # +0.20 realized -> sigma_delta ~ +3.46 -> over; -0.20 -> under; +0.02 -> expected
    trades = [
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct': 0.20,'holding_days':21,'entry_regime':'LOW_VOL'},
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct':-0.20,'holding_days':21,'entry_regime':'LOW_VOL'},
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct': 0.02,'holding_days':21,'entry_regime':'CRISIS'},
    ]
    overall, by_regime = classify_trades_oue(trades, hv, sigma_gate=2.0)
    assert overall == {'over':1,'under':1,'expected':1}
    assert sum(overall.values()) == len(trades)              # invariant
    assert by_regime['LOW_VOL'] == {'over':1,'under':1,'expected':0}
    assert by_regime['CRISIS']  == {'over':0,'under':0,'expected':1}


def test_oue_missing_hv_falls_back_to_expected():
    def hv(ticker, entry_date):
        return None
    trades = [{'ticker':'X','entry_date':'2025-01-02','pnl_pct':0.5,'holding_days':10,'entry_regime':'HIGH_VOL'}]
    overall, by_regime = classify_trades_oue(trades, hv, sigma_gate=2.0)
    assert overall == {'over':0,'under':0,'expected':1}
```

- [ ] **Step 2: Run it, verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_panel.py -q`
Expected: FAIL (`ModuleNotFoundError`/`classify_trades_oue` undefined).

- [ ] **Step 3: Implement the module header + OUE classifier**

```python
# src/backtest/backtest_panel.py
"""Precompute the per-strategy BACKTEST dashboard panel:
effective Sharpe, GBM-σ OUE counts (overall + per regime), and a
weekly-downsampled equity curve vs SP500 with per-point regime.

This is the dashboard's backtest panel — SEPARATE from the live
#trade-reports OUE digest. Persisted to strategy_backtest_panel.
"""
from __future__ import annotations
import json
import math
import os
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from execution.oue_classifier import classify          # noqa: E402
from strategies import historical_regimes               # noqa: E402

PRICES_PARQUET = ROOT / 'data' / 'master' / 'prices.parquet'


def classify_trades_oue(trades: list[dict],
                        hv21_for: Callable[[str, str], Optional[float]],
                        sigma_gate: float = 2.0) -> tuple[dict, dict]:
    """Classify each backtest trade Over/Under/Expected vs a zero-drift GBM
    expectation, reusing oue_classifier.classify (ev_gbm=0). Returns
    (overall_counts, by_regime_counts). Trades with no computable hv21 →
    'expected' (mirrors the live classifier's missing-EV fallback), so
    O+U+E == len(trades) always holds."""
    overall = {'over': 0, 'under': 0, 'expected': 0}
    by_regime: dict[str, dict] = {}
    for t in trades:
        regime = t.get('entry_regime') or 'UNKNOWN'
        slot = by_regime.setdefault(regime, {'over': 0, 'under': 0, 'expected': 0})
        hv = hv21_for(t['ticker'], str(t['entry_date']))
        if hv is None or not math.isfinite(hv) or hv <= 0:
            kind = 'expected'
        else:
            kind, _ = classify(float(t['pnl_pct']), int(t.get('holding_days') or 1),
                               ev_gbm=0.0, hv21=float(hv), sigma_gate=sigma_gate)
        overall[kind] += 1
        slot[kind] += 1
    return overall, by_regime
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_panel.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/backtest_panel.py tests/test_backtest_panel.py
git commit -m "feat(backtest): GBM-σ OUE classifier over backtest trades"
```

---

## Task 3: Builder pure functions — effective Sharpe + equity curve

**Files:**
- Modify: `src/backtest/backtest_panel.py`
- Test: `tests/test_backtest_panel.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_backtest_panel.py
from backtest.backtest_panel import effective_sharpe, build_equity_curve


def test_effective_sharpe():
    assert math.isclose(effective_sharpe(2.0, 4.0), 1.0)      # 2 / sqrt(4)
    assert math.isclose(effective_sharpe(1.5, 0.0), 1.5)      # cadence floored to 1
    assert effective_sharpe(None, 4.0) is None


def test_build_equity_curve_normalizes_and_tags_regime():
    # 3 trades over distinct windows; benchmark flat -> spx_equity constant 1.0
    trades = [
        {'ticker':'AAA','entry_date':'2025-01-02','pnl_pct':0.10,'holding_days':5},
        {'ticker':'AAA','entry_date':'2025-02-02','pnl_pct':-0.05,'holding_days':5},
    ]
    bench = pd.Series([0.0]*400,
                      index=pd.date_range('2025-01-01', periods=400, freq='D'))
    def regime_for(dates):
        return pd.Series(['LOW_VOL']*len(list(dates)), index=list(dates))
    curve = build_equity_curve(trades, bench_daily_ret=bench,
                               regime_series_fn=regime_for, weekly=True)
    assert curve, "curve should be non-empty"
    assert curve[0]['strat_equity'] == 1.0          # normalized start
    assert all('date' in p and 'spx_equity' in p and 'regime' in p for p in curve)
    assert all(abs(p['spx_equity'] - 1.0) < 1e-9 for p in curve)  # flat benchmark
    assert len(curve) <= 60                           # weekly downsample of ~2 months
```

- [ ] **Step 2: Run, verify fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_panel.py -q`
Expected: FAIL (`effective_sharpe`/`build_equity_curve` undefined).

- [ ] **Step 3: Implement**

```python
# append to src/backtest/backtest_panel.py
from backtest.unified_backtest import _portfolio_daily_returns   # noqa: E402


def effective_sharpe(total_sharpe: Optional[float], cadence_days: Optional[float]) -> Optional[float]:
    """Sharpe / sqrt(cadence). cadence floored at 1 day."""
    if total_sharpe is None:
        return None
    return float(total_sharpe) / math.sqrt(max(1.0, float(cadence_days or 1.0)))


def build_equity_curve(trades: list[dict],
                       bench_daily_ret: pd.Series,
                       regime_series_fn=historical_regimes.regime_series,
                       weekly: bool = True) -> list[dict]:
    """Reconstruct the strategy's equity curve from backtest trades (reusing
    unified_backtest._portfolio_daily_returns), overlay the benchmark
    (^GSPC) normalized to the same start (1.0), tag each point with its
    regime, and downsample to weekly. Returns [{date, strat_equity,
    spx_equity, regime}, ...] (ascending date)."""
    daily_ret, dates = _portfolio_daily_returns(trades)
    if len(daily_ret) == 0:
        return []
    idx = pd.DatetimeIndex(dates)
    strat_eq = pd.Series(np.cumprod(1.0 + daily_ret), index=idx)
    # Benchmark cumulative over the same span, normalized to 1.0 at start.
    b = bench_daily_ret.reindex(pd.date_range(idx.min(), idx.max(), freq='D')).fillna(0.0)
    bench_eq_full = (1.0 + b).cumprod()
    bench_eq = bench_eq_full.reindex(idx, method='ffill')
    bench_eq = bench_eq / float(bench_eq.iloc[0])
    regimes = regime_series_fn(idx)
    regimes.index = idx
    frame = pd.DataFrame({'strat_equity': strat_eq,
                          'spx_equity': bench_eq.values,
                          'regime': regimes.values}, index=idx)
    if weekly:
        # Keep the last point of each ISO week (compact, ≤~520 pts for 10y).
        frame = frame.groupby(frame.index.to_period('W')).tail(1)
    out = []
    for ts, row in frame.iterrows():
        out.append({'date': ts.strftime('%Y-%m-%d'),
                    'strat_equity': round(float(row['strat_equity']), 6),
                    'spx_equity': round(float(row['spx_equity']), 6),
                    'regime': None if pd.isna(row['regime']) else str(row['regime'])})
    return out
```

- [ ] **Step 4: Run, verify pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_panel.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/backtest_panel.py tests/test_backtest_panel.py
git commit -m "feat(backtest): effective-sharpe + equity-curve builders"
```

---

## Task 4: Builder orchestration — `build_panel` + prices/hv21 + persist + CLI

**Files:**
- Modify: `src/backtest/backtest_panel.py`
- Test: `tests/test_backtest_panel.py`

- [ ] **Step 1: Write the failing test (pure hv21 lookup)**

```python
# append to tests/test_backtest_panel.py
from backtest.backtest_panel import build_hv21_lookup


def test_build_hv21_lookup_returns_callable_with_annualized_vol():
    # synthetic prices: ticker AAA random-walk-ish; expect a positive hv21 after 21+ days
    dates = pd.date_range('2025-01-01', periods=60, freq='B')
    px = pd.DataFrame({'ticker':'AAA','date':dates,
                       'close': 100*np.cumprod(1+np.linspace(0.001,0.002,60))})
    hv = build_hv21_lookup(px)
    v = hv('AAA', dates[40].strftime('%Y-%m-%d'))
    assert v is not None and v > 0
    assert hv('MISSING', dates[40].strftime('%Y-%m-%d')) is None
```

- [ ] **Step 2: Run, verify fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_panel.py::test_build_hv21_lookup_returns_callable_with_annualized_vol -q`
Expected: FAIL (`build_hv21_lookup` undefined).

- [ ] **Step 3: Implement hv21 lookup, build_panel, persist, CLI**

```python
# append to src/backtest/backtest_panel.py
TRADING_DAYS = 252


def build_hv21_lookup(prices: pd.DataFrame):
    """From a long prices frame [ticker, date, close], build a per-ticker
    21-day annualized realized-vol series and return hv21(ticker, date) →
    Optional[float] (asof nearest prior date)."""
    prices = prices[['ticker', 'date', 'close']].dropna()
    prices = prices.assign(date=pd.to_datetime(prices['date']))
    hv_by_ticker: dict[str, pd.Series] = {}
    for tkr, g in prices.sort_values('date').groupby('ticker'):
        s = g.set_index('date')['close'].astype(float)
        logret = np.log(s).diff()
        hv = logret.rolling(21).std() * math.sqrt(TRADING_DAYS)
        hv_by_ticker[tkr] = hv.dropna()

    def lookup(ticker: str, entry_date: str) -> Optional[float]:
        s = hv_by_ticker.get(ticker)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(entry_date)
        prior = s.loc[:ts]
        if prior.empty:
            return None
        v = float(prior.iloc[-1])
        return v if math.isfinite(v) and v > 0 else None
    return lookup


def _sigma_gate(cur) -> float:
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key='sigma_gate'")
        r = cur.fetchone()
        return float(r[0]) if r else 2.0
    except Exception:
        return 2.0


def _benchmark_daily_returns(prices: pd.DataFrame, ticker: str = '^GSPC') -> pd.Series:
    g = prices[prices['ticker'] == ticker][['date', 'close']].dropna()
    g = g.assign(date=pd.to_datetime(g['date'])).sort_values('date').set_index('date')
    return g['close'].astype(float).pct_change().fillna(0.0)


def build_panel(strategy_id: str, conn, prices: pd.DataFrame,
                hv21_for, bench_ret: pd.Series) -> Optional[dict]:
    """Compute the panel dict for one strategy from its primary_window run.
    Returns None if the strategy has no primary_window backtest trades."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT run_id, total_sharpe, avg_holding_days
              FROM strategy_backtest_runs
             WHERE strategy_id=%s AND primary_window=TRUE
             ORDER BY run_at DESC LIMIT 1
        """, (strategy_id,))
        run = cur.fetchone()
        if not run:
            return None
        cur.execute("""
            SELECT ticker, entry_date, pnl_pct, holding_days, entry_regime
              FROM strategy_backtest_trades WHERE run_id=%s ORDER BY exit_date
        """, (run['run_id'],))
        trades = [dict(r) for r in cur.fetchall()]
        gate = _sigma_gate(cur)
    if not trades:
        return None
    overall, by_regime = classify_trades_oue(trades, hv21_for, sigma_gate=gate)
    curve = build_equity_curve(trades, bench_ret, weekly=True)
    eff = effective_sharpe(run['total_sharpe'], run['avg_holding_days'])
    return {
        'strategy_id': strategy_id,
        'run_id': run['run_id'],
        'effective_sharpe': eff,
        'cadence_days': float(run['avg_holding_days'] or 1.0),
        'oue_over': overall['over'], 'oue_under': overall['under'],
        'oue_expected': overall['expected'], 'oue_by_regime': by_regime,
        'oue_sigma_gate': gate, 'equity_curve': curve, 'n_trades': len(trades),
    }


def persist_panel(conn, panel: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO strategy_backtest_panel
              (strategy_id, run_id, effective_sharpe, cadence_days,
               oue_over, oue_under, oue_expected, oue_by_regime,
               oue_sigma_gate, equity_curve, n_trades, computed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            ON CONFLICT (strategy_id) DO UPDATE SET
               run_id=EXCLUDED.run_id, effective_sharpe=EXCLUDED.effective_sharpe,
               cadence_days=EXCLUDED.cadence_days, oue_over=EXCLUDED.oue_over,
               oue_under=EXCLUDED.oue_under, oue_expected=EXCLUDED.oue_expected,
               oue_by_regime=EXCLUDED.oue_by_regime, oue_sigma_gate=EXCLUDED.oue_sigma_gate,
               equity_curve=EXCLUDED.equity_curve, n_trades=EXCLUDED.n_trades,
               computed_at=NOW()
        """, (panel['strategy_id'], panel['run_id'], panel['effective_sharpe'],
              panel['cadence_days'], panel['oue_over'], panel['oue_under'],
              panel['oue_expected'], json.dumps(panel['oue_by_regime']),
              panel['oue_sigma_gate'], json.dumps(panel['equity_curve']),
              panel['n_trades']))
    conn.commit()


def rebuild(strategy_id: Optional[str] = None) -> dict:
    """Build + persist panels. If strategy_id is None, rebuild all strategies
    that have a primary_window run."""
    prices = pd.read_parquet(PRICES_PARQUET, columns=['ticker', 'date', 'close'])
    hv21_for = build_hv21_lookup(prices)
    bench_ret = _benchmark_daily_returns(prices, '^GSPC')
    conn = psycopg2.connect(os.environ['POSTGRES_URI'])
    try:
        if strategy_id:
            sids = [strategy_id]
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT strategy_id FROM strategy_backtest_runs WHERE primary_window=TRUE")
                sids = [r[0] for r in cur.fetchall()]
        stats = {'built': 0, 'skipped': 0}
        for sid in sids:
            panel = build_panel(sid, conn, prices, hv21_for, bench_ret)
            if panel is None:
                stats['skipped'] += 1
                continue
            persist_panel(conn, panel)
            stats['built'] += 1
        return stats
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', '.env'))
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--strategy-id', default=None)
    a = ap.parse_args()
    print(rebuild(a.strategy_id))
```

- [ ] **Step 4: Run, verify pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_backtest_panel.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/backtest_panel.py tests/test_backtest_panel.py
git commit -m "feat(backtest): build_panel orchestration + hv21 + persist + CLI"
```

---

## Task 5: Build panels for all live strategies + verify invariants

**Files:** none (data/ops step).

- [ ] **Step 1: Rebuild all panels**

Run: `cd /root/openclaw && export POSTGRES_URI=$(grep -E "^POSTGRES_URI=" .env|cut -d= -f2-) && PYTHONPATH=src python3 -m backtest.backtest_panel --rebuild`
Expected: prints `{'built': N, 'skipped': M}` with `built` ≈ 50–51.

- [ ] **Step 2: Verify invariants (O+U+E == n_trades; curve non-empty)**

Run:
```bash
python3 - <<'PY'
import os,psycopg2,psycopg2.extras,json
c=psycopg2.connect(os.environ['POSTGRES_URI']).cursor(cursor_factory=psycopg2.extras.RealDictCursor)
c.execute("SELECT strategy_id,oue_over,oue_under,oue_expected,n_trades,equity_curve FROM strategy_backtest_panel")
bad=0
for r in c.fetchall():
    if r['oue_over']+r['oue_under']+r['oue_expected']!=r['n_trades']: bad+=1; print("OUE!=n", r['strategy_id'])
    if not r['equity_curve']: print("empty curve", r['strategy_id'])
print("rows ok" if bad==0 else f"{bad} OUE invariant breaks")
PY
```
Expected: `rows ok`, no empty curves for covered strategies.

- [ ] **Step 3: Commit (empty)**

```bash
git commit --allow-empty -m "chore(backtest): build dashboard panels for all live strategies"
```

---

## Task 6: Hook panel rebuild into the backtest flow

**Files:**
- Modify: `src/backtest/unified_backtest.py` (after the `run_backtest` persistence block, ~line 805, inside `run_backtest`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_unified_backtest_panel_hook.py
import sys, inspect
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from backtest import unified_backtest

def test_run_backtest_invokes_panel_rebuild():
    src = inspect.getsource(unified_backtest.run_backtest)
    assert 'backtest_panel' in src and 'rebuild' in src, \
        "run_backtest must refresh the dashboard panel after persisting a run"
```

- [ ] **Step 2: Run, verify fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_panel_hook.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement the hook**

In `src/backtest/unified_backtest.py`, immediately after the `primary_window` demotion / commit at the end of `run_backtest` (after line ~805, before `return`), add:

```python
        # Refresh the dashboard backtest panel for this strategy (best-effort;
        # a panel build failure must never fail the backtest itself).
        try:
            from backtest.backtest_panel import rebuild as _rebuild_panel
            _rebuild_panel(strategy_id)
        except Exception as _e:
            print(f'[unified_backtest] panel rebuild skipped: {_e}')
```

- [ ] **Step 4: Run, verify pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_unified_backtest_panel_hook.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_unified_backtest_panel_hook.py
git commit -m "feat(backtest): refresh dashboard panel after each backtest run"
```

---

## Task 7: API — `/api/strategies` backtest rewrite

**Files:**
- Modify: `src/channels/api/server.js` (the `/api/strategies` handler, ~lines 1154–1515)
- Test: `tests/test_api_strategies_backtest.test.js`

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_api_strategies_backtest.test.js
// Contract test: the per-strategy object builder maps backtest sources and
// keeps only last_signal + status live. We test the pure mapper exported
// from server for testability.
const assert = require('assert');
const { buildStrategyRow } = require('../src/channels/api/strategy_row');

const row = buildStrategyRow({
  sid: 'S_x', rec: { state: 'live', metadata: {} },
  isStale: false, regimeActive: true, activeRegimes: ['LOW_VOL'],
  eligRaw: ['LOW_VOL'], currentRegime: 'LOW_VOL',
  run: { total_sharpe: 2.0, total_return_pct: 50, total_max_dd_pct: 12,
         total_trades: 120, total_hit_rate: 0.55, avg_holding_days: 4 },
  regimeBreakdown: { LOW_VOL: { sharpe: 1.8, trade_count: 80, return_pct: 30, hit_rate: 0.6 } },
  panel: { effective_sharpe: 1.0, oue_over: 10, oue_under: 5, oue_expected: 105,
           oue_by_regime: { LOW_VOL: { over:10, under:5, expected:65 } } },
  bestWorst: { best: 0.22, worst: -0.09 },
  lastSignalDate: '2026-05-28',
});

assert.strictEqual(row.sharpe, 2.0);
assert.strictEqual(row.effective_sharpe, 1.0);
assert.strictEqual(row.closed_count, 120);          // backtest trade count
assert.strictEqual(row.win_rate, 0.55);             // backtest hit rate
assert.strictEqual(row.oue_over, 10);
assert.strictEqual(row.last_signal_date, '2026-05-28');  // live
assert.strictEqual(row.status, 'live');
assert.ok(!('open_count' in row), 'open_count must be removed');
assert.ok(!('avg_unrealized_pct' in row), 'live unrealized removed');
assert.ok(!('oue_multipliers_by_regime' in row), 'oue multiplier removed');
console.log('ok');
```

- [ ] **Step 2: Run, verify fail**

Run: `cd /root/openclaw && node tests/test_api_strategies_backtest.test.js`
Expected: FAIL (`Cannot find module '.../strategy_row'`).

- [ ] **Step 3: Implement the pure mapper**

```javascript
// src/channels/api/strategy_row.js
// Pure builder for one /api/strategies row. Backtest-sourced; only
// last_signal_date + status are live. No open positions, no live P&L.
function buildStrategyRow(x) {
  const run = x.run || {};
  const panel = x.panel || {};
  const bw = x.bestWorst || {};
  const act = run.avg_holding_days != null ? Number(run.avg_holding_days) : null;
  const arr = run.total_trades ? (Number(run.total_return_pct) / Number(run.total_trades)) : null; // mean trade %
  const adr = (arr != null && act) ? (arr / Math.max(1, act)) : null;
  return {
    strategy_id: x.sid,
    status: x.rec?.state || 'unknown',
    state: x.rec?.state || 'unknown',
    is_stale: x.isStale,
    regime_active: x.regimeActive,
    active_in_regimes: x.activeRegimes,
    eligible_regimes: x.eligRaw,
    current_regime: x.currentRegime,
    description: x.rec?.metadata?.description || '',
    // ── Backtest-sourced metrics ──
    sharpe: run.total_sharpe ?? null,
    effective_sharpe: panel.effective_sharpe ?? null,
    backtest_return_pct: run.total_return_pct ?? null,
    backtest_max_dd_pct: run.total_max_dd_pct ?? null,
    closed_count: run.total_trades ?? 0,
    win_rate: run.total_hit_rate ?? null,
    arr_pct: arr,
    adr_pct: adr,
    act_days: act,
    best_trade_pct: bw.best ?? null,
    worst_trade_pct: bw.worst ?? null,
    backtest_regime_breakdown: x.regimeBreakdown || {},
    oue_over: panel.oue_over ?? 0,
    oue_under: panel.oue_under ?? 0,
    oue_expected: panel.oue_expected ?? 0,
    oue_by_regime: panel.oue_by_regime || null,
    has_backtest_panel: !!x.panel,
    // ── Live (the ONLY live fields) ──
    last_signal_date: x.lastSignalDate ?? null,
  };
}
module.exports = { buildStrategyRow };
```

- [ ] **Step 4: Run, verify pass**

Run: `cd /root/openclaw && node tests/test_api_strategies_backtest.test.js`
Expected: `ok`.

- [ ] **Step 5: Rewire the `/api/strategies` handler to use the mapper + backtest queries**

In `src/channels/api/server.js`: at top add `const { buildStrategyRow } = require('./strategy_row');`. In the handler (~1154):
- Keep queries 4 & 5 (`strategy_backtest_runs`, `strategy_backtest_regimes`) and the `unifiedBacktest` assembly.
- ADD a panel query + a best/worst query + a last-signal query:
```javascript
const panelRows = (await dbQuery(`
  SELECT strategy_id, effective_sharpe, oue_over, oue_under, oue_expected, oue_by_regime
    FROM strategy_backtest_panel`).catch(() => ({ rows: [] }))).rows;
const panelById = Object.fromEntries(panelRows.map(r => [r.strategy_id, r]));

const bwRows = (await dbQuery(`
  SELECT t.strategy_id, MAX(t.pnl_pct) AS best, MIN(t.pnl_pct) AS worst
    FROM strategy_backtest_trades t
    JOIN (SELECT DISTINCT ON (strategy_id) strategy_id, run_id
            FROM strategy_backtest_runs WHERE primary_window=TRUE
            ORDER BY strategy_id, run_at DESC) r ON r.run_id=t.run_id
   GROUP BY t.strategy_id`).catch(() => ({ rows: [] }))).rows;
const bwById = Object.fromEntries(bwRows.map(r => [r.strategy_id, r]));

const lsRows = (await dbQuery(`
  SELECT strategy_id, MAX(signal_date) AS last_signal_date
    FROM execution_signals WHERE strategy_id IS NOT NULL GROUP BY strategy_id`
  ).catch(() => ({ rows: [] }))).rows;
const lastSignalById = Object.fromEntries(lsRows.map(r => [r.strategy_id, r.last_signal_date]));
```
- REMOVE queries that are now unused for the payload: `strategy_stats` (Q1), the live regime breakdown (Q7), the live OUE counts (Q6), and the oue_multiplier (Q8). (Keep `strategy_registry`/`strategy_regime_params` only for `eligible_regimes`/`active_in_regimes`/staging.)
- REPLACE the `rows.push({...})` object literal (~1409–1461) with:
```javascript
rows.push(buildStrategyRow({
  sid, rec, isStale, regimeActive, activeRegimes, eligRaw: _eligRaw, currentRegime,
  run: ubtRunById[sid] || {},
  regimeBreakdown: _decoratedBreakdown,
  panel: panelById[sid] || null,
  bestWorst: bwById[sid] || {},
  lastSignalDate: lastSignalById[sid] || null,
}));
```
(where `ubtRunById` is `Object.fromEntries(ubtRunRows.map(r => [r.strategy_id, r]))` — add it next to the `unifiedBacktest` assembly.)

- [ ] **Step 6: Smoke the endpoint**

Run: `cd /root/openclaw && node -e "require('./src/channels/api/server.js')" &` then `sleep 2 && curl -s localhost:3000/api/strategies | python3 -c "import sys,json;d=json.load(sys.stdin);r=d[0];print('keys ok' if 'sharpe' in r and 'open_count' not in r else 'BAD', 'n=',len(d))"` (kill the node process after).
Expected: `keys ok n= <count>`.

- [ ] **Step 7: Commit**

```bash
git add src/channels/api/strategy_row.js tests/test_api_strategies_backtest.test.js src/channels/api/server.js
git commit -m "feat(api): /api/strategies backtest-sourced (last_signal+status live only)"
```

---

## Task 8: API — backtest equity-curve endpoint

**Files:**
- Modify: `src/channels/api/server.js` (add route near the old arr-curve route ~2433)

- [ ] **Step 1: Write the failing test**

```javascript
// tests/test_api_backtest_curve.test.js  (integration: needs DB + server)
// Asserts the route exists and returns {rows:[{date,strat_equity,spx_equity,regime}]}.
const http = require('http');
require('../src/channels/api/server.js');
setTimeout(() => {
  http.get('http://localhost:3000/api/strategies/' +
    encodeURIComponent(process.env.SMOKE_SID || 'momentum_12_1') + '/backtest-curve', res => {
    let b=''; res.on('data',d=>b+=d); res.on('end',()=>{
      const j=JSON.parse(b);
      if (!Array.isArray(j.rows)) { console.error('BAD shape'); process.exit(1); }
      if (j.rows.length && !('spx_equity' in j.rows[0])) { console.error('missing spx_equity'); process.exit(1); }
      console.log('ok rows='+j.rows.length); process.exit(0);
    });
  });
}, 1500);
```

- [ ] **Step 2: Run, verify fail**

Run: `cd /root/openclaw && POSTGRES_URI=$(grep -E "^POSTGRES_URI=" .env|cut -d= -f2-) node tests/test_api_backtest_curve.test.js`
Expected: FAIL (404/non-array — route missing).

- [ ] **Step 3: Implement the route**

In `src/channels/api/server.js` (near line 2433):
```javascript
app.get('/api/strategies/:id/backtest-curve', async (req, res) => {
  const sid = String(req.params.id || '');
  if (!sid) return res.status(400).json({ error: 'strategy id required' });
  try {
    const r = (await dbQuery(
      `SELECT equity_curve FROM strategy_backtest_panel WHERE strategy_id=$1`, [sid])).rows[0];
    res.json({ rows: (r && r.equity_curve) ? r.equity_curve : [] });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});
```

- [ ] **Step 4: Run, verify pass**

Run: `cd /root/openclaw && POSTGRES_URI=$(grep -E "^POSTGRES_URI=" .env|cut -d= -f2-) node tests/test_api_backtest_curve.test.js`
Expected: `ok rows=<n>`.

- [ ] **Step 5: Commit**

```bash
git add src/channels/api/server.js tests/test_api_backtest_curve.test.js
git commit -m "feat(api): /api/strategies/:id/backtest-curve endpoint"
```

---

## Task 9: Frontend — active-stack table columns

**Files:**
- Modify: `src/channels/api/server.js` (`_renderActiveStack` ~8814–8886, sort keys ~8735–8787)

- [ ] **Step 1: Update the `<thead>` (replace the header `<tr>` ~8814)**

Replace the **Open** header with nothing (remove it) and add **Sharpe** + **Eff.Sharpe**; retitle OUE to backtest:
```javascript
el.innerHTML = `<table class="db-table st-active-table" style="min-width:1180px">
  <tr>
    <th data-sort-key="strategy_id" data-sort-type="str">Strategy</th>
    <th data-sort-key="_active_rank" data-sort-type="num" title="Waiting(0)<Stale(1)<Live(2)">Status</th>
    <th title="Per-regime BACKTEST Sharpe; dot=current regime; blue=declared">By Regime</th>
    <th class="num" data-sort-key="sharpe" data-sort-type="num" title="Backtest Sharpe (primary window)">Sharpe</th>
    <th class="num" data-sort-key="effective_sharpe" data-sort-type="num" title="Sharpe / sqrt(avg holding days)">Eff.Sharpe</th>
    <th class="num" data-sort-key="closed_count" data-sort-type="num" title="Backtest trade count">Closed</th>
    <th class="num" data-sort-key="win_rate" data-sort-type="num" title="Backtest hit rate">Win&nbsp;%</th>
    <th class="num" data-sort-key="arr_pct" data-sort-type="num" title="Backtest mean trade return %">ARR&nbsp;%</th>
    <th class="num" data-sort-key="adr_pct" data-sort-type="num" title="Backtest ARR / ACT">ADR&nbsp;%</th>
    <th class="num" data-sort-key="act_days" data-sort-type="num" title="Backtest avg holding days">ACT</th>
    <th class="num" data-sort-key="_oue_total" data-sort-type="num" title="BACKTEST OUE (GBM σ): O=realized>expectation by ≥${'2'}σ, U=below, E=within. O+U+E = backtest trades.">#&nbsp;O/U/E</th>
    <th data-sort-key="last_signal_date" data-sort-type="date">Last Signal</th>
    <th>Actions</th>
  </tr>
```

- [ ] **Step 2: Update the per-row `<td>` cells (~8865–8884)**

Remove the Open `<td>`, change Closed/Win/ARR/ADR/ACT/OUE to the new backtest fields, add Sharpe/Eff.Sharpe, keep Last Signal:
```javascript
  const arr = r.arr_pct, adr = r.adr_pct, act = r.act_days;
  const o = r.oue_over||0, u = r.oue_under||0, e = r.oue_expected||0;
  const oueEmpty = (o+u+e)===0;
  const ourCell = oueEmpty ? '<span style="color:var(--dim)">—</span>'
    : `<span style="color:#4ade80">${o}</span>/<span style="color:#f87171">${u}</span>/<span style="color:#94a3b8">${e}</span>`;
  const sh = r.sharpe, esh = r.effective_sharpe;
  const closedTxt = r.closed_count || 0;
  const winTxt = r.win_rate != null ? Math.round(parseFloat(r.win_rate)*100)+'%' : '—';
  const actTxt = act != null ? act.toFixed(1)+(act===1?' day':' days') : '—';
  // ... inside the returned <tr> (drop the Open <td>):
  // <td>${_regimeBreakdown(r)}</td>
  // <td class="num">${sh!=null?parseFloat(sh).toFixed(2):'—'}</td>
  // <td class="num">${esh!=null?parseFloat(esh).toFixed(2):'—'}</td>
  // <td class="num">${closedTxt}</td>
  // <td class="num">${winTxt}</td>
  // <td class="num ${pnlCls(arr)}">${arr!=null?((arr>=0?'+':'')+arr.toFixed(2)+'%'):'—'}</td>
  // <td class="num ${pnlCls(adr)}">${adr!=null?((adr>=0?'+':'')+adr.toFixed(2)+'%'):'—'}</td>
  // <td class="num" style="color:var(--muted)">${actTxt}</td>
  // <td class="num">${ourCell}</td>
  // <td style="color:var(--dim)">${_fmtDate(r.last_signal_date)}</td>
```
Also update the expand row `colspan` from `11` to `12` (one column added net: −Open, +Sharpe, +Eff.Sharpe = +1).

- [ ] **Step 3: Update sort-key derivations (~8735–8787)**

Replace `_arr_pct`/`_adr_pct`/`_act_days`/`d1_total`/`open_count`/`_filtered_*` derivations with backtest-direct fields and add `_oue_total`:
```javascript
  r._oue_total = (r.oue_over||0)+(r.oue_under||0)+(r.oue_expected||0);
  // arr_pct / adr_pct / act_days / sharpe / effective_sharpe / closed_count /
  // win_rate now come straight from the API payload — no live derivation.
```
Remove references to `open_count`, `_filtered_closed`, `_filtered_win`, `_arr_pct`/`_adr_pct`/`_act_days` live-recompute, `_oue_mult`.

- [ ] **Step 3b: Repoint `_regimeBreakdown()` to backtest (helper ~8342–8398)**

The "By Regime" column helper currently reads `r.live_regime_breakdown` and `b.arr`. Repoint it to `r.backtest_regime_breakdown` and show backtest **Sharpe** per regime (the breakdown's per-regime fields are `sharpe`, `trade_count`, `return_pct`, `hit_rate` — there is no `arr`/`adr`/`act`):
```javascript
function _regimeBreakdown(r) {
  const breakdown = r.backtest_regime_breakdown || {};
  const eligible = Array.isArray(r.eligible_regimes)
    ? r.eligible_regimes
    : (r.active_in_regimes && r.active_in_regimes.length ? r.active_in_regimes : _REGIME_AXIS);
  const current  = r.current_regime || null;
  const sid      = r.strategy_id;
  const editable = r.state === 'live';
  const cells = _REGIME_AXIS.map(rg => {
    const isEligible = eligible.includes(rg);
    const b      = breakdown[rg];
    const trades = b && b.trade_count ? parseInt(b.trade_count) : 0;
    const sharpe = (b && b.sharpe != null) ? parseFloat(b.sharpe) : null;
    const klass = ['st-regime-cell', `st-rg-${rg}`];
    if (current === rg) klass.push('st-rg-current');
    if (!trades)        klass.push('st-rg-na');
    if (!isEligible)    klass.push('st-rg-ineligible');
    if (editable)       klass.push('st-rg-editable');
    const valTxt = sharpe != null ? sharpe.toFixed(2) : '—';
    const statsLine = !trades ? 'no backtest trades'
      : 'Sharpe ' + valTxt
        + ' · Ret ' + (b.return_pct != null ? (b.return_pct >= 0 ? '+' : '') + parseFloat(b.return_pct).toFixed(1) + '%' : '—')
        + ' · Win ' + (b.hit_rate != null ? Math.round(b.hit_rate * 100) + '%' : '—')
        + ' · ' + trades + ' trades';
    const eligLine = isEligible ? 'eligible' : 'NOT eligible';
    const editHint = editable ? ' · click to toggle' : '';
    const ttl = rg + ' [' + eligLine + ']: ' + statsLine + editHint;
    const onclick = editable ? ` onclick="_stToggleRegimeEligibility(event, '${_escStr(sid)}', '${rg}')"` : '';
    return `<span class="${klass.join(' ')}" title="${_escStr(ttl)}"${onclick}>
              <span class="st-rg-tag">${rg}</span>
              <span class="st-rg-val">${valTxt}</span>
            </span>`;
  }).join('');
  const gridTitle = editable
    ? 'Per-regime BACKTEST Sharpe · click a regime to toggle eligibility'
    : "Per-regime BACKTEST Sharpe · operator toggle only for state='live'";
  return `<div class="st-regime-grid" title="${gridTitle}">${cells}</div>`;
}
```
(Drops the drift-badge lookup `_rdDriftFor` for now — it was keyed on live data; re-add later if a backtest-drift signal exists.)

- [ ] **Step 4: Manual verify in browser/curl**

Run server, load dashboard; confirm: no **Open** column, **Sharpe** + **Eff.Sharpe** present, Closed/Win/ARR/ADR/ACT show backtest values, **# O/U/E** shows backtest counts, **Last Signal** + **Status** unchanged. (No automated test — DOM render.)

- [ ] **Step 5: Commit**

```bash
git add src/channels/api/server.js
git commit -m "feat(dashboard): backtest columns in active-stack table (remove Open, add Sharpe/Eff.Sharpe)"
```

---

## Task 10: Frontend — expansion panel charts (equity-vs-SP500 + closed-per-regime)

**Files:**
- Modify: `src/channels/api/server.js` (`_stPaintExpand` body ~8995–9026; replace `_stRenderArrChart` ~9028 and `_stRenderFlowChart` ~9113; fetch ~9008)

- [ ] **Step 1: Update the expansion body + fetch (replace ARR/flow markup + fetch)**

In `_stPaintExpand`, replace the right-hand `st-arr-wrap` section markup with two canvases and switch the fetch to the new endpoint:
```javascript
      <div class="st-expand-section st-arr-wrap">
        <div class="st-expand-section-title"><span>Backtest equity vs SP500</span><span style="color:var(--dim);font-size:9px">regime-shaded</span></div>
        <div class="st-arr-canvas-wrap"><canvas id="st-eq-chart-${sid}"></canvas></div>
        <div class="st-flow-row">
          <div class="st-flow-meta"><span style="color:var(--dim);font-size:9.5px">Backtest positions closed per regime</span><span></span></div>
          <div class="st-flow-canvas-wrap"><canvas id="st-cpr-chart-${sid}"></canvas></div>
        </div>
      </div>`;
  // fetch the backtest curve (cache reused)
  let payload = _stArrCache[sid];
  if (!payload) {
    if (!_stArrFetching[sid]) {
      _stArrFetching[sid] = fetch('/api/strategies/'+encodeURIComponent(sid)+'/backtest-curve')
        .then(r => r.ok ? r.json() : { rows: [] }).catch(() => ({ rows: [] }));
    }
    payload = await _stArrFetching[sid]; delete _stArrFetching[sid]; _stArrCache[sid] = payload;
  }
  if (_stExpandedSid !== sid) return;
  _stRenderEquityChart(sid, payload);
  _stRenderClosedPerRegimeChart(sid, r);     // r.backtest_regime_breakdown
```
Also change the per-regime grid (`cellsHtml`) source from `r.live_regime_breakdown` to `r.backtest_regime_breakdown` (fields `sharpe`, `trade_count`, `return_pct`, `hit_rate`) — relabel ARR/Win → backtest. (Mirror the existing cell markup; substitute fields.)

- [ ] **Step 2: Implement `_stRenderEquityChart` (replaces `_stRenderArrChart`)**

```javascript
let _stEqChart = null;
function _stRenderEquityChart(sid, payload) {
  const canvas = document.getElementById('st-eq-chart-' + sid);
  if (!canvas) return;
  if (_stEqChart) { try { _stEqChart.destroy(); } catch (_) {} _stEqChart = null; }
  const rows = (payload && payload.rows) || [];
  const wrap = canvas.parentElement;
  if (!rows.length) { if (wrap) wrap.style.display = 'none'; return; }
  if (wrap) wrap.style.display = '';
  const labels = rows.map(r => r.date);
  const strat  = rows.map(r => r.strat_equity);
  const spx    = rows.map(r => r.spx_equity);
  const regimeMap = {};
  for (const r of rows) if (r.regime) regimeMap[r.date] = r.regime;
  _stEqChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [
      { label:'strategy', data:strat, borderColor:'#58a6ff', backgroundColor:'rgba(88,166,255,0.10)',
        borderWidth:1.6, pointRadius:0, fill:true, tension:0.15 },
      { label:'SP500', data:spx, borderColor:'#8b949e', borderWidth:1.2, pointRadius:0,
        borderDash:[4,3], fill:false, tension:0.15 },
    ]},
    options: {
      responsive:true, maintainAspectRatio:false, animation:false, resizeDelay:200,
      interaction:{ mode:'index', intersect:false },
      plugins: {
        legend:{ display:true, labels:{ color:'#8b949e', font:{size:9}, boxWidth:10 } },
        regimeBands:{ map: regimeMap },
        tooltip:{ backgroundColor:'#0d1117', borderColor:'#30363d', borderWidth:1,
          titleColor:'#c9d1d9', bodyColor:'#e6edf3', displayColors:true, padding:8,
          callbacks:{ label: ctx => {
            const r = rows[ctx.dataIndex];
            return ctx.dataset.label + ' ' + ctx.parsed.y.toFixed(3) + 'x' +
                   (ctx.datasetIndex===0 && r ? '  · '+(r.regime||'—') : '');
          }}},
      },
      scales: {
        x:{ ticks:{ color:'#484f58', maxTicksLimit:8, font:{size:9} }, grid:{ color:'rgba(48,54,61,0.45)' } },
        y:{ position:'right', ticks:{ color:'#484f58', font:{size:9}, callback:v=>v.toFixed(1)+'x', maxTicksLimit:5 },
            grid:{ color:'rgba(48,54,61,0.45)' } }
      }
    }
  });
}
```

- [ ] **Step 3: Implement `_stRenderClosedPerRegimeChart` (replaces `_stRenderFlowChart`)**

```javascript
let _stCprChart = null;
const _CPR_AXIS = ['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'];
const _CPR_COLORS = { LOW_VOL:'rgba(63,185,80,0.9)', TRANSITIONING:'rgba(210,153,34,0.9)',
                      HIGH_VOL:'rgba(240,136,62,0.9)', CRISIS:'rgba(248,81,73,0.9)' };
function _stRenderClosedPerRegimeChart(sid, r) {
  const canvas = document.getElementById('st-cpr-chart-' + sid);
  if (!canvas) return;
  if (_stCprChart) { try { _stCprChart.destroy(); } catch (_) {} _stCprChart = null; }
  const bd = r.backtest_regime_breakdown || {};
  const counts = _CPR_AXIS.map(rg => (bd[rg] && bd[rg].trade_count) ? bd[rg].trade_count : 0);
  const wrap = canvas.parentElement;
  if (!counts.some(c => c > 0)) { if (wrap) wrap.style.display = 'none'; return; }
  if (wrap) wrap.style.display = '';
  _stCprChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: { labels: _CPR_AXIS, datasets: [{ data: counts,
      backgroundColor: _CPR_AXIS.map(rg => _CPR_COLORS[rg]), borderWidth: 0, barPercentage: 0.7 }]},
    options: {
      responsive:true, maintainAspectRatio:false, animation:false, resizeDelay:200,
      plugins: { legend:{ display:false },
        tooltip:{ backgroundColor:'#0d1117', borderColor:'#30363d', borderWidth:1,
          titleColor:'#c9d1d9', bodyColor:'#e6edf3', displayColors:false, padding:8,
          callbacks:{ label: ctx => 'closed: ' + ctx.parsed.y }}},
      scales: {
        x:{ ticks:{ color:'#484f58', font:{size:9} }, grid:{ display:false } },
        y:{ position:'right', beginAtZero:true, ticks:{ color:'#484f58', font:{size:9}, precision:0, maxTicksLimit:3 },
            grid:{ display:false } }
      }
    }
  });
}
```

- [ ] **Step 4: Remove the now-dead `/api/strategies/:id/arr-curve` route and `_stRenderArrChart`/`_stRenderFlowChart`**

Delete the old route handler (~2433–2546) and the two old render functions (~9028–9209). Grep to confirm no remaining callers: `grep -n "_stRenderArrChart\|_stRenderFlowChart\|arr-curve" src/channels/api/server.js` → only definitions removed, no live callers.

- [ ] **Step 5: Manual verify**

Restart the dashboard, expand a strategy: confirm the equity-vs-SP500 line chart renders with regime shading, the closed-per-regime bar renders, and the per-regime grid shows backtest stats. Confirm no console errors and the old opened/closed flow + ARR curve are gone.

- [ ] **Step 6: Commit**

```bash
git add src/channels/api/server.js
git commit -m "feat(dashboard): backtest equity-vs-SP500 + closed-per-regime charts; drop live ARR/flow"
```

---

## Task 11: Full verification + deploy

**Files:** none.

- [ ] **Step 1: Run all new tests**

Run:
```bash
cd /root/openclaw
python3 -m pytest tests/test_backtest_panel.py tests/test_unified_backtest_panel_hook.py -q
node tests/test_api_strategies_backtest.test.js
POSTGRES_URI=$(grep -E "^POSTGRES_URI=" .env|cut -d= -f2-) node tests/test_api_backtest_curve.test.js
```
Expected: all PASS / `ok`.

- [ ] **Step 2: Regression sanity (no unrelated breakage)**

Run: `python3 -m pytest -q -p no:cacheprovider -m "not integration" --ignore=tests/integration_test.py --ignore=tests/test_migration_111.py --ignore=tests/test_migration_115.py --ignore=tests/test_migrations_112_113_114.py 2>&1 | tail -3`
Expected: failure count equals the documented pre-existing baseline (35) — no NEW failures from this work.

- [ ] **Step 3: Restart the dashboard service + eyeball**

Restart `johnbot` (which serves :3000) and load the dashboard; verify the active stack + an expanded strategy render fully with backtest data and only `last_signal`/`status` live.

- [ ] **Step 4: Final commit / branch push**

```bash
git push -u origin feat/dashboard-backtest-metrics
```

---

## Notes / invariants
- **Never-delete:** migration 124 is additive; no drops. `strategy_backtest_panel` is fully recomputed (upsert), not appended.
- **Freshness:** panels refresh on every `unified_backtest` run (Task 6) + manual `--rebuild`. The dashboard reads precomputed rows — no per-request pandas.
- **Graceful gaps:** strategies without a primary-window run get no panel row → `has_backtest_panel=false`, empty curve, OUE `—` (UI placeholder).
- **Dashboard-only:** no live-trading behavior changes; rollback = revert `server.js` + the builder (panel table is harmless if orphaned).
