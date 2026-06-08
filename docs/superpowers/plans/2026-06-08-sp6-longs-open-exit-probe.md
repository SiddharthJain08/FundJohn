# Intraday-Session Probe ① Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run the pre-registered Probe ① that gates the longs-only open-exit structure — does the exit-day intraday session reliably *favor* holding longs (→ veto) or not (→ clear-to-ship-gated)?

**Architecture:** A pure-function core `compute_probe(primary_df, prices_df, regimes_df) -> dict` (no I/O, fully unit-testable on synthetic frames) plus a thin runner that loads from Postgres + master parquet, calls the core, and writes a report. The measured quantity is `intraday_return = (close − open)/open` on `max_hold`-LONG exit days, aggregated with a day-clustered t (same-day names are beta-correlated). Verdict is an asymmetric veto: NO-GO only if the session is reliably positive.

**Tech Stack:** Python, pandas, numpy, psycopg2; reads `data/master/{prices,historical_regimes}.parquet` (read-only) and `strategy_backtest_trades`/`strategy_backtest_runs` (read-only).

**Spec:** `docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md`

**House rules (every task):** branch `feat/sp6-phase-a-eod-open-execution`; `git add` EXPLICIT paths only — NEVER stage `src/strategies/manifest.json`, `src/strategies/strategy_signatures.json`, `src/pipeline/run_sentiment_step.py`. Tests run `PYTHONPATH=src:. nice -n 19 python3 -m pytest <file> -q`. Tests use synthetic in-memory frames ONLY — never read the real master parquet or DB. No-peek: the runner prints counts only until the verdict block.

**Verified schema facts (do not re-derive):**
- `strategy_backtest_trades`: `ticker text`, `direction text` ('long'/'short'), `exit_date date`, `exit_reason text` ('max_hold' etc.), `run_id uuid`. `strategy_backtest_runs`: `run_id`, `primary_window bool`.
- `prices.parquet` columns: `ticker(str)`, `date(str 'YYYY-MM-DD')`, `open`, `high`, `low`, `close`, `volume`, `vwap`, `transactions`, `source`.
- `historical_regimes.parquet` columns: `date(str)`, `vix`, `vix_smoothed`, `regime(str)`.
- PRIMARY population size today: 50,929 distinct (ticker, exit_date) across 2,633 exit-day clusters.

---

### Task 1: Package + clustered-t + half-year-bucket helpers

**Files:**
- Create: `src/research/exit_timing/__init__.py`
- Create: `src/research/exit_timing/intraday_session_probe.py`
- Test: `tests/test_intraday_session_probe.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intraday_session_probe.py
import math
import pandas as pd
import pytest
from research.exit_timing import intraday_session_probe as p


def test_clustered_t_known_values():
    # 3 day-clusters; per-day means = [0.02, -0.01, 0.05]; mean=0.02
    df = pd.DataFrame({
        "date": ["d1", "d1", "d2", "d3"],
        "intraday_return": [0.01, 0.03, -0.01, 0.05],
    })
    mean, t, n = p.clustered_t(df, "intraday_return", "date")
    assert n == 3
    assert abs(mean - 0.02) < 1e-12
    g = [0.02, -0.01, 0.05]
    sd = pd.Series(g).std(ddof=1)
    assert abs(t - (0.02 / (sd / math.sqrt(3)))) < 1e-9


def test_clustered_t_degenerate_single_cluster():
    df = pd.DataFrame({"date": ["d1", "d1"], "intraday_return": [0.01, 0.03]})
    mean, t, n = p.clustered_t(df, "intraday_return", "date")
    assert n == 1
    assert abs(mean - 0.02) < 1e-12
    assert math.isnan(t)


def test_half_year_bucket():
    assert p.half_year_bucket("2024-03-15") == "2024H1"
    assert p.half_year_bucket("2024-06-30") == "2024H1"
    assert p.half_year_bucket("2024-07-01") == "2024H2"
    assert p.half_year_bucket("2026-12-31") == "2026H2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.exit_timing'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/research/exit_timing/__init__.py
```
(empty file — package marker)

