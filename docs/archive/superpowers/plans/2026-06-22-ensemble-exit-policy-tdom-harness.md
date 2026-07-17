# Ensemble Exit Policy — T-DOM Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a tested harness that runs the Ensemble Exit Policy spec (long via `exit_sim.py`, short via `exit_sim_short.py`, deltas D1–D3) against our real multi-strategy `execution_signals` clusters and adjudicates the spec's **T-DOM** gate — does the policy beat both rejected baselines, net of cost, with a block-bootstrap CI lower bound > 0.

**Architecture:** Pure-Python (numpy + psycopg2) analysis harness in the isolated worktree. Levels are computed by the reference generators (deterministic in the floor-pinned regime, verified in Step 0). One direction-aware daily-bar multi-day first-touch replay engine scores all three policies on identical price paths; growth `G` is computed identically per policy; a stationary block-bootstrap by trading day gives the CI on `ΔG`.

**Tech Stack:** Python 3.11 (`/usr/bin/python3`, no venv), numpy 2.4.4, psycopg2, pandas/pyarrow for parquet, `python-dotenv`; reuses `src/execution/{strategy_weights,strategy_similarity,bracket_stacking}.py` and `src/strategies/.../oxford_crabel.atr`; reads Alpaca/parquet prices.

**Design doc:** `docs/superpowers/specs/2026-06-22-ensemble-exit-policy-tdom-harness-design.md`

## Global Constraints

