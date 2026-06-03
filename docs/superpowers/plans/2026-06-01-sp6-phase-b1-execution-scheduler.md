# SP-6 Phase B1 — Intraday Execution Scheduler (Shadow Build) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an observation-only layer that measures whether working an open order along 9:30→close (VWAP base + sᵢ-decay front-loading) beats `close[T+1]` after costs — a go/no-go for a future live executor.

**Architecture:** Five small, isolated units behind the default-OFF gate `OPENCLAW_B1_SHADOW`: a pure participation-curve planner, a pure shadow simulator (with a conservative haircut), an order-source adapter (case-study from `signal_pnl`, live from `alpaca_submissions`), a shadow ledger+report (own table, Discord via `requests`), and a CLI driver. Zero changes to the live trading path. Spec: `docs/superpowers/specs/2026-06-01-sp6-phase-b1-execution-scheduler-design.md`.

**Tech Stack:** Python 3.13, `psycopg2` (inline `psycopg2.connect(os.environ['POSTGRES_URI'])` — the repo's convention; no shared helper), `pyarrow`/`pandas` for `data/master/prices_30m.parquet`, `requests` for Discord, `pytest`.

**Conventions grounded in the codebase:** RTH = **13** 30-min buckets (9:30→16:00). Order dict shape (shared across tasks): `{ticker, signed_qty (+long/−short), s_i∈[0,1], run_date 'YYYY-MM-DD' (=T+1), naive_fill|None, close_t1|None, strategy_id, regime_state, signal_id?, resolve_next_session:bool}`. Bars dict: `{bucket:int → {vwap,high,low,volume}}`. Plan: `list[(bucket:int, slice_qty:float)]` summing to `signed_qty`. Gate check: `os.environ.get('OPENCLAW_B1_SHADOW') == '1'`.

---

## ⚠️ GROUNDING CORRECTIONS (2026-06-02) — apply BEFORE implementing

Verified against the live DB + filesystem during kickoff. These override the original task text where they conflict.

**C1 — Task 4 join is wrong.** `execution_signals` has **no `signal_id` column** (its PK is `id`, type **uuid**); `signal_pnl.signal_id` (uuid) is the FK → `execution_signals.id`. Fix the join to `JOIN execution_signals es ON es.id = sp.signal_id` and select `es.id::text AS signal_id`. (Migration 128's `signal_id UUID` is **correct** — both ids are uuid. No change to Task 1.) `signal_pnl.status` values are `'open'`/`'closed'` (5,771 closed); `realized_pnl_pct` is numeric. ✓

**C2 — `target_date` is NULL for all history.** `execution_signals.target_date` is the brand-new SP-6 EOD-lane column; it is populated only for signals computed since 2026-06-01. **All 5,771 historical closed signals have `target_date = NULL`**, so the original `WHERE es.target_date IS NOT NULL` returns **zero** case studies. Use **`es.signal_date`** instead (drop the target_date filter). The worked session = **the first bar-session strictly after `signal_date`** — this *reconstructs* the now-NULL `target_date` (which, when populated, IS T+1 = first session after signal_date), so we measure B1's actual question: "does working the **T+1** open beat dumping at close[T+1]." Implement via an order field `resolve_next_session: True` for case studies (`run_date = signal_date.isoformat()`), `False` for live shadow (`run_date` = the exact T+1 from `alpaca_submissions.run_date`). In the driver, build the ticker's full `by_date` map and pick the session: `resolve_next_session` ⇒ `min(d for d in by_date if d > run_date)`; else ⇒ `run_date if run_date in by_date else None` (skip, never impute).

**C3 — Task 7's Polygon ingester is DEAD; build a NEW Alpaca one.** Both `src/ingestion/ingest_prices_30m.py` and `fetch_30m_bars.py` are Polygon-based; **`POLYGON_API_KEY` was purged in the SP-1 cutover**, so they fetch nothing (this is why `prices_30m.parquet` froze at 2026-04-22 with only 5 tickers: AAPL/MSFT/NVDA/SPY/TSLA). Replace Task 7's backfill with a new **Alpaca** 30-min ingester. Alpaca CLI verified working: `alpaca data bars --symbol <T> --timeframe 30Min --start <d> --end <d> --feed sip` returns `{c,h,l,o,v,vw,n,t}` (incl. pre-market 08:00Z bars). Requirements:
  - **🚨 APPEND-ONLY (CLAUDE.md core invariant — `prices_30m.parquet` is NEVER-DELETE master data):** `load_existing()` → concat new → **dedup on (ticker, datetime)** → write. **NO `--rebuild`, NO truncation.** Writing only the case-study tickers must NOT drop the existing AAPL/MSFT/NVDA/SPY/TSLA history or shrink the date axis. This is a hard invariant — a naive overwrite corrupts live master data.
  - **Schema match:** map `vw→vwap`, `n→transactions`; keep `datetime` tz-aware UTC (`datetime64[us, UTC]`); **also produce the ET-calendar `date` column** (the driver keys `by_date` on `r['date']`). RTH-filter: convert `t` to `America/New_York`, keep **09:30–16:00 ET** only (drop pre/post-market), mirroring the old Polygon ingester.
  - Chunked + `nice -n 19` (2-core/8GB; weekend-OOM lesson). **Do NOT run the parquet-heavy backfill during 19:55–20:30 UTC** (today's 3:55 fill → 4:15 EOD compute live cycle).

**C4 — Worktree has no `.env`** (gitignored). DB code and the Alpaca fetch must read `POSTGRES_URI` / `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` from **`/root/openclaw/.env`** (absolute path), e.g. parse the line directly; do not assume env vars are exported in the worktree shell. `pytest.ini` has `pythonpath = src` (confirmed) → imports are `from execution.b1_X import` and tests run as plain `python3 -m pytest` from the worktree root.

**Terminal state:** standing operator constraints = no merge/push/restart. When `finishing-a-development-branch` runs, choose **keep-as-is** (branch local, gate `OPENCLAW_B1_SHADOW` OFF).

---

### Task 1: Migration 128 — `b1_shadow_exec_ledger` table

**Files:**
- Create: `src/database/migrations/128_b1_shadow_exec_ledger.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 128_b1_shadow_exec_ledger.sql
-- SP-6 Phase B1 shadow execution ledger. Observation-only; never affects trading.
CREATE TABLE IF NOT EXISTS b1_shadow_exec_ledger (
  id              BIGSERIAL PRIMARY KEY,
  mode            TEXT NOT NULL,             -- 'case_study' | 'live_shadow'
  run_date        DATE,                      -- the T+1 session worked
  ticker          TEXT NOT NULL,
  strategy_id     TEXT,
  signal_id       UUID,
  regime_state    TEXT,
  signed_qty      NUMERIC,
  s_i             NUMERIC,
  lam             NUMERIC,
  actual_fill     NUMERIC,                   -- qty-weighted sim fill (alpha plan)
  close_t1        NUMERIC,
  exec_ledger     NUMERIC,                   -- (close_t1 - actual_fill)*signed_qty (alpha plan)
  naive_ledger    NUMERIC,                   -- vs the naive 3:55 fill
  vwap_base_ledger NUMERIC,                  -- vs the lam=0 VWAP-base plan
  completion      NUMERIC,                   -- filled_qty / signed_qty
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_b1_shadow_run_date ON b1_shadow_exec_ledger (run_date);
CREATE INDEX IF NOT EXISTS ix_b1_shadow_mode ON b1_shadow_exec_ledger (mode);
```

- [ ] **Step 2: Apply it and verify (read-only check after)**

Run:
```bash
cd /root/openclaw && python3 - <<'PY'
import os, psycopg2
uri=os.environ['POSTGRES_URI']
c=psycopg2.connect(uri); cur=c.cursor()
cur.execute(open('src/database/migrations/128_b1_shadow_exec_ledger.sql').read()); c.commit()
cur.execute("SELECT to_regclass('public.b1_shadow_exec_ledger')")
print('table:', cur.fetchone()[0]); c.close()
PY
```
Expected: `table: b1_shadow_exec_ledger`

- [ ] **Step 3: Commit**

```bash
git add src/database/migrations/128_b1_shadow_exec_ledger.sql
git commit -m "feat(b1): migration 128 — b1_shadow_exec_ledger table"
```

---

### Task 2: `b1_planner.py` — expected volume profile + participation curve (pure)

**Files:**
- Create: `src/execution/b1_planner.py`
- Test: `tests/test_b1_planner.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_b1_planner.py
import math
from execution.b1_planner import expected_volume_profile, plan, RTH_BUCKETS, U_SHAPE

def test_profile_fallback_u_shape_when_no_history():
    assert expected_volume_profile([]) == U_SHAPE

def test_profile_is_normalized_and_flat_for_flat_days():
    prof = expected_volume_profile([[10.0]*RTH_BUCKETS, [20.0]*RTH_BUCKETS])
    assert abs(sum(prof) - 1.0) < 1e-9
    assert all(abs(p - 1.0/RTH_BUCKETS) < 1e-9 for p in prof)

def test_plan_slices_sum_to_qty():
    prof = expected_volume_profile([])
    assert abs(sum(q for _, q in plan(1000.0, 0.8, prof, 2.0)) - 1000.0) < 1e-6
    assert abs(sum(q for _, q in plan(-500.0, 0.8, prof, 2.0)) + 500.0) < 1e-6

def test_lam_zero_is_pure_base():
    prof = expected_volume_profile([])
    for t, q in plan(1000.0, 1.0, prof, 0.0):
        assert abs(q - 1000.0 * prof[t]) < 1e-6

def test_si_zero_is_pure_base():
    prof = expected_volume_profile([])
    for t, q in plan(1000.0, 0.0, prof, 5.0):
        assert abs(q - 1000.0 * prof[t]) < 1e-6

def test_higher_si_front_loads():
    prof = expected_volume_profile([])
    early = lambda sl: sum(q for t, q in sl if t < 3)
    assert early(plan(1000.0, 0.9, prof, 3.0)) > early(plan(1000.0, 0.1, prof, 3.0))
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_planner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b1_planner'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/execution/b1_planner.py
"""B1 participation-curve planner (pure). VWAP-volume base modulated by sᵢ-decay
front-loading: slice(t) ∝ profile[t]·exp(-lam·s_i·t). Consumes ONLY ≤9:30 info
(an EXPECTED volume profile), never today's realized volume."""
from __future__ import annotations
import math

RTH_BUCKETS = 13  # 30-min buckets 9:30..16:00
# Market-wide intraday U-shape fallback (sums to 1.0), 13 buckets.
U_SHAPE = [0.13, 0.09, 0.07, 0.06, 0.055, 0.05, 0.05,
           0.05, 0.055, 0.06, 0.07, 0.085, 0.135]


def expected_volume_profile(history_days):
    """history_days: list of per-day lists of RTH_BUCKETS volumes (trailing N days,
    EXCLUDING the day being planned — causal). Returns a length-RTH_BUCKETS profile
    summing to 1.0; falls back to U_SHAPE when history is empty/degenerate."""
    sums = [0.0] * RTH_BUCKETS
    n = 0
    for day in history_days or []:
        if not day or len(day) != RTH_BUCKETS:
            continue
        tot = sum(day)
        if tot <= 0:
            continue
        for i, v in enumerate(day):
            sums[i] += v / tot
        n += 1
    if n == 0:
        return list(U_SHAPE)
    prof = [s / n for s in sums]
    tot = sum(prof)
    return [p / tot for p in prof] if tot > 0 else list(U_SHAPE)


def plan(signed_qty, s_i, profile, lam):
    """signed_qty: +long/-short. s_i in [0,1]. profile: length-RTH_BUCKETS expected
    fractions. lam>=0. Returns [(bucket, slice_qty)] summing to signed_qty. lam=0 or
    s_i=0 ⇒ pure VWAP base. Pure: only `profile` is used (no realized bars)."""
    k = len(profile)
    w = [profile[t] * math.exp(-lam * s_i * t) for t in range(k)]
    tot = sum(w)
    if tot <= 0:
        w, tot = list(profile), (sum(profile) or 1.0)
    return [(t, signed_qty * w[t] / tot) for t in range(k)]
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_planner.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/b1_planner.py tests/test_b1_planner.py
git commit -m "feat(b1): pure participation-curve planner (VWAP base + sᵢ-decay)"
```

---

### Task 3: `b1_simulator.py` — shadow execution simulator (pure)

**Files:**
- Create: `src/execution/b1_simulator.py`
- Test: `tests/test_b1_simulator.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_b1_simulator.py
from execution.b1_simulator import simulate

def _bars(vwaps):  # uniform tight bars (haircut≈impact only)
    return {i: {'vwap': v, 'high': v, 'low': v, 'volume': 100.0} for i, v in enumerate(vwaps)}

def test_buy_filled_below_close_is_positive_ledger():
    plan = [(0, 50.0), (1, 50.0)]
    bars = _bars([99.0, 99.0])
    r = simulate(plan, bars, close_t1=100.0, naive_fill=100.0, impact_bps=0.0)
    assert r['actual_fill'] == 99.0
    assert r['exec_ledger'] > 0          # bought at 99 vs close 100
    assert abs(r['completion'] - 1.0) < 1e-9

def test_haircut_is_adverse_for_buyer():
    plan = [(0, 100.0)]
    bars = {0: {'vwap': 100.0, 'high': 100.5, 'low': 99.5, 'volume': 1.0}}
    r = simulate(plan, bars, close_t1=100.0, impact_bps=0.0)
    assert r['actual_fill'] > 100.0      # buyer pays vwap + half-spread

def test_missing_bar_reduces_completion_and_is_skipped():
    plan = [(0, 50.0), (1, 50.0)]
    bars = {0: {'vwap': 100.0, 'high': 100.0, 'low': 100.0, 'volume': 1.0}}  # bucket 1 absent
    r = simulate(plan, bars, close_t1=100.0, impact_bps=0.0)
    assert abs(r['completion'] - 0.5) < 1e-9
    assert r['filled_qty'] == 50.0

def test_no_fills_returns_zero_ledger():
    r = simulate([(0, 10.0)], {}, close_t1=100.0)
    assert r['filled_qty'] == 0.0 and r['exec_ledger'] == 0.0 and r['actual_fill'] is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_simulator.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b1_simulator'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/execution/b1_simulator.py
"""B1 shadow simulator (pure). Fills each plan slice at its bucket's realized VWAP
minus a conservative haircut (adverse to us), then computes the execution ledger vs
close[T+1] and the naive-3:55 baseline. Buckets without a bar are skipped (lower
completion) — never imputed. Returns floats only; raises nothing."""
from __future__ import annotations

_HAIRCUT_CAP_BPS = 50.0


def _haircut_px(bar, impact_bps):
    """Half-spread proxy (from bar high-low) + fixed impact, as a price delta."""
    v = bar.get('vwap', 0.0)
    if v <= 0:
        return 0.0
    half_spread_bps = 0.5 * (bar.get('high', v) - bar.get('low', v)) / v * 1e4
    bps = min(half_spread_bps, _HAIRCUT_CAP_BPS) + impact_bps
    return bps / 1e4 * v


def simulate(plan_slices, realized_bars, close_t1, naive_fill=None, impact_bps=2.0):
    """plan_slices: [(bucket, slice_qty)]. realized_bars: {bucket:{vwap,high,low,volume}}.
    close_t1: official close. naive_fill: actual 3:55 fill (defaults to close_t1).
    Returns {actual_fill, exec_ledger, naive_ledger, completion, filled_qty}."""
    signed_qty = sum(q for _, q in plan_slices)
    side = 1.0 if signed_qty >= 0 else -1.0   # buyer pays more, seller receives less
    filled_notional = 0.0
    filled_qty = 0.0
    for bucket, q in plan_slices:
        bar = realized_bars.get(bucket)
        if not bar or bar.get('vwap', 0.0) <= 0:
            continue
        fill_px = bar['vwap'] + side * _haircut_px(bar, impact_bps)
        filled_notional += q * fill_px
        filled_qty += q
    if filled_qty == 0:
        return {'actual_fill': None, 'exec_ledger': 0.0, 'naive_ledger': 0.0,
                'completion': 0.0, 'filled_qty': 0.0}
    actual_fill = filled_notional / filled_qty
    naive = naive_fill if naive_fill is not None else close_t1
    return {'actual_fill': actual_fill,
            'exec_ledger': (close_t1 - actual_fill) * filled_qty,
            'naive_ledger': (close_t1 - naive) * filled_qty,
            'completion': abs(filled_qty / signed_qty) if signed_qty else 0.0,
            'filled_qty': filled_qty}
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_simulator.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/b1_simulator.py tests/test_b1_simulator.py
git commit -m "feat(b1): pure shadow execution simulator with conservative haircut"
```

---

### Task 4: `b1_order_source.py` — order adapters (case-study + live)

**Files:**
- Create: `src/execution/b1_order_source.py`
- Test: `tests/test_b1_order_source.py`

- [ ] **Step 1: Write the failing tests** (pure helpers only; DB functions are smoke-tested in Task 7)

```python
# tests/test_b1_order_source.py
from execution.b1_order_source import si_from_pct, SIZE_CAP

def test_si_normalizes_to_unit_interval():
    assert si_from_pct(None) == 0.0
    assert si_from_pct(0.0) == 0.0
    assert abs(si_from_pct(SIZE_CAP) - 1.0) < 1e-9
    assert si_from_pct(SIZE_CAP * 2) == 1.0      # clamps at 1
    assert abs(si_from_pct(SIZE_CAP / 2) - 0.5) < 1e-9
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_order_source.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b1_order_source'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/execution/b1_order_source.py
"""Where the order to 'work' comes from. NO re-sizing — B1 measures timing of the
same qty. live_shadow_orders: today's actual opens (alpaca_submissions).
case_study_orders: curated historical opens where execution mattered
(signal_pnl ⨝ execution_signals)."""
from __future__ import annotations
import os
import psycopg2
import psycopg2.extras

SIZE_CAP = 0.25   # daily sizing cap → normalizes a sizing pct into s_i ∈ [0,1]


def _conn():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def si_from_pct(pct):
    if pct is None:
        return 0.0
    return max(0.0, min(1.0, float(pct) / SIZE_CAP))


def _sign(direction):
    return 1.0 if str(direction or '').upper() in ('LONG', 'BUY') else -1.0


def live_shadow_orders(run_date):
    """run_date 'YYYY-MM-DD'. Today's equity opens Phase A executed."""
    out = []
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT ticker, direction, qty, pct_nav, filled_avg_price,
                      official_close, strategy_id
               FROM alpaca_submissions
               WHERE run_date = %s AND instrument_class = 'equity'
                 AND qty IS NOT NULL AND qty <> 0""", (run_date,))
        for r in cur.fetchall():
            out.append({
                'ticker': r['ticker'], 'signed_qty': _sign(r['direction']) * float(r['qty']),
                's_i': si_from_pct(r['pct_nav']), 'run_date': run_date,
                'naive_fill': float(r['filled_avg_price']) if r['filled_avg_price'] else None,
                'close_t1': float(r['official_close']) if r['official_close'] else None,
                'strategy_id': r['strategy_id'], 'regime_state': None, 'signal_id': None})
    return out


def case_study_orders(n_losers=25, n_movers=25):
    """Curated: heaviest realized losers + biggest realized gainers. Unit qty (±1) —
    case studies report per-share beat-close (bps); $ scales linearly. close_t1/naive
    resolved later from bars."""
    base = """
        SELECT es.ticker, es.direction, es.position_size_pct, es.regime_state,
               es.signal_id::text AS signal_id, es.strategy_id, es.target_date,
               sp.realized_pnl_pct
        FROM signal_pnl sp JOIN execution_signals es ON es.signal_id = sp.signal_id
        WHERE sp.status = 'closed' AND sp.realized_pnl_pct IS NOT NULL
              AND es.target_date IS NOT NULL
        ORDER BY {order} LIMIT %s"""
    rows = []
    with _conn() as c:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(base.format(order="sp.realized_pnl_pct ASC"), (n_losers,))
        rows += cur.fetchall()
        cur.execute(base.format(order="sp.realized_pnl_pct DESC"), (n_movers,))
        rows += cur.fetchall()
    out, seen = [], set()
    for r in rows:
        if r['signal_id'] in seen:
            continue
        seen.add(r['signal_id'])
        out.append({
            'ticker': r['ticker'], 'signed_qty': _sign(r['direction']) * 1.0,
            's_i': si_from_pct(r['position_size_pct']),
            'run_date': r['target_date'].isoformat() if r['target_date'] else None,
            'naive_fill': None, 'close_t1': None, 'strategy_id': r['strategy_id'],
            'regime_state': r['regime_state'], 'signal_id': r['signal_id']})
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_order_source.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/b1_order_source.py tests/test_b1_order_source.py
git commit -m "feat(b1): order-source adapters (case-study + live shadow)"
```

---

### Task 5: `b1_ledger.py` — persistence + report (Discord via requests)

**Files:**
- Create: `src/execution/b1_ledger.py`
- Test: `tests/test_b1_ledger.py`

- [ ] **Step 1: Write the failing tests** (pure report builder)

```python
# tests/test_b1_ledger.py
from execution.b1_ledger import build_report

def test_report_classifies_beat_tie_loss_vs_both_baselines():
    rows = [
        # beats both naive and base
        {'exec_ledger': 10.0, 'naive_ledger': 2.0, 'vwap_base_ledger': 4.0,
         'completion': 1.0, 'regime_state': 'LOW_VOL'},
        # loses to naive
        {'exec_ledger': -5.0, 'naive_ledger': 1.0, 'vwap_base_ledger': -6.0,
         'completion': 1.0, 'regime_state': 'LOW_VOL'},
    ]
    rep = build_report(rows)
    assert rep['n'] == 2
    assert rep['beats_naive'] == 1
    assert rep['beats_base'] == 1
    assert 'LOW_VOL' in rep['by_regime']
    assert abs(rep['total_exec_ledger'] - 5.0) < 1e-9
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b1_ledger'`

- [ ] **Step 3: Write the minimal implementation** (Discord post mirrors `regime_liquidator._post_to_discord`, with a UA per the 2026-06-01 Cloudflare-1010 fix)

```python
# src/execution/b1_ledger.py
"""Persist B1 shadow ledger rows + build/post the go/no-go report. Discord posting
uses requests (passes Cloudflare) with an explicit UA for safety."""
from __future__ import annotations
import os
import psycopg2
import psycopg2.extras
import requests


def _conn():
    return psycopg2.connect(os.environ['POSTGRES_URI'])


def persist_rows(mode, rows):
    """rows: list of dicts with the ledger fields. Writes to b1_shadow_exec_ledger."""
    if not rows:
        return 0
    with _conn() as c:
        cur = c.cursor()
        psycopg2.extras.execute_values(cur, """
            INSERT INTO b1_shadow_exec_ledger
              (mode, run_date, ticker, strategy_id, signal_id, regime_state,
               signed_qty, s_i, lam, actual_fill, close_t1, exec_ledger,
               naive_ledger, vwap_base_ledger, completion)
            VALUES %s""",
            [(mode, r.get('run_date'), r['ticker'], r.get('strategy_id'),
              r.get('signal_id'), r.get('regime_state'), r.get('signed_qty'),
              r.get('s_i'), r.get('lam'), r.get('actual_fill'), r.get('close_t1'),
              r.get('exec_ledger'), r.get('naive_ledger'), r.get('vwap_base_ledger'),
              r.get('completion')) for r in rows])
        c.commit()
    return len(rows)


def build_report(rows):
    """Aggregate beat-close vs BOTH baselines, overall + per regime. Pure."""
    rep = {'n': len(rows), 'beats_naive': 0, 'beats_base': 0,
           'total_exec_ledger': 0.0, 'total_naive_ledger': 0.0,
           'avg_completion': 0.0, 'by_regime': {}}
    for r in rows:
        ex = r.get('exec_ledger') or 0.0
        rep['total_exec_ledger'] += ex
        rep['total_naive_ledger'] += r.get('naive_ledger') or 0.0
        rep['avg_completion'] += r.get('completion') or 0.0
        if ex > (r.get('naive_ledger') or 0.0):
            rep['beats_naive'] += 1
        if ex > (r.get('vwap_base_ledger') or 0.0):
            rep['beats_base'] += 1
        g = r.get('regime_state') or 'UNKNOWN'
        rg = rep['by_regime'].setdefault(g, {'n': 0, 'exec_ledger': 0.0})
        rg['n'] += 1
        rg['exec_ledger'] += ex
    if rows:
        rep['avg_completion'] /= len(rows)
    return rep


def format_report(mode, rep):
    lines = [f"**B1 shadow ({mode})** — n={rep['n']}",
             f"beats naive-3:55: {rep['beats_naive']}/{rep['n']}  |  beats VWAP-base: {rep['beats_base']}/{rep['n']}",
             f"Σ exec-ledger: {rep['total_exec_ledger']:+.2f}  (naive baseline {rep['total_naive_ledger']:+.2f})",
             f"avg completion: {rep['avg_completion']:.0%}",
             "by regime: " + ", ".join(f"{g}:{v['exec_ledger']:+.1f}(n={v['n']})"
                                       for g, v in rep['by_regime'].items())]
    return "\n".join(lines)


def post_report(text, channel='data-alerts'):
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT webhook_urls->>%s FROM agent_registry WHERE id='botjohn'", (channel,))
        row = cur.fetchone()
    if not row or not row[0]:
        return False
    try:
        r = requests.post(row[0], json={'content': text[:1900]}, timeout=10,
                          headers={'User-Agent': 'OpenClaw-B1Shadow/1.0 (+botjohn)'})
        return 200 <= r.status_code < 300
    except Exception:
        return False
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_ledger.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/b1_ledger.py tests/test_b1_ledger.py
git commit -m "feat(b1): shadow ledger persistence + go/no-go report"
```

---

### Task 6: `b1_run.py` — gated CLI driver wiring source→planner→simulator→ledger

**Files:**
- Create: `src/execution/b1_run.py`
- Test: `tests/test_b1_run.py`

- [ ] **Step 1: Write the failing tests** (gate off ⇒ no-op; bars-loader bucket mapping)

```python
# tests/test_b1_run.py
import os
from execution.b1_run import bucket_of, run

def test_bucket_mapping_rth():
    assert bucket_of("2026-06-01T13:30:00+00:00") == 0     # 9:30 ET = 13:30 UTC
    assert bucket_of("2026-06-01T14:00:00+00:00") == 1
    assert bucket_of("2026-06-01T19:30:00+00:00") == 12    # 15:30 ET
    assert bucket_of("2026-06-01T20:00:00+00:00") is None  # 16:00 close — not a working bucket

def test_run_noop_when_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_B1_SHADOW', raising=False)
    assert run(mode='case_study') == {'status': 'gate_off', 'persisted': 0}
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_run.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b1_run'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/execution/b1_run.py
"""B1 shadow driver (gated OPENCLAW_B1_SHADOW). Modes: 'case_study' (curated history),
'live_shadow' (today's opens). For each order: load that day's realized 30-min bars +
trailing history → plan (sᵢ-decay) and base-plan (lam=0) → simulate both → persist.
Observation-only: never submits an order."""
from __future__ import annotations
import os
import sys
import argparse
import datetime as dt
import pandas as pd

from execution.b1_planner import expected_volume_profile, plan, RTH_BUCKETS
from execution.b1_simulator import simulate
from execution import b1_order_source as src_mod
from execution import b1_ledger

LAM = 4.0                      # principled prior (spec §5); validated, not fit
TRAILING_DAYS = 20
_PARQUET = 'data/master/prices_30m.parquet'
_OPEN_BUCKET_UTC = (13, 30)   # 9:30 ET = 13:30 UTC (EDT); close 16:00 ET = 20:00 UTC


def bucket_of(datetime_iso):
    """Map a 30-min bar timestamp (UTC ISO) to RTH bucket 0..12, or None if outside
    [9:30,16:00) ET. Assumes EDT (UTC-4); the parquet stores tz-aware UTC."""
    t = dt.datetime.fromisoformat(str(datetime_iso))
    minutes = (t.hour - _OPEN_BUCKET_UTC[0]) * 60 + (t.minute - _OPEN_BUCKET_UTC[1])
    if minutes < 0 or minutes >= RTH_BUCKETS * 30:
        return None
    return minutes // 30


def _load_day_bars(df_t, run_date):
    """df_t: prices_30m rows for one ticker. Returns {bucket:{vwap,high,low,volume}}
    for run_date, and a list of trailing-day bucket-volume vectors."""
    day_bars, trailing = {}, []
    by_date = {}
    for _, r in df_t.iterrows():
        b = bucket_of(r['datetime'].isoformat())
        if b is None:
            continue
        d = str(r['date'])
        by_date.setdefault(d, {})[b] = {'vwap': float(r['vwap']), 'high': float(r['high']),
                                        'low': float(r['low']), 'volume': float(r['volume'])}
    day_bars = by_date.get(run_date, {})
    past = sorted(d for d in by_date if d < run_date)[-TRAILING_DAYS:]
    for d in past:
        vec = [by_date[d].get(b, {}).get('volume', 0.0) for b in range(RTH_BUCKETS)]
        trailing.append(vec)
    return day_bars, trailing


def _score_order(o, df_ticker):
    day_bars, trailing = _load_day_bars(df_ticker, o['run_date'])
    if not day_bars:
        return None  # no coverage — skip (don't impute)
    close_t1 = o['close_t1']
    if close_t1 is None:
        last = max(day_bars)                       # last available bucket vwap ≈ close proxy
        close_t1 = day_bars[last]['vwap']
    profile = expected_volume_profile(trailing)
    alpha = simulate(plan(o['signed_qty'], o['s_i'], profile, LAM), day_bars, close_t1, o.get('naive_fill'))
    base = simulate(plan(o['signed_qty'], 0.0, profile, LAM), day_bars, close_t1, o.get('naive_fill'))
    if alpha['actual_fill'] is None:
        return None
    return {**o, 'lam': LAM, 'close_t1': close_t1, 'actual_fill': alpha['actual_fill'],
            'exec_ledger': alpha['exec_ledger'], 'naive_ledger': alpha['naive_ledger'],
            'vwap_base_ledger': base['exec_ledger'], 'completion': alpha['completion']}


def run(mode='case_study', n_losers=25, n_movers=25, run_date=None, post=False):
    if os.environ.get('OPENCLAW_B1_SHADOW') != '1':
        return {'status': 'gate_off', 'persisted': 0}
    orders = (src_mod.case_study_orders(n_losers, n_movers) if mode == 'case_study'
              else src_mod.live_shadow_orders(run_date or dt.date.today().isoformat()))
    df = pd.read_parquet(_PARQUET, columns=['date', 'datetime', 'ticker', 'high', 'low', 'vwap', 'volume'])
    rows = []
    for o in orders:
        if not o.get('run_date'):
            continue
        scored = _score_order(o, df[df['ticker'] == o['ticker']])
        if scored:
            rows.append(scored)
    persisted = b1_ledger.persist_rows(mode, rows)
    rep = b1_ledger.build_report(rows)
    text = b1_ledger.format_report(mode, rep)
    print(text)
    if post:
        b1_ledger.post_report(text)
    return {'status': 'ok', 'persisted': persisted, 'report': rep}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['case_study', 'live_shadow'], default='case_study')
    ap.add_argument('--run-date')
    ap.add_argument('--n-losers', type=int, default=25)
    ap.add_argument('--n-movers', type=int, default=25)
    ap.add_argument('--post', action='store_true')
    a = ap.parse_args()
    out = run(mode=a.mode, n_losers=a.n_losers, n_movers=a.n_movers, run_date=a.run_date, post=a.post)
    print(out['status'], 'persisted=', out.get('persisted'))
    return 0 if out['status'] in ('ok', 'gate_off') else 1


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_run.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the full B1 unit suite**

Run: `cd /root/openclaw && python3 -m pytest tests/test_b1_*.py -q`
Expected: PASS (14 passed)

- [ ] **Step 6: Commit**

```bash
git add src/execution/b1_run.py tests/test_b1_run.py
git commit -m "feat(b1): gated shadow driver wiring source→planner→sim→ledger"
```

---

### Task 7: Curated bars backfill + end-to-end smoke

**Files:**
- Create: `scripts/backfill_b1_bars.sh`
- Uses (existing): `src/ingestion/ingest_prices_30m.py` (has `--rebuild --from`, `--universe-file`, `--dry-run`)

- [ ] **Step 1: Build the curated universe + date list from the case studies**

Run:
```bash
cd /root/openclaw && PYTHONPATH=src OPENCLAW_B1_SHADOW=1 python3 - <<'PY'
from execution.b1_order_source import case_study_orders
o = case_study_orders(25, 25)
tickers = sorted({x['ticker'] for x in o if x['ticker']})
open('/tmp/b1_universe.txt', 'w').write("\n".join(tickers))
print('case-study orders:', len(o), 'unique tickers:', len(tickers))
print('date span:', min(x['run_date'] for x in o if x['run_date']), '→', max(x['run_date'] for x in o if x['run_date']))
PY
```
Expected: prints counts + a date span; writes `/tmp/b1_universe.txt`.

- [ ] **Step 2: Write the chunked backfill driver** (one ticker-month per subprocess — RSS frees between; per the weekend-OOM lesson, 2-core/8GB)

```bash
# scripts/backfill_b1_bars.sh
#!/usr/bin/env bash
# Curated 30-min bars backfill for B1 case studies. Chunked (one --from window per
# call so RSS frees between) + nice'd. Append-only into prices_30m.parquet.
set -euo pipefail
cd /root/openclaw
UNIV="${1:-/tmp/b1_universe.txt}"
FROM="${2:-2025-06-01}"
echo "[b1-backfill] tickers=$(wc -l < "$UNIV") from=$FROM"
nice -n 19 python3 src/ingestion/ingest_prices_30m.py --rebuild --from "$FROM" --universe-file "$UNIV"
echo "[b1-backfill] done"
```

- [ ] **Step 3: Run the backfill (verify coverage grew)**

Run:
```bash
cd /root/openclaw && chmod +x scripts/backfill_b1_bars.sh && ./scripts/backfill_b1_bars.sh /tmp/b1_universe.txt 2025-06-01
python3 -c "import pyarrow.parquet as pq; print('prices_30m rows:', pq.read_metadata('data/master/prices_30m.parquet').num_rows)"
```
Expected: row count materially larger than the pre-backfill 16,539.

- [ ] **Step 4: End-to-end case-study smoke**

Run:
```bash
cd /root/openclaw && PYTHONPATH=src OPENCLAW_B1_SHADOW=1 python3 -m execution.b1_run --mode case_study --n-losers 25 --n-movers 25
```
Expected: prints the report (n, beats-naive, beats-base, Σ exec-ledger, per-regime) and `ok persisted=<N>`. Then confirm rows landed:
```bash
python3 -c "import os,psycopg2; c=psycopg2.connect(os.environ['POSTGRES_URI']).cursor(); c.execute(\"SELECT mode,COUNT(*),ROUND(SUM(exec_ledger)::numeric,2) FROM b1_shadow_exec_ledger GROUP BY 1\"); print(c.fetchall())"
```
Expected: a `case_study` row with a count and a Σ exec-ledger.

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_b1_bars.sh
git commit -m "feat(b1): curated bars backfill driver + e2e case-study smoke"
```

---

## Self-Review

**Spec coverage:** §1 objective → Task 6 driver; §2 math → encoded in planner `exp(-λ·sᵢ·t)` (Task 2); §3 components 1-5 → Tasks 7,2,3,4,5 respectively; §4 data flow → Task 6 `run()`; §5 validation (case-study + report) → Tasks 4,5,7; §6 error handling (skip-don't-impute, look-ahead) → simulator skip (Task 3) + planner causality test (Task 2); §7 tests → each task; §8 gate → Task 6 `run()` gate check. All covered.

**Deferred (correctly absent):** live executor, sizing-timing cutover, Hawkes/§28, impact-Almgren term, close-side scheduling — none appear as tasks. ✓

**Type consistency:** order dict shape identical across Tasks 4/6; `plan()` returns `[(bucket, slice_qty)]` consumed unchanged by `simulate()`; simulate return keys (`actual_fill, exec_ledger, naive_ledger, completion, filled_qty`) match `_score_order` usage; `build_report` consumes `exec_ledger/naive_ledger/vwap_base_ledger/completion/regime_state` which `_score_order` produces. ✓

**Known approximations (intentional, per spec §9 / operator steer):** `bucket_of` assumes EDT (UTC-4) — fine for the current DST window; flag a DST-aware refinement if backfilling across a Nov/Mar boundary. Case-study uses unit qty (per-share bps); `close_t1` proxy = last available bucket VWAP when the official close isn't carried on the order.