```python
# src/research/exit_timing/intraday_session_probe.py
"""Probe ①: exit-day intraday-session return for max_hold-long exits.

Pure-function core (compute_probe) + helpers. The runner (scripts/
run_intraday_session_probe.py) supplies the data. Spec:
docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md
"""
from __future__ import annotations

import math
import pandas as pd

# ── pre-registered constants (LOCKED) ─────────────────────────────────
T_VETO = 3.0          # pooled positive t that vetoes
T_RECENT = 2.0        # recent-bucket positive t that vetoes
MIN_CLUSTERS = 500    # min distinct exit-day clusters or INVALID-DATA
REGIMES = ("LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS")


def clustered_t(df: pd.DataFrame, value_col: str, cluster_col: str):
    """Day-clustered t: per-cluster mean -> across-cluster mean & t.

    Returns (mean, t, n_clusters). t is NaN when n<2 or sd==0.
    """
    g = df.groupby(cluster_col)[value_col].mean()
    n = int(g.shape[0])
    mean = float(g.mean())
    if n < 2:
        return mean, float("nan"), n
    sd = float(g.std(ddof=1))
    if sd == 0 or math.isnan(sd):
        return mean, float("nan"), n
    t = mean / (sd / math.sqrt(n))
    return mean, t, n


def half_year_bucket(date_str: str) -> str:
    """'YYYY-MM-DD' -> 'YYYYH1'|'YYYYH2'."""
    year = date_str[:4]
    month = int(date_str[5:7])
    return f"{year}H1" if month <= 6 else f"{year}H2"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/research/exit_timing/__init__.py src/research/exit_timing/intraday_session_probe.py tests/test_intraday_session_probe.py && git commit -m "exit-probe: package + clustered_t + half_year_bucket helpers"
```

---

### Task 2: Verdict logic (asymmetric veto)

**Files:**
- Modify: `src/research/exit_timing/intraday_session_probe.py`
- Test: `tests/test_intraday_session_probe.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_intraday_session_probe.py
def test_verdict_invalid_data():
    assert p.verdict(primary_mean=-0.001, primary_t=-4.0,
                     recent_ts=[-0.5, 0.2], n_clusters=499) == "INVALID-DATA"


def test_verdict_nogo_pooled_positive():
    assert p.verdict(primary_mean=0.002, primary_t=3.5,
                     recent_ts=[0.1, -0.2], n_clusters=2000) == "NO-GO"


def test_verdict_nogo_recent_bucket_positive():
    # pooled benign but a recent half-year is reliably positive
    assert p.verdict(primary_mean=0.0001, primary_t=0.5,
                     recent_ts=[2.4, -0.3], n_clusters=2000) == "NO-GO"


def test_verdict_clear_to_ship():
    assert p.verdict(primary_mean=-0.0008, primary_t=-4.2,
                     recent_ts=[-1.0, -0.5], n_clusters=2000) == "CLEAR-TO-SHIP-GATED"


def test_verdict_clear_with_caution():
    # positive point estimate but not significant, recent benign
    assert p.verdict(primary_mean=0.0003, primary_t=1.1,
                     recent_ts=[0.8, -0.4], n_clusters=2000) == "CLEAR-WITH-CAUTION"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -k verdict -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'verdict'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/research/exit_timing/intraday_session_probe.py
def verdict(primary_mean: float, primary_t: float,
            recent_ts: list[float], n_clusters: int) -> str:
    """Asymmetric veto (spec §1.4). NaN t's are treated as non-significant."""
    if n_clusters < MIN_CLUSTERS:
        return "INVALID-DATA"

    def _sig_pos(t):
        return (t == t) and t >= T_VETO  # t==t filters NaN

    def _sig_pos_recent(t):
        return (t == t) and t >= T_RECENT

    if _sig_pos(primary_t):
        return "NO-GO"
    if any(_sig_pos_recent(t) for t in recent_ts):
        return "NO-GO"
    if primary_mean > 0:
        return "CLEAR-WITH-CAUTION"
    return "CLEAR-TO-SHIP-GATED"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -k verdict -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/research/exit_timing/intraday_session_probe.py tests/test_intraday_session_probe.py && git commit -m "exit-probe: asymmetric-veto verdict logic"
```