- **Working dir / branch:** worktree `/root/openclaw/.claude/worktrees/ensemble-exit-tdom`, branch `feat/ensemble-exit-tdom`. Never `git checkout`/switch the main `/root/openclaw` checkout (tomorrow's 12:00 UTC C3 flip restarts johnbot from it).
- **Resource ceiling:** 2-core / 8 GB / no swap, with a ~1.65 GB calendar sweep live. All compute single-threaded, `nice -n 19`. **Never load the full 728 MB `prices.parquet`** — always slice by ticker+date. No backtest/pytest fan-out that runs concurrent heavy CPU. Keep `MemAvailable` comfortably > 4.5 GB so the C3 guard window (Tue 13:30–15:30 UTC) is unaffected; finish before then regardless.
- **Read-only against production:** the harness only reads `execution_signals`, `strategy_weights_by_regime`, `strategy_similarity_matrix`, `ticker_metadata_snapshots`, and price parquet/Alpaca. It writes only to `harness/out/`. No DB writes, no service restarts, no master-data mutation.
- **Env:** `from dotenv import load_dotenv; load_dotenv('/root/openclaw/.env')` before reading `os.environ['POSTGRES_URI']`. Reference prices/data by absolute path under `/root/openclaw/`.
- **All paths below are relative to the worktree root** unless absolute.
- **Units:** spec runs in `unit_mode="price"` mapped so 1 bar = 1 trading day; `session_bars=None` (non-intraday). σ_eff is daily ATR(20) in price units. Per-trade return `R` is reported in σ units (PnL fraction on entry ÷ σ_eff/entry → consistent). φ (kelly_fraction) = 0.5.
- **Determinism:** every MC/bootstrap call takes an explicit seed; default seed 0. No `Math.random`/wall-clock in logic.
- **Verbatim reference code is frozen:** `exit_sim.py` / `exit_sim_short.py` are copied byte-for-byte from the spec; do not edit them — wrap/dispatch in `generator.py`.

---

### Task 1: Vendoring + direction dispatch (`generator.py`)

**Files:**
- Create: `harness/__init__.py` (empty)
- Create: `harness/exit_sim.py` (copy verbatim from `/tmp/exit_sim.py`)
- Create: `harness/exit_sim_short.py` (copy verbatim from `/tmp/exit_sim_short.py`; it does `import exit_sim as e`, so the harness must run with `harness/` on `sys.path`)
- Create: `harness/generator.py`
- Test: `harness/tests/test_generator.py`

**Interfaces:**
- Consumes: `exit_sim.{Strategy,Context,Config,generate_exit_policy}`, `exit_sim_short.generate_exit_policy`.
- Produces:
  - `Policy = dict(stop_dist: float, takes: list[dict(distance: float, fraction: float, time_bars: float)], time_stop_bars: float, direction: int, diagnostics: dict)` — a normalized shape identical for long & short.
  - `generate(strategies: list[Strategy], ctx: Context, config: Config) -> Policy` — dispatches on `int(strategies[0].direction)`: `+1` → `exit_sim.generate_exit_policy`; `−1` → `exit_sim_short.generate_exit_policy`. Asserts all `strategies[i].direction` equal (spec A-7); raises `ValueError("mixed-sign ensemble")` otherwise. Normalizes both outputs into `Policy` (distances are in price units; `direction` recorded; `diagnostics` carries `a_mult`,`stopout_prob`,`S_comb`,`mu0`,`E_tau`,`kappa_C` if present else None).

- [ ] **Step 1: Write the failing test**

```python
# harness/tests/test_generator.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import exit_sim as e
from generator import generate, Policy_keys  # Policy_keys: tuple of required keys

CFG = e.Config(mc_paths=20_000, mc_dt=0.5, seed=7, a_grid=(0.5, 5.0, 0.5))

def _strats(direction):
    return [e.Strategy(f"s{i}", sharpe=1.0, half_life=h, direction=direction)
            for i, h in enumerate([6, 12, 24])]

def test_long_dispatch_shape():
    s = _strats(+1)
    ctx = e.Context(C=np.eye(3), sigma_underlying=1.0, hurdle_g_star=0.03, entry_price=100.0)
    p = generate(s, ctx, CFG)
    assert set(Policy_keys) <= set(p)
    assert p["direction"] == 1
    assert p["stop_dist"] > 0 and len(p["takes"]) == 3

def test_short_is_carryzero_mirror_of_long():
    # T-SYM at the harness level: carry=0 short == long mirror (distances equal)
    sL, sS = _strats(+1), _strats(-1)
    ctxL = e.Context(C=np.eye(3), sigma_underlying=1.0, hurdle_g_star=0.03, entry_price=100.0)
    ctxS = e.Context(C=np.eye(3), sigma_underlying=1.0, hurdle_g_star=0.03, entry_price=100.0)
    setattr(ctxS, "carry_per_bar", 0.0)
    pL, pS = generate(sL, ctxL, CFG), generate(sS, ctxS, CFG)
    assert abs(pL["stop_dist"] - pS["stop_dist"]) < 1e-9
    assert pS["direction"] == -1

def test_mixed_sign_rejected():
    s = _strats(+1); s[1].direction = -1
    ctx = e.Context(C=np.eye(3), sigma_underlying=1.0)
    try:
        generate(s, ctx, CFG); assert False, "should reject"
    except ValueError as ex:
        assert "mixed-sign" in str(ex)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw/.claude/worktrees/ensemble-exit-tdom && nice -n 19 python3 -m pytest harness/tests/test_generator.py -q`
Expected: FAIL (ImportError: cannot import name 'generate').

- [ ] **Step 3: Copy reference files + write `generator.py`**

```bash
cp /tmp/exit_sim.py       harness/exit_sim.py
cp /tmp/exit_sim_short.py harness/exit_sim_short.py
: > harness/__init__.py
: > harness/tests/__init__.py
```

```python
# harness/generator.py
"""Direction-aware dispatch over the verified reference generators.
Long  -> exit_sim.generate_exit_policy
Short -> exit_sim_short.generate_exit_policy (deltas D1-D3)
Normalizes both into a single Policy dict. Reference modules are NOT edited."""
import exit_sim as e
import exit_sim_short as es

Policy_keys = ("stop_dist", "takes", "time_stop_bars", "direction", "diagnostics")

def generate(strategies, ctx, config=None):
    config = config or e.Config()
    dirs = {int(getattr(s, "direction", 1)) for s in strategies}
    if len(dirs) != 1:
        raise ValueError("mixed-sign ensemble: refuse to net long and short (spec A-7)")
    d = dirs.pop()
    raw = (es.generate_exit_policy if d == -1 else e.generate_exit_policy)(strategies, ctx, config)
    diag = raw.get("diagnostics", {})
    return dict(
        stop_dist=float(raw["stop"]["distance"]),
        takes=[dict(distance=float(t["distance"]), fraction=float(t["fraction"]),
                    time_bars=float(t["time_bars"])) for t in raw["takes"]],
        time_stop_bars=float(raw["time_stop"]["bars"]),
        direction=d,
        diagnostics=dict(a_mult=diag.get("a_mult"), stopout_prob=diag.get("stopout_prob"),
                         S_comb=diag.get("S_comb"), mu0=diag.get("mu0"),
                         E_tau=diag.get("E_tau"), kappa_C=diag.get("kappa_C"),
                         fallback_used=diag.get("fallback_used"), carry=diag.get("carry", 0.0)),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw/.claude/worktrees/ensemble-exit-tdom && nice -n 19 python3 -m pytest harness/tests/test_generator.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add harness/ && git commit -m "feat(exit-tdom): vendor reference sims + direction dispatch"
```

---

### Task 2: Sliced price loader + ATR (`prices.py`)

**Files:**
- Create: `harness/prices.py`
- Test: `harness/tests/test_prices.py`

**Interfaces:**
- Consumes: `/root/openclaw/data/master/prices.parquet` (columns include `ticker,date,open,high,low,close`; confirm exact names at Step 3 — adapt if `o/h/l/c`).
- Produces:
  - `load_daily(tickers: set[str], start: str, end: str) -> dict[str, pandas.DataFrame]` — per ticker a date-indexed OHLC frame, **filtered on read** via pyarrow predicate pushdown (never materialize the full panel). Sorted ascending by date.
  - `atr(df: pandas.DataFrame, n: int = 20, as_of: str | None = None) -> float` — Wilder/simple ATR over the last `n` daily bars up to and including `as_of` (default last row). Returns `nan` if < `n` bars.

- [ ] **Step 1: Write the failing test** (uses real parquet, tiny slice; read-only)

```python
# harness/tests/test_prices.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import prices, math

def test_slice_is_small_and_correct():
    out = prices.load_daily({"AAPL"}, "2026-05-01", "2026-06-01")
    assert "AAPL" in out and len(out["AAPL"]) > 5
    df = out["AAPL"]
    assert set(["high","low","close"]).issubset({c.lower() for c in df.columns})
    assert df["close"].notna().all()

def test_atr_positive_and_bounded():
    out = prices.load_daily({"AAPL"}, "2026-03-01", "2026-06-01")
    a = prices.atr(out["AAPL"], n=20, as_of="2026-05-04")
    assert a > 0 and math.isfinite(a)

def test_atr_nan_when_insufficient():
    out = prices.load_daily({"AAPL"}, "2026-05-28", "2026-06-01")
    assert math.isnan(prices.atr(out["AAPL"], n=20))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `nice -n 19 python3 -m pytest harness/tests/test_prices.py -q`
Expected: FAIL (ModuleNotFoundError: prices).

- [ ] **Step 3: Inspect parquet schema, then implement**

First confirm columns (cheap, read 1 row group metadata):
```bash
nice -n 19 python3 -c "import pyarrow.parquet as pq; print(pq.ParquetFile('/root/openclaw/data/master/prices.parquet').schema_arrow)"
```

```python
# harness/prices.py
"""Memory-safe sliced daily-OHLC loader + ATR. Never loads the full panel."""
import numpy as np, pandas as pd, pyarrow.parquet as pq, pyarrow.compute as pc
PARQUET = "/root/openclaw/data/master/prices.parquet"
# adapt these to the real schema confirmed in Step 3:
COLS = ["ticker", "date", "open", "high", "low", "close"]

def load_daily(tickers, start, end):
    tickers = list(tickers)
    flt = (pc.field("ticker").isin(tickers) & (pc.field("date") >= pd.Timestamp(start)) &
           (pc.field("date") <= pd.Timestamp(end)))
    tbl = pq.read_table(PARQUET, columns=COLS, filters=flt)  # predicate pushdown
    df = tbl.to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    out = {}
    for tk, g in df.groupby("ticker"):
        out[str(tk)] = g.sort_values("date").set_index("date")
    return out

def atr(df, n=20, as_of=None):
    if df is None or len(df) == 0:
        return float("nan")
    d = df if as_of is None else df.loc[:pd.Timestamp(as_of)]
    if len(d) < n:
        return float("nan")
    h, l, c = d["high"].astype(float), d["low"].astype(float), d["close"].astype(float)
    pc_ = c.shift(1)
    tr = pd.concat([(h - l), (h - pc_).abs(), (l - pc_).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())
```

If Step-3 schema uses `o/h/l/c` or `pyarrow` filters reject `Timestamp`, adapt `COLS` and cast `date` to the parquet's type. Keep the slice predicate — do not drop it.

- [ ] **Step 4: Run test to verify it passes**

Run: `nice -n 19 python3 -m pytest harness/tests/test_prices.py -q`
Expected: PASS (3 passed). If a test ticker has no rows in-window, switch to a liquid ticker present in the panel (e.g. "MSFT").

- [ ] **Step 5: Commit**

```bash
git add harness/prices.py harness/tests/test_prices.py && git commit -m "feat(exit-tdom): memory-safe sliced price loader + ATR"
```

---

### Task 3: half-life estimator (`half_life.py`)

**Files:**
- Create: `harness/half_life.py`
- Test: `harness/tests/test_half_life.py`

**Interfaces:**
- Consumes: `strategy_backtest_trades` / `signal_pnl` daily returns per strategy (read-only). `strategy_weights_by_regime.cadence_days` for the passthrough.
- Produces:
  - `autocorr_half_life(returns: numpy.ndarray, lo: float = 1.0, hi: float = 252.0) -> float` — AR(1) lag-1 autocorr `ρ̂`; `hl = ln2 / (−ln ρ̂)` for `0 < ρ̂ < 1`; clamp to `[lo, hi]`; if `ρ̂ ≤ 0` (mean-reverting/no persistence) return `lo`; if `ρ̂ ≥ 1` return `hi`. Requires ≥ 8 obs else returns `nan`.
  - `series_for_strategy(strategy_id: str, conn) -> numpy.ndarray` — daily-return series (prefers `strategy_backtest_trades.pnl_pct` ordered by date; falls back to `signal_pnl.realized_pnl_pct`). Empty array if none.
  - `half_life_for(strategy_id, conn, mode: str, cadence_days: float) -> float` — `mode="autocorr"` → `autocorr_half_life(series_for_strategy(...))` with `nan`→`cadence_days` fallback; `mode="cadence"` → `cadence_days`.

- [ ] **Step 1: Write the failing test** (synthetic AR(1), no DB)

```python
# harness/tests/test_half_life.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import half_life as hl

def _ar1(rho, n=4000, seed=0):
    rng = np.random.default_rng(seed); x = np.zeros(n)
    for t in range(1, n):
        x[t] = rho * x[t-1] + rng.standard_normal()
    return x

def test_recovers_known_half_life():
    rho = 0.5 ** (1/10.0)             # true half-life 10
    est = hl.autocorr_half_life(_ar1(rho))
    assert 7.0 <= est <= 14.0, est

def test_meanreverting_returns_floor():
    assert hl.autocorr_half_life(_ar1(-0.3)) == 1.0

def test_too_short_returns_nan():
    import math
    assert math.isnan(hl.autocorr_half_life(np.array([0.1, -0.2, 0.05])))
```

- [ ] **Step 2: Run to verify it fails** — `nice -n 19 python3 -m pytest harness/tests/test_half_life.py -q` → FAIL (no module).

- [ ] **Step 3: Implement**

```python
# harness/half_life.py
import numpy as np

def autocorr_half_life(returns, lo=1.0, hi=252.0):
    r = np.asarray(returns, float); r = r[np.isfinite(r)]
    if r.size < 8:
        return float("nan")
    r = r - r.mean()
    denom = float(np.dot(r, r))
    if denom <= 0:
        return float(lo)
    rho = float(np.dot(r[:-1], r[1:]) / denom)
    if rho <= 0:
        return float(lo)
    if rho >= 1:
        return float(hi)
    return float(min(max(np.log(2.0) / (-np.log(rho)), lo), hi))

def series_for_strategy(strategy_id, conn):
    cur = conn.cursor()
    cur.execute("""select pnl_pct from strategy_backtest_trades
                   where strategy_id=%s and pnl_pct is not null order by exit_date nulls last, id""",
                (strategy_id,))
    rows = [float(x[0]) for x in cur.fetchall()]
    if len(rows) < 8:
        cur.execute("""select realized_pnl_pct from signal_pnl
                       where strategy_id=%s and realized_pnl_pct is not null order by closed_at nulls last""",
                    (strategy_id,))
        rows = [float(x[0]) for x in cur.fetchall()]
    return np.array(rows, float)

def half_life_for(strategy_id, conn, mode, cadence_days):
    if mode == "cadence":
        return float(cadence_days)
    est = autocorr_half_life(series_for_strategy(strategy_id, conn))
    return float(cadence_days) if not np.isfinite(est) else est
```

At Step 3, verify `strategy_backtest_trades` column names (`exit_date`/`id`/`pnl_pct`) and `signal_pnl` (`closed_at`/`realized_pnl_pct`) with `\d` first; adapt the ORDER BY/columns to the real schema.

- [ ] **Step 4: Run to verify pass** — same pytest → PASS (3 passed).

- [ ] **Step 5: Commit** — `git add harness/half_life.py harness/tests/test_half_life.py && git commit -m "feat(exit-tdom): autocorrelation half-life estimator + cadence passthrough"`

---

### Task 4: Cluster extraction + input mapping (`inputs.py`)

**Files:**
- Create: `harness/inputs.py`
- Test: `harness/tests/test_inputs.py`

**Interfaces:**
- Consumes: `execution_signals`; `strategy_weights.load_current(regime)`; `strategy_similarity.load_groups(regime)['matrix']`; `prices.atr`; `half_life.half_life_for`; `ticker_metadata_snapshots.easy_to_borrow`.
- Produces:
  - `Cluster = dataclass(day:str, ticker:str, direction:int, entry:float, legs:list[dict(strategy_id,stop_loss,target_1,target_2)], regime:str, easy_to_borrow:bool)`.
  - `extract_clusters(conn, window_start="2026-05-04", min_legs=2) -> list[Cluster]` — group `execution_signals` by `(coalesce(target_date,signal_date), ticker, dsign(direction))` over the window; **drop `direction='FLAT'`**; keep groups with `>= min_legs` legs; entry = top-`effective_sharpe` leg's `entry_price`.
  - `build_context(cluster, regime_tables, conn, half_life_mode, carry_mode, atr_value) -> (list[Strategy], Context)` — maps each leg's strategy to `Strategy(sharpe=effective_sharpe, half_life=half_life_for(...), confidence=daily_weight, direction=cluster.direction)`; `C` = `matrix` sliced to the legs (diag 1.0; missing pair → off-diagonal default 0.05, matching the live `strategy_similarity` sparse default); `Context(C, sigma_underlying=atr_value, entry_price=cluster.entry, hurdle_g_star=0.0, kelly_fraction=0.5, session_bars=None, txn_cost=<config>)`; for shorts set `ctx.carry_per_bar = carry_for(cluster, carry_mode)`.
  - `carry_for(cluster, carry_mode) -> float` — `carry_mode="zero"`→0.0; `carry_mode="tiered"`→ `−(borrow+div)/252` with borrow=0.003 if `easy_to_borrow` else 0.05, div default 0.0 (or sourced est.). Longs: 0.0.
  - `dsign(direction:str)->int` — `+1` if upper in `{LONG,BUY,BUY_VOL}` else `−1`; `FLAT` excluded upstream.

- [ ] **Step 1: Write the failing test** (mostly synthetic; one tiny DB read)

```python
# harness/tests/test_inputs.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import inputs

def test_dsign():
    assert inputs.dsign("LONG") == 1 and inputs.dsign("BUY_VOL") == 1
    assert inputs.dsign("SHORT") == -1 and inputs.dsign("SELL") == -1

def test_carry_tiered_sign_and_scale():
    class Cl: pass
    c = Cl(); c.direction = -1; c.easy_to_borrow = True
    z = inputs.carry_for(c, "zero"); t = inputs.carry_for(c, "tiered")
    assert z == 0.0 and t < 0 and abs(t) < 1e-3      # GC ~0.3%/yr per bar is tiny
    c.easy_to_borrow = False
    assert inputs.carry_for(c, "tiered") < t          # HTB more negative
    c.direction = 1
    assert inputs.carry_for(c, "tiered") == 0.0       # longs: no borrow

def test_C_slice_is_psd_like_and_bounded():
    M = {"a": {"a":1.0,"b":0.3}, "b":{"a":0.3,"b":1.0}}
    C = inputs.slice_C(["a","b"], M)
    assert C.shape == (2,2) and C[0,1] == 0.3 and C[0,0] == 1.0
    C2 = inputs.slice_C(["a","x"], M)                 # missing pair -> default
    assert C2[0,1] == 0.05
```

- [ ] **Step 2: Run to verify it fails** — pytest test_inputs → FAIL.

- [ ] **Step 3: Implement** (`dsign`, `carry_for`, `slice_C`, `extract_clusters`, `build_context`)

```python
# harness/inputs.py  (key functions; DB-touching parts read-only)
from dataclasses import dataclass
import numpy as np

DEF_OFFDIAG = 0.05  # matches strategy_similarity sparse default

def dsign(d): return 1 if str(d).upper() in ("LONG","BUY","BUY_VOL") else -1

def carry_for(cluster, carry_mode, div_yield=0.0):
    if int(cluster.direction) != -1 or carry_mode == "zero":
        return 0.0
    borrow = 0.003 if getattr(cluster, "easy_to_borrow", True) else 0.05
    return -(borrow + div_yield) / 252.0

def slice_C(ids, matrix):
    n = len(ids); C = np.full((n, n), DEF_OFFDIAG, float); np.fill_diagonal(C, 1.0)
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j: continue
            v = (matrix.get(a, {}) or {}).get(b)
            if v is not None: C[i, j] = float(v)
    return 0.5 * (C + C.T)  # symmetrize

@dataclass
class Cluster:
    day: str; ticker: str; direction: int; entry: float
    legs: list; regime: str; easy_to_borrow: bool
# extract_clusters + build_context per the Interfaces block; see design doc §3/§7.
```

Implement `extract_clusters` with the grouping query from `/tmp/combine_backtest.py:24-37` (reuse the `coalesce(target_date,signal_date)` day key, the not-null bracket filter), adding the `direction != 'FLAT'` filter and `min_legs`. Implement `build_context` per the Interfaces signatures. Confirm `ticker_metadata_snapshots` has a current `easy_to_borrow` per ticker; if absent default `True` (GC) and log.

- [ ] **Step 4: Run to verify pass** — pytest test_inputs → PASS.

- [ ] **Step 5: Commit** — `git add harness/inputs.py harness/tests/test_inputs.py && git commit -m "feat(exit-tdom): cluster extraction + spec input mapping (C-slice, carry, half-life)"`

---

### Task 5: Baselines (`baselines.py`)

**Files:**
- Create: `harness/baselines.py`
- Test: `harness/tests/test_baselines.py`

**Interfaces:**
- Consumes: a `Cluster` (Task 4), per-leg `(stop_loss,target_1)`, `daily_weight`/`effective_sharpe` maps, ATR, `entry`, `direction`.
- Produces three functions each returning a `Policy` (same shape as `generator.generate`, `diagnostics={}`):
  - `min_stop_cumulative(cluster, weights) -> Policy` — stop_dist = `min_i |entry − stop_loss_i|`; takes = one tranche per leg at `|target_1_i − entry|`, each `fraction = 1/N`; `time_stop_bars = H_max` (no native time-stop).
  - `conf_weighted_atr(cluster, weights, sharpe, atr) -> Policy` — stop_dist = `(Σ w_i·m_i)·atr`, `m_i=|entry−stop_loss_i|/atr`, `w_i=daily_weight` normalized; one take at `Σ (sharpe_i/Σsharpe)·|target_1_i−entry|`; `time_stop_bars=H_max`.
  - `current_live_v2(cluster, weights) -> Policy` — stop_dist = `min_i |entry−stop_loss_i|`; takes = single tranche at uncapped `Σ_i |target_1_i−entry|`; `time_stop_bars=H_max`. (informational)
  - All are **distance-only** (direction handled by the replay via `direction`); `H_max` from config (default 30).

- [ ] **Step 1: Write failing test** (hand-computed 2-leg cluster)

```python
# harness/tests/test_baselines.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import baselines as B

class Cl:  # minimal cluster stub
    def __init__(s):
        s.entry=100.0; s.direction=1
        s.legs=[dict(strategy_id="a", stop_loss=98.0, target_1=104.0),
                dict(strategy_id="b", stop_loss=95.0, target_1=110.0)]

W = {"a":0.6, "b":0.4}; SH = {"a":2.0, "b":1.0}

def test_min_stop_cumulative():
    p = B.min_stop_cumulative(Cl(), W, H_max=30)
    assert abs(p["stop_dist"] - 2.0) < 1e-9          # min(|100-98|,|100-95|)
    assert {round(t["distance"],3) for t in p["takes"]} == {4.0, 10.0}
    assert all(abs(t["fraction"]-0.5)<1e-9 for t in p["takes"])

def test_conf_weighted_atr():
    p = B.conf_weighted_atr(Cl(), W, SH, atr=2.5, H_max=30)
    # stop = (0.6*(2/2.5)+0.4*(5/2.5))*2.5 = (0.6*0.8+0.4*2.0)*2.5 = (0.48+0.8)*2.5 = 3.2
    assert abs(p["stop_dist"] - 3.2) < 1e-9
    # take = (2/3)*4 + (1/3)*10 = 2.667+3.333 = 6.0
    assert abs(p["takes"][0]["distance"] - 6.0) < 1e-9

def test_v2_uncapped_sum():
    p = B.current_live_v2(Cl(), W, H_max=30)
    assert abs(p["takes"][0]["distance"] - 14.0) < 1e-9   # 4 + 10
    assert abs(p["stop_dist"] - 2.0) < 1e-9
```

- [ ] **Step 2: Run to verify fail** — pytest test_baselines → FAIL.

- [ ] **Step 3: Implement** the three functions exactly to satisfy the hand-computed values above (distances are absolute `|level − entry|`; `time_stop_bars=H_max`; `direction` copied from cluster; `diagnostics={}`).

- [ ] **Step 4: Run to verify pass** — pytest test_baselines → PASS.

- [ ] **Step 5: Commit** — `git add harness/baselines.py harness/tests/test_baselines.py && git commit -m "feat(exit-tdom): three exit baselines (min-stop/cumulative, conf-ATR, live V2)"`

---

### Task 6: Realized replay engine (`replay.py`)

**Files:**
- Create: `harness/replay.py`
- Test: `harness/tests/test_replay.py`

**Interfaces:**
- Consumes: a `Policy` (stop_dist, takes[{distance,fraction}], time_stop_bars, direction), a daily OHLC `DataFrame` starting at the entry bar, `entry`, `carry_per_bar` (0 for longs/zero-arm).
- Produces:
  - `first_touch_multiday(policy, bars, entry, carry_per_bar=0.0) -> dict(R, tau, exit_kind, frac_filled)` where:
    - Walk daily bars from the bar **after** entry (fills at signal close[t], excursions from t+1) up to `min(time_stop_bars, H_max, len(bars))`.
    - Excursion in price = `direction*(price − entry)`. Stop level distance `a`, takes at distances `b_k` (favorable). **Stop-wins-on-tie** within a bar (if both stop and a take are inside `[low,high]`, the stop fills). Gap-through: if a bar's range jumps past a level, fill **at the level** (conservative).
    - Partial takes release `fraction` of remaining position when the bar's favorable extreme reaches `b_k` **or** the bar index reaches the take's `time_bars` (whichever first), once each.
    - On stop: close full remainder at `−a` excursion.
    - On reaching the horizon with remainder: mark remainder at the last close's excursion.
    - **Carry:** subtract `|carry_per_bar| * bars_held` from the position's realized excursion-return (charged on the held fraction over time; for shorts only). `R` is returned in σ units by the caller (Task 7) — here return raw signed return-on-entry `R_ret = Σ frac_k * excursion_k / entry − carry_drag`; `tau` = bar index of final close.
  - Convention helpers exposed for tests: `_touch(bar, level, side)`.

- [ ] **Step 1: Write failing tests** (hand-built bar fixtures)

```python
# harness/tests/test_replay.py
import sys, os, pandas as pd
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import replay as R

def _bars(rows):  # rows: list of (high, low, close)
    idx = pd.date_range("2026-05-04", periods=len(rows), freq="D")
    return pd.DataFrame(rows, columns=["high","low","close"], index=idx)

def P(stop, takes, tsb=30, d=1):
    return dict(stop_dist=stop, takes=[dict(distance=b, fraction=f) for b,f in takes],
               time_stop_bars=tsb, direction=d)

def test_take_only_full():
    bars = _bars([(101,99,100),(106,100,105)])   # long, entry 100
    o = R.first_touch_multiday(P(5,[(5,1.0)]), bars, 100.0)
    assert o["exit_kind"] == "take" and abs(o["R"] - 0.05) < 1e-9 and o["tau"] == 1

def test_stop_only():
    bars = _bars([(101,99,100),(101,94,95)])      # hits -5
    o = R.first_touch_multiday(P(5,[(20,1.0)]), bars, 100.0)
    assert o["exit_kind"] == "stop" and abs(o["R"] + 0.05) < 1e-9

def test_stop_wins_on_tie():
    bars = _bars([(101,99,100),(106,94,100)])      # both +5 take and -5 stop in one bar
    o = R.first_touch_multiday(P(5,[(5,1.0)]), bars, 100.0)
    assert o["exit_kind"] == "stop"

def test_partial_take_then_timestop():
    bars = _bars([(101,99,100),(106,100,105),(105,101,103),(104,102,103)])
    o = R.first_touch_multiday(P(20,[(5,0.5)], tsb=3), bars, 100.0)
    assert 0 < o["frac_filled"] < 1.0001 and o["exit_kind"] in ("time","take")
    # 0.5 booked at +5%, 0.5 marked at close[3]=+3%  -> ~ 0.5*0.05+0.5*0.03 = 0.04
    assert abs(o["R"] - 0.04) < 2e-3

def test_short_carry_reduces_return():
    bars = _bars([(101,99,100)] + [(101,99,100)]*10)   # flat, short, no touch
    base = R.first_touch_multiday(P(20,[(20,1.0)], tsb=10, d=-1), bars, 100.0, carry_per_bar=0.0)
    carr = R.first_touch_multiday(P(20,[(20,1.0)], tsb=10, d=-1), bars, 100.0, carry_per_bar=-0.001)
    assert carr["R"] < base["R"] - 1e-6
```

- [ ] **Step 2: Run to verify fail** — pytest test_replay → FAIL.

- [ ] **Step 3: Implement** `first_touch_multiday` exactly to satisfy the fixtures: direction-signed excursions, stop-wins-on-tie, gap-fill-at-level, ordered partial takes with price-or-time trigger, horizon mark, and carry drag `|carry|*tau` subtracted from `R`. Document the fill conventions in the docstring.

- [ ] **Step 4: Run to verify pass** — pytest test_replay → PASS (5 passed).

- [ ] **Step 5: Commit** — `git add harness/replay.py harness/tests/test_replay.py && git commit -m "feat(exit-tdom): daily multi-day first-touch replay (tie/gap/partial/carry)"`

---

### Task 7: Growth + block bootstrap (`growth.py`)

**Files:**
- Create: `harness/growth.py`
- Test: `harness/tests/test_growth.py`

**Interfaces:**
- Consumes: per-trade records `list[dict(day:str, R_ret:float, tau:float, sigma_ret:float)]` (`sigma_ret = atr/entry`, the per-trade σ in return units), φ.
- Produces:
  - `growth_G(trades, phi=0.5) -> float` — `R_i = R_ret_i / sigma_ret_i` (PnL in σ units); `G = mean_i[ ln(clip(1+phi*R_i, 1e-6, None)) ] / mean_i[ tau_i ]`.
  - `bootstrap_delta(trades_A, trades_B, phi=0.5, n_boot=2000, seed=0) -> dict(delta, lo, hi, p_gt0)` — A and B are the SAME trades under two policies, aligned by index; **block bootstrap by `day`**: resample the set of distinct days with replacement, gather all trades on the drawn days for both A and B, recompute `G_A−G_B`; 95% percentile CI `[lo,hi]`, `p_gt0` = fraction of resamples with `ΔG>0`. Returns the point `delta = G(A)−G(B)` on the full sample.

- [ ] **Step 1: Write failing test**

```python
# harness/tests/test_growth.py
import sys, os, numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import growth as G

def _t(day, R, tau, s=0.02): return dict(day=day, R_ret=R, tau=tau, sigma_ret=s)

def test_growth_hand_value():
    tr = [_t("d1", 0.02, 5.0), _t("d1", 0.0, 10.0)]   # R = 1.0, 0.0 ; tau 5,10
    # mean ln(1+0.5*[1,0]) = (ln1.5+ln1)/2 = 0.2027; mean tau = 7.5 ; G=0.02703
    assert abs(G.growth_G(tr) - 0.027031) < 1e-4

def test_bootstrap_brackets_point():
    rng = np.random.default_rng(0)
    A = [_t(f"d{i%20}", 0.03+0.001*rng.standard_normal(), 6.0) for i in range(400)]
    B = [_t(f"d{i%20}", 0.01+0.001*rng.standard_normal(), 6.0) for i in range(400)]
    out = G.bootstrap_delta(A, B, n_boot=500, seed=1)
    assert out["lo"] <= out["delta"] <= out["hi"]
    assert out["delta"] > 0 and out["lo"] > 0           # A clearly dominates
    assert 0.0 <= out["p_gt0"] <= 1.0

def test_degenerate_zero_width():
    tr = [_t("d1", 0.02, 5.0)]
    out = G.bootstrap_delta(tr, tr, n_boot=50, seed=0)
    assert abs(out["delta"]) < 1e-12 and abs(out["hi"]-out["lo"]) < 1e-9
```

- [ ] **Step 2: Run to verify fail** — pytest test_growth → FAIL.

- [ ] **Step 3: Implement** `growth_G` and `bootstrap_delta` exactly per the Interfaces (day-block resampling; clip 1e-6; ratio recomputed each resample).

- [ ] **Step 4: Run to verify pass** — pytest test_growth → PASS (3 passed).

- [ ] **Step 5: Commit** — `git add harness/growth.py harness/tests/test_growth.py && git commit -m "feat(exit-tdom): growth objective + day-block bootstrap CI"`

---

### Task 8: Orchestrator + floor-pin probe + report (`run_tdom.py`)

**Files:**
- Create: `harness/run_tdom.py`
- Create: `harness/out/.gitkeep`
- Test: `harness/tests/test_smoke.py`

**Interfaces:**
- Consumes: everything above + DB + prices.
- Produces:
  - `floor_pin_probe(clusters, regime_tables, conn, n=50, seed=0) -> dict(frac_at_floor, interior_examples)` — run `generator.generate` on `n` real clusters; report fraction whose `diagnostics.a_mult` equals the grid floor; **logged**, decides whether MC is needed.
  - `run(window_start, half_life_mode, carry_mode, n_clusters=None, txn_cost_sigma=0.02, H_max=30, seed=0) -> dict` — full pipeline: extract clusters → for each, build inputs, generate ensemble + 3 baselines, replay all on the same sliced daily bars (charging carry on shorts), assemble per-trade records, compute `growth_G` per policy and `bootstrap_delta(ensemble, baseline)` for each baseline, split long/short/combined. Emits `harness/out/tdom_<mode>.json`.
  - `main()` — runs the matrix {half_life: autocorr,cadence} × {carry: tiered,zero} (carry arm only changes shorts), plus the floor-pin probe once; writes a markdown report `harness/out/REPORT.md`.

- [ ] **Step 1: Write smoke test** (tiny real slice, ~30 clusters, bounded)

```python
# harness/tests/test_smoke.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import run_tdom

def test_smoke_30_clusters_completes():
    out = run_tdom.run(window_start="2026-05-04", half_life_mode="cadence",
                       carry_mode="zero", n_clusters=30, seed=0)
    assert "combined" in out and "min_stop_cumulative" in out["combined"]
    d = out["combined"]["min_stop_cumulative"]
    assert set(["delta","lo","hi"]).issubset(d)
    assert out["n_trades"] >= 20
```

- [ ] **Step 2: Run to verify fail** — `nice -n 19 python3 -m pytest harness/tests/test_smoke.py -q` → FAIL (no module).

- [ ] **Step 3: Implement** `floor_pin_probe`, `run`, `main`. Load `strategy_weights.load_current(regime)` and `strategy_similarity.load_groups(regime)` once per regime (cache). Slice prices per batch of cluster tickers over `[window_start − 60d, today]`. Skip clusters whose ATR is `nan` or whose price slice is empty (log counts). Build per-trade `sigma_ret = atr/entry`.

- [ ] **Step 4: Run smoke to verify pass** — pytest test_smoke → PASS. Watch RSS stays < 1 GB (the sweep needs the headroom): `nice -n 19 /usr/bin/time -v python3 -m pytest harness/tests/test_smoke.py -q 2>&1 | grep Maximum`.

- [ ] **Step 5: Commit** — `git add harness/run_tdom.py harness/out/.gitkeep harness/tests/test_smoke.py && git commit -m "feat(exit-tdom): orchestrator, floor-pin probe, smoke test"`

---

### Task 9: Full run + report

**Files:**
- Modify: none (uses Task 8 `main`)
- Create: `harness/out/REPORT.md` (generated)

- [ ] **Step 1: Pre-flight resource check**

Run: `free -m | awk 'NR==2{print "MemAvailable_used_proxy free=",$4}'; ps -o rss=,args= -C python3 | sort -rn | head`
Expected: confirm the calendar sweep is the only heavy python and `MemAvailable` (from `cat /proc/meminfo | grep MemAvailable`) > 4.5 GB. **Do not run during 13:30–15:30 UTC tomorrow** (C3 guard window). If the sweep is mid-run, the harness still fits (sliced prices, single core), but keep an eye on free mem.

- [ ] **Step 2: Run the floor-pin probe standalone first**

Run: `cd <worktree> && nice -n 19 python3 -c "import sys; sys.path.insert(0,'harness'); import run_tdom, psycopg2, os; from dotenv import load_dotenv; load_dotenv('/root/openclaw/.env'); conn=psycopg2.connect(os.environ['POSTGRES_URI']); cl=__import__('inputs').extract_clusters(conn); print(run_tdom.floor_pin_probe(cl, None, conn, n=50))"`
Expected: `frac_at_floor` reported. If ≈1.0 → deterministic levels confirmed (no MC needed). If < ~0.8 → note interior cases in the report (MC already runs inside the generator, so results stay correct; just flag it).

- [ ] **Step 3: Run the full matrix**

Run: `cd <worktree> && nice -n 19 python3 -m harness.run_tdom` (or `python3 harness/run_tdom.py`)
Expected: writes `harness/out/tdom_*.json` + `harness/out/REPORT.md` with, per arm and per long/short/combined: `G` per policy; `ΔG` + 95% CI vs each baseline; floor-pin fraction; stopout/take-hit/hold distributions; kappa/fallback rates; the carry caveat.

- [ ] **Step 4: Verify the verdict logic**

Confirm the report states the **T-DOM gate** explicitly: adopt iff CI `lo > 0` vs BOTH `min_stop_cumulative` AND `conf_weighted_atr`, on the **primary arm** (autocorr half-life, tiered carry), with the sensitivity arms shown alongside. Never a bare point estimate.

- [ ] **Step 5: Commit the report**

```bash
git add harness/out/REPORT.md harness/out/tdom_*.json && git commit -m "results(exit-tdom): T-DOM verdict + distributions across arms"
```

---

## Self-Review

**Spec coverage:** Tasks map to design-doc sections — §4 architecture (Tasks 1–8), §3 input mapping (Tasks 3–4), §6 T-DOM methodology (Task 7), §7 baselines (Task 5), replay (Task 6), shorts/carry D1–D3 (Tasks 1,4,6), Step-0 floor-pin (Task 8/9), report+caveats (Task 9). Synthetic harness reproduction already done pre-plan.

**Placeholder scan:** No "TBD"/"handle edge cases" — each implementation step names the exact behavior and test values. The two schema-confirm notes (parquet columns, trade-table columns) are explicit "verify with `\d` then adapt" steps, not placeholders.

**Type consistency:** `Policy` shape is identical across `generator.generate` and `baselines.*`; `first_touch_multiday` returns `{R,tau,exit_kind,frac_filled}`; growth consumes `{day,R_ret,tau,sigma_ret}` — the orchestrator (Task 8) is the single place that adapts replay output (`R` as return) into growth input (`R_ret`,`sigma_ret`) — note: replay returns key `R` (return-on-entry) which Task 8 stores as `R_ret`, and pairs it with `sigma_ret=atr/entry`. This adapter is called out in Task 8 Step 3.

**Out of scope (unchanged):** live wiring, Phase-2 A1–A4, sizing.