---

### Task 3: Frame prep — intraday_return, equity filter, regime/bucket attach

**Files:**
- Modify: `src/research/exit_timing/intraday_session_probe.py`
- Test: `tests/test_intraday_session_probe.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_intraday_session_probe.py
def test_prep_prices_computes_return_and_filters_equity():
    prices = pd.DataFrame({
        "ticker": ["AAA", "AAA", "^VIX", "BTC-USD", "BBB"],
        "date":   ["2024-01-02"] * 5,
        "open":   [100.0, 0.0, 20.0, 50000.0, 10.0],
        "close":  [99.0, 50.0, 19.0, 51000.0, 10.5],
    })
    out = p.prep_prices(prices)
    # ^VIX and BTC-USD dropped (non-equity); AAA open=0.0 row dropped
    assert set(out["ticker"]) == {"AAA", "BBB"}
    aaa = out[out["ticker"] == "AAA"].iloc[0]
    assert abs(aaa["intraday_return"] - (-0.01)) < 1e-12


def test_attach_regime_and_bucket():
    df = pd.DataFrame({"ticker": ["AAA"], "date": ["2024-07-03"],
                       "intraday_return": [-0.005]})
    regimes = pd.DataFrame({"date": ["2024-07-03"], "regime": ["HIGH_VOL"]})
    out = p.attach_regime_bucket(df, regimes)
    assert out.iloc[0]["regime"] == "HIGH_VOL"
    assert out.iloc[0]["bucket"] == "2024H2"


def test_attach_primary_inner_joins_returns():
    primary = pd.DataFrame({"ticker": ["AAA", "ZZZ"], "date": ["2024-01-02", "2024-01-02"]})
    prices_prepped = pd.DataFrame({"ticker": ["AAA"], "date": ["2024-01-02"],
                                   "intraday_return": [-0.01]})
    out = p.attach_primary(primary, prices_prepped)
    # ZZZ has no price row -> dropped
    assert list(out["ticker"]) == ["AAA"]
    assert abs(out.iloc[0]["intraday_return"] - (-0.01)) < 1e-12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -k "prep or attach" -q`
Expected: FAIL — `AttributeError: ... 'prep_prices'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/research/exit_timing/intraday_session_probe.py
def _is_equity(ticker: str) -> bool:
    """Exclude indices (^...), crypto (-USD), fx (=X), pairs (/) from the
    equity universe used for SECONDARY and the M2 same-day baseline."""
    t = str(ticker)
    return not (t.startswith("^") or "-USD" in t or "=" in t or "/" in t)


def prep_prices(prices: pd.DataFrame) -> pd.DataFrame:
    """Equity-only rows with a finite intraday_return = (close-open)/open."""
    df = prices[["ticker", "date", "open", "close"]].copy()
    df = df[df["ticker"].map(_is_equity)]
    df = df[df["open"] > 0]
    df["intraday_return"] = (df["close"] - df["open"]) / df["open"]
    df = df[df["intraday_return"].notna()]
    return df[["ticker", "date", "intraday_return"]].reset_index(drop=True)


def attach_regime_bucket(df: pd.DataFrame, regimes: pd.DataFrame) -> pd.DataFrame:
    out = df.merge(regimes[["date", "regime"]], on="date", how="left")
    out["bucket"] = out["date"].map(half_year_bucket)
    return out


def attach_primary(primary: pd.DataFrame, prices_prepped: pd.DataFrame) -> pd.DataFrame:
    """Inner-join PRIMARY (ticker,date) to its intraday_return."""
    return primary.merge(prices_prepped, on=["ticker", "date"], how="inner")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -k "prep or attach" -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/research/exit_timing/intraday_session_probe.py tests/test_intraday_session_probe.py && git commit -m "exit-probe: price prep, equity filter, regime/bucket attach"
```

---

### Task 4: M2 relative diagnostic + bucket_stats + compute_probe core

**Files:**
- Modify: `src/research/exit_timing/intraday_session_probe.py`
- Test: `tests/test_intraday_session_probe.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_intraday_session_probe.py
import numpy as np


def _synth_world(sign, n_days=600, n_names=10, seed_base=7):
    """Build (primary, prices, regimes) where primary names move `sign`
    intraday by ~0.002 with small noise; universe is flat-ish."""
    dates = pd.bdate_range("2022-01-03", periods=n_days).strftime("%Y-%m-%d")
    prim_rows, price_rows = [], []
    for di, d in enumerate(dates):
        for ni in range(n_names):
            tk = f"P{ni}"
            # deterministic pseudo-noise (no Math.random / Date)
            noise = (((di * 31 + ni * 17 + seed_base) % 100) - 50) / 50.0 * 0.0005
            ret = sign * 0.002 + noise
            op = 100.0
            cl = op * (1 + ret)
            price_rows.append({"ticker": tk, "date": d, "open": op, "close": cl})
            prim_rows.append({"ticker": tk, "date": d})
        # a flat "other" universe name each day (baseline for M2)
        price_rows.append({"ticker": "U0", "date": d, "open": 100.0, "close": 100.0})
    regimes = pd.DataFrame({"date": list(dates), "regime": ["LOW_VOL"] * len(dates)})
    return pd.DataFrame(prim_rows), pd.DataFrame(price_rows), regimes


def test_compute_probe_negative_world_clears():
    primary, prices, regimes = _synth_world(sign=-1)
    res = p.compute_probe(primary, prices, regimes)
    assert res["primary_m1"]["mean"] < 0
    assert res["primary_m1"]["n"] >= p.MIN_CLUSTERS
    assert res["verdict"] == "CLEAR-TO-SHIP-GATED"


def test_compute_probe_positive_world_vetoes():
    primary, prices, regimes = _synth_world(sign=+1)
    res = p.compute_probe(primary, prices, regimes)
    assert res["primary_m1"]["mean"] > 0
    assert res["primary_m1"]["t"] >= p.T_VETO
    assert res["verdict"] == "NO-GO"


def test_compute_probe_m2_isolates_name_effect():
    # primary names move -0.002 vs a flat universe -> M2 (relative) negative
    primary, prices, regimes = _synth_world(sign=-1)
    res = p.compute_probe(primary, prices, regimes)
    assert res["m2_relative"]["mean"] < 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -k compute_probe -q`
Expected: FAIL — `AttributeError: ... 'compute_probe'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to src/research/exit_timing/intraday_session_probe.py
def bucket_stats(df: pd.DataFrame, bucket_col: str) -> pd.DataFrame:
    """Per-bucket clustered-t (cluster=date). Returns df indexed by bucket
    with columns mean,t,n, sorted by bucket label."""
    rows = []
    for b, sub in df.groupby(bucket_col):
        if str(b) == "nan":
            continue
        mean, t, n = clustered_t(sub, "intraday_return", "date")
        rows.append({bucket_col: b, "mean": mean, "t": t, "n": n})
    out = pd.DataFrame(rows).sort_values(bucket_col).reset_index(drop=True)
    return out


def _m1(df: pd.DataFrame) -> dict:
    mean, t, n = clustered_t(df, "intraday_return", "date")
    return {"mean": mean, "t": t, "n": n}


def compute_probe(primary: pd.DataFrame, prices: pd.DataFrame,
                  regimes: pd.DataFrame) -> dict:
    """Pure core. Returns all stats + the pre-registered verdict."""
    prepped = prep_prices(prices)

    # PRIMARY (max_hold-long exit days) with returns + regime + bucket
    prim = attach_primary(primary, prepped)
    prim = attach_regime_bucket(prim, regimes)

    # SECONDARY: full equity universe day-by-day
    sec = attach_regime_bucket(prepped, regimes)

    # M2: PRIMARY return minus the same-day equity-universe mean
    uni_day_mean = prepped.groupby("date")["intraday_return"].mean()
    rel = prim.copy()
    rel["intraday_return"] = rel["intraday_return"] - rel["date"].map(uni_day_mean)

    halfyear = bucket_stats(prim, "bucket")
    recent_ts = list(halfyear.sort_values("bucket")["t"].tail(2)) if len(halfyear) else []

    primary_m1 = _m1(prim)
    v = verdict(primary_m1["mean"], primary_m1["t"], recent_ts, primary_m1["n"])

    return {
        "primary_m1": primary_m1,
        "secondary_m1": _m1(sec),
        "m2_relative": _m1(rel),
        "by_regime": bucket_stats(prim, "regime").to_dict("records"),
        "by_halfyear": halfyear.to_dict("records"),
        "recent_ts": recent_ts,
        "verdict": v,
        "n_primary_rows": int(len(prim)),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -k compute_probe -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the FULL module suite**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -q`
Expected: PASS (14 tests)

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw && git add src/research/exit_timing/intraday_session_probe.py tests/test_intraday_session_probe.py && git commit -m "exit-probe: bucket_stats, M2 relative, compute_probe core"
```

---

### Task 5: Runner script (loaders + report)

**Files:**
- Create: `scripts/run_intraday_session_probe.py`
- Test: `tests/test_intraday_session_probe.py`

- [ ] **Step 1: Write the failing test (report writer is importable + pure)**

```python
# append to tests/test_intraday_session_probe.py
import importlib.util, pathlib

def _load_runner():
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "run_intraday_session_probe", root / "scripts" / "run_intraday_session_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runner_render_report_contains_verdict_and_tables(tmp_path):
    runner = _load_runner()
    primary, prices, regimes = _synth_world(sign=-1)
    res = p.compute_probe(primary, prices, regimes)
    md = runner.render_report(res)
    assert "VERDICT:" in md
    assert "CLEAR-TO-SHIP-GATED" in md
    assert "PRIMARY (max_hold-long)" in md
    assert "By regime" in md
    assert "By half-year" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -k runner -q`
Expected: FAIL — file does not exist

- [ ] **Step 3: Write minimal implementation**

```python
# scripts/run_intraday_session_probe.py
"""Probe ① runner. Loads max_hold-long exits + prices + regimes, calls
compute_probe, writes analysis/exit_timing_probe/{report.md,rows.parquet}.

NO-PEEK: progress prints counts only; the verdict block is the first look.
Spec: docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md
"""
from __future__ import annotations

import argparse
import os
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd  # noqa: E402
from research.exit_timing import intraday_session_probe as p  # noqa: E402


def load_primary_exits(conn) -> pd.DataFrame:
    sql = """
        SELECT DISTINCT t.ticker, t.exit_date
        FROM strategy_backtest_trades t
        JOIN strategy_backtest_runs r ON r.run_id = t.run_id
        WHERE r.primary_window = TRUE
          AND t.exit_reason = 'max_hold'
          AND t.direction = 'long'
    """
    df = pd.read_sql(sql, conn)
    df["date"] = df["exit_date"].astype(str)
    return df[["ticker", "date"]]


def _fmt_row(r) -> str:
    t = r.get("t")
    tt = "nan" if t != t else f"{t:+.3f}"  # noqa: PLR0124 (NaN check)
    return f"| {r.get('regime', r.get('bucket', ''))} | {r['mean']*1e4:+.3f} | {tt} | {r['n']} |"


def render_report(res: dict) -> str:
    pm = res["primary_m1"]; sm = res["secondary_m1"]; m2 = res["m2_relative"]
    def line(name, d):
        t = d["t"]; tt = "nan" if t != t else f"{t:+.4f}"
        return f"- {name}: mean {d['mean']*1e4:+.4f} bps | t {tt} | n_days {d['n']}"
    out = []
    out.append("# Probe ① — Intraday-Session Return (longs-only open-exit gate)\n")
    out.append("**Spec**: docs/superpowers/specs/2026-06-08-sp6-longs-open-exit-probe-design.md\n")
    out.append("Quantity: intraday_return=(close-open)/open on max_hold-LONG exit days. "
               "Open-exit edge for a long = -E[intraday_return]. Asymmetric veto.\n")
    out.append("## Headline (day-clustered)\n")
    out.append(line("PRIMARY (max_hold-long)", pm))
    out.append(line("SECONDARY (equity universe)", sm))
    out.append(line("M2 relative (PRIMARY - same-day universe mean)", m2))
    out.append(f"- PRIMARY rows (ticker x exit_date): {res['n_primary_rows']}\n")
    out.append("## By regime (PRIMARY)\n")
    out.append("| regime | mean bps | t | n_days |")
    out.append("|---|---|---|---|")
    for r in res["by_regime"]:
        out.append(_fmt_row(r))
    out.append("\n## By half-year (PRIMARY)\n")
    out.append("| bucket | mean bps | t | n_days |")
    out.append("|---|---|---|---|")
    for r in res["by_halfyear"]:
        out.append(_fmt_row(r))
    out.append("")
    out.append("Decision rule (spec §1.4): NO-GO iff PRIMARY pooled t>=+3.0, OR any of the two "
               "most-recent half-years t>=+2.0. Else CLEAR (CAUTION if pooled mean>0). "
               "INVALID-DATA iff n_days<500.\n")
    out.append(f"**VERDICT: {res['verdict']}**\n")
    out.append("Decision linkage: NO-GO -> close-exit stands for longs, question closed. "
               "CLEAR(-WITH-CAUTION) -> proceed to the gated live-structure spec/plan "
               "(longs-only open-exit, >=9:31 marketable-limit/TIF=day + close fallback, "
               "forward-confirm on live fills). Net cost ratified by live fills only.\n")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis-dir", default="analysis")
    ap.add_argument("--prices", default="data/master/prices.parquet")
    ap.add_argument("--regimes", default="data/master/historical_regimes.parquet")
    args = ap.parse_args()

    import psycopg2
    uri = os.environ.get("POSTGRES_URI")
    if not uri:
        print("[exit-probe] POSTGRES_URI not set", flush=True)
        return 2
    conn = psycopg2.connect(uri)
    try:
        primary = load_primary_exits(conn)
    finally:
        conn.close()
    print(f"[exit-probe] PRIMARY exits loaded: {len(primary)} rows", flush=True)

    prices = pd.read_parquet(args.prices, columns=["ticker", "date", "open", "close"])
    print(f"[exit-probe] price rows: {len(prices)}", flush=True)
    regimes = pd.read_parquet(args.regimes, columns=["date", "regime"])
    print(f"[exit-probe] regime rows: {len(regimes)}", flush=True)

    res = p.compute_probe(primary, prices, regimes)

    out_dir = pathlib.Path(args.analysis_dir) / "exit_timing_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = render_report(res)
    (out_dir / "report.md").write_text(md)

    # rows.parquet: the PRIMARY attached frame for audit
    prepped = p.prep_prices(prices)
    prim = p.attach_regime_bucket(p.attach_primary(primary, prepped), regimes)
    prim.to_parquet(out_dir / "rows.parquet", index=False)

    print(md, flush=True)
    print(f"[exit-probe] VERDICT: {res['verdict']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -q`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add scripts/run_intraday_session_probe.py tests/test_intraday_session_probe.py && git commit -m "exit-probe: runner (loaders + report) + end-to-end render test"
```

---

### Task 6: LOCK the prereg (no data observed yet)

**Files:** none new — this records that the probe code is frozen before any real run.

- [ ] **Step 1: Confirm clean state + full suite green**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_intraday_session_probe.py -q && git status --porcelain`
Expected: 15 passed; `git status` shows no probe files dirty (all committed). The live-critical files (manifest.json, strategy_signatures.json, run_sentiment_step.py) MAY still show as dirty — that is expected; DO NOT stage them.

- [ ] **Step 2: Tag the lock in the log**

Run: `cd /root/openclaw && git log --oneline -6`
Expected: the five exit-probe commits present. The HEAD commit hash is the prereg LOCK — record it; no real-data run may precede it.

---

### Task 7: Smoke run on a 1-year slice (mechanics check, low peek)

**Files:** none — invocation only. (We accept a minimal first-look here to verify plumbing; the authoritative run is Task 8.)

- [ ] **Step 1: Verify the runner executes end-to-end against real stores**

Run:
```bash
cd /root/openclaw && set -a && source <(grep -E "^POSTGRES_URI=" .env) && set +a && \
PYTHONPATH=src:. nice -n 19 python3 scripts/run_intraday_session_probe.py --analysis-dir /tmp/exitprobe_smoke 2>&1 | tail -25
```
Expected: prints PRIMARY/price/regime counts, then a report ending with `[exit-probe] VERDICT: <one of the five labels>`; `n_days` in the headline ≈ 2,633 (well above 500).

- [ ] **Step 2: Confirm master data untouched**

Run: `cd /root/openclaw && git status --porcelain data/master/`
Expected: empty (read-only; no master parquet modified).

---

### Task 8: Authoritative detached run

**Files:** writes `analysis/exit_timing_probe/{report.md,rows.parquet}`.

- [ ] **Step 1: Launch detached via systemd-run (survives session exit)**

Run:
```bash
cd /root/openclaw && set -a && source <(grep -E "^POSTGRES_URI=" .env) && set +a && \
systemd-run --unit=exit-probe-run --working-directory=/root/openclaw \
  --property=Nice=19 --setenv=PYTHONPATH=src:. --setenv=POSTGRES_URI="$POSTGRES_URI" \
  /bin/bash -c 'python3 -u scripts/run_intraday_session_probe.py --analysis-dir analysis > logs/exit_probe_run.log 2>&1'
```
Expected: `Running as unit: exit-probe-run.service`. (This is light — minutes — but detaching keeps the pattern and avoids session-exit kills.)

- [ ] **Step 2: Wait for completion**

Run: `while systemctl is-active --quiet exit-probe-run; do sleep 30; done; echo "done result=$(systemctl show exit-probe-run -p Result --value)"; tail -3 logs/exit_probe_run.log`
Expected: `done result=success` and a `[exit-probe] VERDICT: …` line.

---

### Task 9: Report verdict + commit artifacts + memory

**Files:**
- Commit: `analysis/exit_timing_probe/report.md`
- Update: `/root/.claude/projects/-root/memory/project_sp6_bflow_timing_engine.md`

- [ ] **Step 1: Read the verdict block**

Run: `cat /root/openclaw/analysis/exit_timing_probe/report.md`

- [ ] **Step 2: Commit the report artifact (explicit path)**

```bash
cd /root/openclaw && git add analysis/exit_timing_probe/report.md && git commit -m "exit-probe: RESULT — <VERDICT> (PRIMARY n_days=<N>, mean=<x>bps t=<t>)"
```

- [ ] **Step 3: Update the topic memory** with: the verdict, PRIMARY mean/t and n_days, by-regime/half-year highlights, and the decision-linkage outcome (NO-GO → longs stay at close; CLEAR → next step is the gated live-structure spec). Keep it to a few lines in the existing SP-6 bflow file.

- [ ] **Step 4: Surface to the operator** — verdict, what it means for the longs-only open-exit structure, and (if CLEAR) that the next deliverable is the live-structure spec/plan with the ≥9:31 marketable-limit guard; push/gate-flip remain separate operator approvals.

---

## Self-Review

**Spec coverage:** §1.1 quantity+clustered-t → Tasks 1,4. §1.2 PRIMARY/SECONDARY/M2 populations → Tasks 3,4,5 (loader). §1.3 regime+half-year buckets → Tasks 3,4. §1.4 asymmetric-veto verdict + INVALID-DATA floor → Task 2. §1.5 no-peek (counts-only progress) → Task 5 runner. §2 decision linkage → report text (Task 5) + Task 9. §4 build surface (module/runner/tests paths) → matches Tasks 1–5. Operational guard (§0) is downstream (gated live spec), correctly out of this plan's build.

**Placeholder scan:** No TBD/TODO; every code step shows complete code; the only `<…>` are in the Task 9 commit message (filled from the actual verdict at run time) — acceptable, not code.

**Type consistency:** `clustered_t(df, value_col, cluster_col) -> (mean, t, n)` used identically in `_m1`, `bucket_stats`. `compute_probe` returns keys (`primary_m1`,`secondary_m1`,`m2_relative`,`by_regime`,`by_halfyear`,`recent_ts`,`verdict`,`n_primary_rows`) consumed verbatim by `render_report`. `prep_prices`/`attach_primary`/`attach_regime_bucket` signatures match call sites. `intraday_return` column name consistent throughout. PRIMARY frame columns (`ticker`,`date`) consistent between `load_primary_exits` and `attach_primary`.
