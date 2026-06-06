# SP-6 B-flow Phase-1d MA-Reversion Entry Policy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pre-registered Phase-1d MA-reversion entry-policy counterfactual (spec: `docs/superpowers/specs/2026-06-06-sp6-bflow-phase1d-mr-policy-design.md`) — trigger scan + causal fills + LOSO minute-matched drift null + verdict — WITHOUT running it on historical data (the run is the orchestrator's step, after the kill-test evaluator).

**Architecture:** One pure module `src/research/bflow/mr_policy.py` (pair simulation, delta vectors, LOSO accumulator, stats, verdict) + one runner `scripts/run_bflow_phase1d.py` (two session-major passes over a minute-bar cache dir). Reuses frozen Phase-1/1b/1c machinery verbatim — NO re-derived math.

**Tech Stack:** Python 3, pandas/numpy, pytest. Tests run `nice -n 19 python3 -m pytest` SEQUENTIAL (2-core box — NEVER parallel).

**House rules (binding):** work in `/root/openclaw` on branch `feat/sp6-phase-a-eod-open-execution`. `git add` EXPLICIT FILE PATHS ONLY — repo carries unrelated dirty live-critical files (`src/strategies/manifest.json`, `src/strategies/strategy_signatures.json`, `src/pipeline/run_sentiment_step.py`) that must never be staged. Never `git reset --hard`. Run tests with `PYTHONPATH=src:.` from `/root/openclaw` (bflow modules import both `research.bflow.*` and `src.research.bflow.*` forms).

**Frozen machinery to REUSE (verified signatures — do not reimplement):**

| Surface | Where | Contract |
|---|---|---|
| `compute_features(df)` | `src/research/bflow/flow_features.py` | per-(ticker,session) frame → DataFrame indexed minute 0..389, col `vwap_disp_30` (strictly trailing) |
| `running_z(r)` | `src/research/bflow/energy_counterfactual.py` | Series→Series; t-INCLUSIVE trailing, sample sd ddof=1; NaN until ≥2 finite; NaN where sd=0 |
| `gross_bps(p_eod, p_orc, direction)` | `src/research/bflow/oracle.py` | direction "LONG"/"SHORT"; LONG = (p_eod−p_orc)/p_eod·1e4 |
| `spread_bps(bar)` | `oracle.py` | dict bar → min(0.5·(h−l)/vw·1e4, 50.0) |
| `eod_dump_window_spread_bps(bars)` | `oracle.py` | list-of-dicts → dump-window spread or None |
| `dump_benchmark(bars)` | `oracle.py` | list-of-dicts → vol-weighted vw over minute≥385 valid bars, or None |
| `valid_bar(bar)` | `oracle.py` | vw>0, v>0, h≥l, NaN-safe |
| `_delta_bps(p_eod_dump, entry_price, entry_spread, bars_df, direction)` | `src/research/bflow/flow_policy.py` | net Δbps = gross − (entry_spread − dump_spread); NaN-safe |
| `enumerate_cache_sessions(cache_dir)` / `load_session_frame(cache_dir, session)` / `_ticker_frames(sdf)` | `src/research/bflow/run_phase1b.py` | cache-dir iteration → per-ticker frames |
| `_valid_bar_count(tdf)`, `MIN_VALID_BARS` (=60) | `src/research/bflow/predictability.py` | eligibility floor |

**Pre-registered constants (spec §1/§3 — copy EXACTLY, never tune):**
`ZETAS = (1.0, 1.5, 2.0)`; scan `t ∈ [30, 383]`; LONG trigger `z(t) <= -zeta`, SHORT `z(t) >= +zeta`; fill `vw_{t+1}`; fallback Δ=0/e=0; LOSO null floor `MIN_NULL_OBS = 30`; cell pass `t >= +3`; leg pass ≥2/3 cells; guardrail margin `+10bps` on p95 adverse vs matched pool; entry-spread bar = the FILL bar (t+1) — the bar you transact in, used identically for policy and null (apples-to-apples).

**Core math (used in Tasks 1–3):** for each minute m with a valid bar at m+1:
`G(m) = oracle.gross_bps(dump, vw_{m+1}, "LONG")`, `C(m) = oracle.spread_bps(bar_{m+1}) − dump_spread` ⇒ `net_long(m) = G(m) − C(m)`, `net_short(m) = −G(m) − C(m)`. NaN where bar m+1 invalid or dump is None.

---

### Task 1: `mr_policy.py` — delta vectors + pair simulation

**Files:**
- Create: `src/research/bflow/mr_policy.py`
- Test: `tests/test_bflow_mr_policy_pair.py`

- [ ] **Step 1: Write failing tests** — `tests/test_bflow_mr_policy_pair.py`:

```python
"""Phase-1d mr_policy: delta vectors + simulate_pair.

Synthetic-session conventions copied from tests/test_bflow_flow_policy.py:
a bar dict is {"minute": m, "o":p,"h":p+0.2,"l":p-0.2,"c":p,"v":1000,"vw":p}.
"""
import numpy as np
import pandas as pd
import pytest

from research.bflow import mr_policy as mp
from research.bflow import oracle


def _bar(m, p, v=1000.0):
    return {"minute": m, "o": p, "h": p + 0.2, "l": p - 0.2,
            "c": p, "v": v, "vw": p}


def _flat_session(price=100.0, n=390):
    return pd.DataFrame([_bar(m, price) for m in range(n)])


def test_delta_vectors_flat_session_zero_gross():
    df = _flat_session()
    dump = oracle.dump_benchmark(df.to_dict("records"))
    G, C = mp.delta_vectors(df, dump)
    # flat tape: gross identically 0 at every minute with a valid next bar
    assert np.allclose(G[:-1][np.isfinite(G[:-1])], 0.0)
    # cost differential: identical bars -> entry spread == dump spread -> 0
    assert np.allclose(C[:-1][np.isfinite(C[:-1])], 0.0)
    # minute 389 has no bar 390 -> NaN
    assert np.isnan(G[389]) and np.isnan(C[389])


def test_delta_vectors_long_short_identity():
    df = _flat_session()
    df.loc[df["minute"] == 100, ["o", "h", "l", "c", "vw"]] = 99.0  # dip at 100
    dump = oracle.dump_benchmark(df.to_dict("records"))
    G, C = mp.delta_vectors(df, dump)
    # buying the dip fill at minute 100 means decision minute 99
    assert G[99] > 0
    nl, ns = mp.net_legs(G, C)
    assert np.isclose(nl[99], G[99] - C[99])
    assert np.isclose(ns[99], -G[99] - C[99])


def test_simulate_pair_triggers_on_dip():
    df = _flat_session()
    # carve a deep V: minutes 60..80 fall to 95 then recover
    for m in range(60, 81):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 95.0
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="LONG", zeta=1.0)
    assert row["triggered"] is True
    assert 30 <= row["entry_minute"] <= 383
    assert np.isfinite(row["delta_net_bps"])
    assert np.isfinite(row["gross_at_entry"]) and np.isfinite(row["cost_at_entry"])


def test_simulate_pair_fallback_on_flat():
    df = _flat_session()  # constant tape -> vwap_disp_30 == 0, trailing sd == 0 -> z NaN
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="LONG", zeta=1.0)
    assert row["triggered"] is False
    assert row["entry_minute"] is None
    assert row["delta_net_bps"] == 0.0   # fallback fills AT the benchmark


def test_simulate_pair_void_trigger_continues_scanning():
    df = _flat_session()
    # First dip ONSET at 60 (95.0); a SECOND deeper drop at minute 120 (90.0).
    # vwap_disp_30 is onset-sensitive (a sustained plateau converges to 0) and
    # an invalid bar NaNs the feature for the next 30 minutes — so the
    # re-trigger needs a FRESH negative-displacement onset after the blackout.
    for m in range(60, 120):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 95.0
    for m in range(120, 151):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 90.0
    # invalidate the bar right after the first would-be trigger minute
    first = mp.simulate_pair(
        df, oracle.dump_benchmark(df.to_dict("records")), leg="LONG", zeta=1.0
    )["entry_minute"]
    df.loc[df["minute"] == first + 1, "v"] = 0.0   # invalid fill bar -> VOID
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="LONG", zeta=1.0)
    assert row["triggered"] is True
    assert row["entry_minute"] > first   # scanned past the void


def test_simulate_pair_short_mirror():
    df = _flat_session()
    for m in range(60, 81):   # rip UP -> short trigger
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 105.0
    dump = oracle.dump_benchmark(df.to_dict("records"))
    row = mp.simulate_pair(df, dump, leg="SHORT", zeta=1.0)
    assert row["triggered"] is True


def test_z_convention_matches_running_z():
    """The z used for triggering MUST be energy_counterfactual.running_z of
    compute_features(df)['vwap_disp_30'] — verbatim, t-inclusive."""
    from research.bflow.energy_counterfactual import running_z
    from research.bflow.flow_features import compute_features
    df = _flat_session()
    for m in range(60, 81):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = 95.0
    z_expected = running_z(compute_features(df)["vwap_disp_30"])
    z_actual = mp.trigger_z(df)
    pd.testing.assert_series_equal(z_actual, z_expected)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_mr_policy_pair.py -x -q`
Expected: FAIL `ModuleNotFoundError: No module named 'research.bflow.mr_policy'`

- [ ] **Step 3: Implement** — `src/research/bflow/mr_policy.py`:

```python
"""SP-6 B-flow Phase-1d — MA-reversion entry policy (PRE-REGISTERED).

Spec (BINDING): docs/superpowers/specs/2026-06-06-sp6-bflow-phase1d-mr-policy-design.md
All constants are pre-registered; NEVER tune after first historical run.

Reuses frozen machinery verbatim: flow_features.compute_features,
energy_counterfactual.running_z, oracle cost trio, flow_policy._delta_bps.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.research.bflow import oracle
from src.research.bflow.flow_features import compute_features, _reindex_valid_frame
from src.research.bflow.energy_counterfactual import running_z
from src.research.bflow.flow_policy import _delta_bps

ZETAS = (1.0, 1.5, 2.0)
SCAN_START, SCAN_END = 30, 383          # decision minutes (inclusive)
MIN_NULL_OBS = 30                       # LOSO floor (spec §3)
GUARDRAIL_BPS = 10.0                    # p95-adverse margin (spec §3)
T_PASS = 3.0                            # cell pass bar (spec §3)
LEGS = ("LONG", "SHORT")


def trigger_z(df):
    """Trailing within-session z of vwap_disp_30 — conventions VERBATIM
    (t-inclusive, ddof=1, NaN guards live in running_z)."""
    return running_z(compute_features(df)["vwap_disp_30"])


def delta_vectors(df, p_eod_dump):
    """(G, C) length-390 float arrays. For decision minute m: fill = bar m+1.
    G[m]  = LONG gross bps of fill vw_{m+1} vs the dump.
    C[m]  = spread_bps(bar_{m+1}) − dump-window spread (differential cost).
    NaN where bar m+1 is invalid/absent or the dump is None/NaN."""
    work, _ = _reindex_valid_frame(df)
    G = np.full(390, np.nan)
    C = np.full(390, np.nan)
    p = oracle._f(p_eod_dump) if p_eod_dump is not None else float("nan")
    if not np.isfinite(p):
        return G, C
    dump_spread = oracle.eod_dump_window_spread_bps(df.to_dict("records"))
    if dump_spread is None:
        dump_spread = 0.0
    vw = work["vw"].to_numpy()
    h = work["h"].to_numpy()
    l = work["l"].to_numpy()
    for m in range(389):
        fill = vw[m + 1]
        if not (fill > 0):              # invalid/absent next bar (NaN-safe)
            continue
        G[m] = oracle.gross_bps(p, fill, "LONG")
        # fill-bar spread via the frozen oracle helper (never re-derive)
        entry_spread = oracle.spread_bps(
            {"vw": fill, "h": h[m + 1], "l": l[m + 1]})
        C[m] = entry_spread - dump_spread
    return G, C


def net_legs(G, C):
    """(net_long, net_short) from the shared vectors."""
    return G - C, -G - C


def simulate_pair(df, p_eod_dump, leg, zeta):
    """One (ticker, session, leg, zeta) entry. Spec §1: scan decision minutes
    [30, 383]; trigger = first t with z<=-zeta (LONG) / z>=+zeta (SHORT) AND a
    valid fill bar at t+1 (else VOID -> keep scanning); fill vw_{t+1}; never
    triggered -> forced dump fallback (delta = 0 BY CONSTRUCTION)."""
    z = trigger_z(df)
    G, C = delta_vectors(df, p_eod_dump)
    nl, ns = net_legs(G, C)
    net = nl if leg == "LONG" else ns
    zv = z.reindex(range(390)).to_numpy()
    for t in range(SCAN_START, SCAN_END + 1):
        ztv = zv[t]
        if not np.isfinite(ztv):
            continue
        hit = (ztv <= -zeta) if leg == "LONG" else (ztv >= zeta)
        if not hit:
            continue
        if not np.isfinite(net[t]):     # VOID: invalid fill bar — scan on
            continue
        return {"leg": leg, "zeta": zeta, "triggered": True,
                "entry_minute": t, "delta_net_bps": float(net[t]),
                "gross_at_entry": float(G[t]), "cost_at_entry": float(C[t]),
                "fallback": False}
    return {"leg": leg, "zeta": zeta, "triggered": False,
            "entry_minute": None, "delta_net_bps": 0.0,
            "gross_at_entry": float("nan"), "cost_at_entry": float("nan"),
            "fallback": True}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_mr_policy_pair.py -x -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/research/bflow/mr_policy.py tests/test_bflow_mr_policy_pair.py && git commit -m "bflow(1d): mr_policy delta vectors + pair simulation (pre-registered constants)"
```

---

### Task 2: session driver + eligibility

**Files:**
- Modify: `src/research/bflow/mr_policy.py` (append)
- Test: `tests/test_bflow_mr_policy_session.py`

- [ ] **Step 1: Write failing tests** — `tests/test_bflow_mr_policy_session.py`:

```python
import numpy as np
import pandas as pd

from research.bflow import mr_policy as mp


def _bar(m, p, v=1000.0):
    return {"minute": m, "o": p, "h": p + 0.2, "l": p - 0.2,
            "c": p, "v": v, "vw": p}


def _dippy(price=100.0):
    rows = [_bar(m, price) for m in range(390)]
    df = pd.DataFrame(rows)
    for m in range(60, 81):
        df.loc[df["minute"] == m, ["o", "h", "l", "c", "vw"]] = price * 0.95
    return df


def test_simulate_session_rows_full_grid():
    frames = {"AAA": _dippy(), "BBB": _dippy(50.0)}
    rows = mp.simulate_session_rows(frames, session="2024-01-05")
    # 2 tickers x 2 legs x 3 zetas, every eligible pair emits a row
    assert len(rows) == 12
    assert {r["session"] for r in rows} == {"2024-01-05"}
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    assert {(r["leg"], r["zeta"]) for r in rows} == {
        (l, z) for l in mp.LEGS for z in mp.ZETAS}


def test_simulate_session_rows_skips_no_dump():
    df = _dippy()
    df = df[df["minute"] < 385]          # no dump window -> ineligible
    rows = mp.simulate_session_rows({"AAA": df}, session="2024-01-05")
    assert rows == []


def test_simulate_session_rows_skips_thin_ticker():
    df = _dippy().head(40)               # < 60 valid bars -> floor reject
    rows = mp.simulate_session_rows({"AAA": df}, session="2024-01-05")
    assert rows == []


def test_session_delta_records_shape():
    frames = {"AAA": _dippy()}
    recs = mp.session_delta_records(frames, session="2024-01-05")
    df = pd.DataFrame(recs)
    assert set(df.columns) == {"session", "ticker", "minute", "G", "C"}
    # only minutes with finite G are emitted
    assert df["G"].notna().all()
    assert df["minute"].between(0, 388).all()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_mr_policy_session.py -x -q`
Expected: FAIL `AttributeError: ... 'simulate_session_rows'`

- [ ] **Step 3: Implement** — append to `src/research/bflow/mr_policy.py`:

```python
def _eligible_pair(tdf, dump):
    from src.research.bflow import predictability as pr
    if dump is None or not np.isfinite(oracle._f(dump)):
        return False
    return pr._valid_bar_count(tdf) >= pr.MIN_VALID_BARS


def simulate_session_rows(frames, session):
    """Pass-1 worker: all eligible tickers x LEGS x ZETAS for one session.
    Eligibility (spec §2): dump exists + registered 60-valid-bar floor.
    HOISTED HOT PATH (quality-review finding, Task-1 review): z/G/C are
    computed ONCE per (ticker, session) — 6x redundancy removed (~10.5h ->
    ~2.75h at full scale). Semantics identical to simulate_pair."""
    rows = []
    for ticker, tdf in frames.items():
        dump = oracle.dump_benchmark(tdf.to_dict("records"))
        if not _eligible_pair(tdf, dump):
            continue
        zv = trigger_z(tdf).to_numpy()
        G, C = delta_vectors(tdf, dump)
        nl, ns = net_legs(G, C)
        for leg in LEGS:
            net = nl if leg == "LONG" else ns
            for zeta in ZETAS:
                row = _scan(zv, net, G, C, leg, zeta)
                row.update({"session": session, "ticker": ticker})
                rows.append(row)
    return rows
```

**Also in Step 3 (refactor, Task-1 tests must stay green):** extract the scan loop
out of `simulate_pair` into `_scan(zv, net, G, C, leg, zeta)` (same body, takes
precomputed numpy arrays; docstring notes `entry_minute` = DECISION minute t,
fill at bar t+1 — distinct from energy_counterfactual's fill-minute convention).
`simulate_pair` becomes a thin wrapper: compute `zv = trigger_z(df).to_numpy()`,
`G, C = delta_vectors(df, p_eod_dump)`, pick the leg's net via `net_legs`, and
delegate to `_scan`. Two micro-cleanups from the Task-1 quality review while
touching these lines: `p = oracle._f(p_eod_dump)` directly (the `is not None`
ternary is redundant — `_f` handles None), and drop the no-op
`z.reindex(range(390))` (compute_features always returns the full 0..389 axis).

```python


def session_delta_records(frames, session):
    """Pass-2 worker: per-(ticker, minute) unconditional entry economics for
    the LOSO null — one record per minute with a finite G (valid fill bar)."""
    recs = []
    for ticker, tdf in frames.items():
        dump = oracle.dump_benchmark(tdf.to_dict("records"))
        if not _eligible_pair(tdf, dump):
            continue
        G, C = delta_vectors(tdf, dump)
        for m in np.flatnonzero(np.isfinite(G)):
            recs.append({"session": session, "ticker": ticker,
                         "minute": int(m), "G": float(G[m]),
                         "C": float(C[m])})
    return recs
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_mr_policy_session.py tests/test_bflow_mr_policy_pair.py -q`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/research/bflow/mr_policy.py tests/test_bflow_mr_policy_session.py && git commit -m "bflow(1d): session driver + eligibility (dump + 60-bar floor)"
```

---

### Task 3: LOSO null, excess stats, guardrail, verdict

**Files:**
- Modify: `src/research/bflow/mr_policy.py` (append)
- Test: `tests/test_bflow_mr_policy_null.py`

- [ ] **Step 1: Write failing tests** — `tests/test_bflow_mr_policy_null.py`:

```python
import numpy as np
import pandas as pd
import pytest

from research.bflow import mr_policy as mp


def _mk_rows(n_sessions, delta=5.0, minute=100, leg="LONG", zeta=1.0,
             gross=None, cost=0.0):
    # gross_at_entry is the LONG-signed G (own session's value in the
    # accumulator); for LONG with cost=0 it equals delta.
    g = delta if gross is None else gross
    return [{"session": f"2024-01-{d:02d}", "ticker": "AAA", "leg": leg,
             "zeta": zeta, "triggered": True, "entry_minute": minute,
             "delta_net_bps": delta, "gross_at_entry": g,
             "cost_at_entry": cost, "fallback": False}
            for d in range(1, n_sessions + 1)]


def _mk_recs(n_sessions, G=5.0, C=0.0, minute=100):
    return [{"session": f"2024-01-{d:02d}", "ticker": "AAA",
             "minute": minute, "G": G, "C": C}
            for d in range(1, n_sessions + 1)]


def test_loso_identity_constant_world():
    """In a constant world (every session identical), LOSO null == the value,
    so excess == 0 exactly. Verifies (sum - own)/(n-1) arithmetic."""
    acc = mp.NullAccumulator()
    acc.add_records(_mk_recs(40))
    scored, excluded = mp.score_rows(_mk_rows(40), acc)
    assert excluded == 0
    assert all(np.isclose(r["excess_bps"], 0.0) for r in scored)


def test_null_floor_excludes_thin():
    acc = mp.NullAccumulator()
    acc.add_records(_mk_recs(10))        # 10 - 1 = 9 LOSO obs < 30
    scored, excluded = mp.score_rows(_mk_rows(10), acc)
    assert excluded == 10
    assert scored == []                  # every row was triggered-and-thin


def test_fallback_excess_is_zero():
    acc = mp.NullAccumulator()
    rows = [{"session": "2024-01-01", "ticker": "AAA", "leg": "LONG",
             "zeta": 1.0, "triggered": False, "entry_minute": None,
             "delta_net_bps": 0.0, "gross_at_entry": float("nan"),
             "cost_at_entry": float("nan"), "fallback": True}]
    scored, excluded = mp.score_rows(rows, acc)
    assert excluded == 0
    assert scored[0]["excess_bps"] == 0.0


def test_short_leg_null_sign():
    """Null for SHORT uses -G - C (not the negated long null)."""
    acc = mp.NullAccumulator()
    acc.add_records(_mk_recs(40, G=5.0, C=1.0))
    rows = _mk_rows(40, delta=-6.0, leg="SHORT", gross=5.0, cost=1.0)
    scored, _ = mp.score_rows(rows, acc)
    # short null = mean(-G - C) over OTHER sessions = -6.0 -> excess 0
    assert all(np.isclose(r["excess_bps"], 0.0) for r in scored)


def test_cell_stats_clustered_t():
    """t = mean/(sd/sqrt(n)) over per-session means — registered shape."""
    rows = []
    rng = np.random.default_rng(7)
    for d in range(1, 41):
        rows.append({"session": f"2024-02-{d:02d}", "ticker": "AAA",
                     "leg": "LONG", "zeta": 1.0, "triggered": True,
                     "entry_minute": 100, "delta_net_bps": 5.0,
                     "gross_at_entry": 5.0, "cost_at_entry": 0.0,
                     "fallback": False,
                     "excess_bps": 2.0 + rng.normal(0, 0.5)})
    stats = mp.cell_stats(rows)
    cell = stats[("LONG", 1.0)]
    assert cell["n_sessions"] == 40
    sess_means = pd.DataFrame(rows).groupby("session")["excess_bps"].mean()
    t_expected = sess_means.mean() / (sess_means.std(ddof=1) / np.sqrt(40))
    assert np.isclose(cell["t"], t_expected)


def test_verdict_rules():
    # leg passes: >=2/3 cells with t >= +3 AND guardrail ok
    stats = {("LONG", 1.0): {"t": 3.5, "n_sessions": 800},
             ("LONG", 1.5): {"t": 3.2, "n_sessions": 800},
             ("LONG", 2.0): {"t": 1.0, "n_sessions": 800},
             ("SHORT", 1.0): {"t": -0.5, "n_sessions": 800},
             ("SHORT", 1.5): {"t": 0.2, "n_sessions": 800},
             ("SHORT", 2.0): {"t": 2.9, "n_sessions": 800}}
    guard = {("LONG", 1.0): {"policy_p95_adverse": 40.0, "pool_p95_adverse": 35.0},
             ("LONG", 1.5): {"policy_p95_adverse": 60.0, "pool_p95_adverse": 35.0},
             ("LONG", 2.0): {"policy_p95_adverse": 40.0, "pool_p95_adverse": 35.0},
             ("SHORT", 1.0): {"policy_p95_adverse": 30.0, "pool_p95_adverse": 35.0},
             ("SHORT", 1.5): {"policy_p95_adverse": 30.0, "pool_p95_adverse": 35.0},
             ("SHORT", 2.0): {"policy_p95_adverse": 30.0, "pool_p95_adverse": 35.0}}
    v = mp.leg_verdicts(stats, guard)
    # LONG: 2 cells pass t-bar, but cell (LONG,1.5) breaches 35+10 < 60
    assert v["LONG"] == "PASS-WITH-TAIL-BREACH"
    assert v["SHORT"] == "FAIL"
    guard[("LONG", 1.5)]["policy_p95_adverse"] = 44.0   # within 35+10
    assert mp.leg_verdicts(stats, guard)["LONG"] == "PASS"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_mr_policy_null.py -x -q`
Expected: FAIL `AttributeError: ... 'NullAccumulator'`

- [ ] **Step 3: Implement** — append to `src/research/bflow/mr_policy.py`:

```python
class NullAccumulator:
    """Streaming per-(ticker, minute) sums for the LOSO minute-matched null.
    Accumulates Σ G, Σ C, n and the raw values list per key (values are needed
    for the guardrail pool quantile; ~179k keys x ~800 floats is fine)."""

    def __init__(self):
        self.sum_g = {}
        self.sum_c = {}
        self.n = {}
        self.values = {}   # key -> {"G": [..], "C": [..], "session": [..]}

    def add_records(self, recs):
        for r in recs:
            k = (r["ticker"], r["minute"])
            self.sum_g[k] = self.sum_g.get(k, 0.0) + r["G"]
            self.sum_c[k] = self.sum_c.get(k, 0.0) + r["C"]
            self.n[k] = self.n.get(k, 0) + 1
            v = self.values.setdefault(k, {"G": [], "C": [], "session": []})
            v["G"].append(r["G"])
            v["C"].append(r["C"])
            v["session"].append(r["session"])

    def loso_null(self, ticker, minute, own_g, own_c, leg):
        """((Σ−own)/(n−1)) for the leg-signed net. None if n−1 < MIN_NULL_OBS."""
        k = (ticker, minute)
        n = self.n.get(k, 0)
        if n - 1 < MIN_NULL_OBS:
            return None
        g = (self.sum_g[k] - own_g) / (n - 1)
        c = (self.sum_c[k] - own_c) / (n - 1)
        return (g - c) if leg == "LONG" else (-g - c)

    def pool_values(self, ticker, minute, own_session, leg):
        """The matched pool {net(k, s', m): s' != own} for the guardrail."""
        k = (ticker, minute)
        v = self.values.get(k)
        if v is None:
            return []
        out = []
        for g, c, s in zip(v["G"], v["C"], v["session"]):
            if s == own_session:
                continue
            out.append((g - c) if leg == "LONG" else (-g - c))
        return out


def score_rows(rows, acc):
    """Attach excess_bps per spec §3. Returns (scored_rows, n_excluded).
    Fallback rows: excess = 0 by construction. Triggered rows with a thin
    null (LOSO obs < MIN_NULL_OBS) are EXCLUDED (dropped + counted)."""
    scored, excluded = [], 0
    for r in rows:
        if r["fallback"]:
            r = dict(r, excess_bps=0.0)
            scored.append(r)
            continue
        null = acc.loso_null(r["ticker"], r["entry_minute"],
                             r["gross_at_entry"], r["cost_at_entry"], r["leg"])
        if null is None:
            excluded += 1
            continue
        scored.append(dict(r, excess_bps=r["delta_net_bps"] - null))
    return scored, excluded


def cell_stats(scored_rows):
    """Per (leg, zeta): across-session mean of per-session mean excess and the
    clustered t = mean/(sd/sqrt(n_sessions)) — the registered statistic shape."""
    df = pd.DataFrame(scored_rows)
    out = {}
    if not len(df):
        return out
    for (leg, zeta), cell in df.groupby(["leg", "zeta"]):
        sm = cell.groupby("session")["excess_bps"].mean()
        n = len(sm)
        sd = sm.std(ddof=1)
        t = float(sm.mean() / (sd / np.sqrt(n))) if n >= 2 and sd > 0 else float("nan")
        out[(leg, float(zeta))] = {
            "mean_excess_bps": float(sm.mean()), "t": t, "n_sessions": n,
            "trigger_rate": float(cell["triggered"].mean()),
            "mean_delta_vs_dump_bps": float(
                cell.loc[cell["triggered"], "delta_net_bps"].mean())
            if cell["triggered"].any() else float("nan"),
        }
    return out


def guardrail_stats(scored_rows, acc):
    """Per (leg, zeta): p95 adverse of triggered policy deltas vs the pooled
    minute-matched distribution (spec §3 — own session excluded per entry)."""
    df = pd.DataFrame([r for r in scored_rows if r["triggered"]])
    out = {}
    if not len(df):
        return out
    for (leg, zeta), cell in df.groupby(["leg", "zeta"]):
        pol = -float(np.quantile(cell["delta_net_bps"], 0.05))
        pool = []
        for _, r in cell.iterrows():
            pool.extend(acc.pool_values(r["ticker"], r["entry_minute"],
                                        r["session"], leg))
        pl = -float(np.quantile(pool, 0.05)) if pool else float("nan")
        out[(leg, float(zeta))] = {"policy_p95_adverse": pol,
                                   "pool_p95_adverse": pl}
    return out


def leg_verdicts(stats, guard):
    """Spec §3: cell passes at t >= +3; leg passes with >=2/3 cells; every
    t-passing cell must satisfy the +10bps relative tail guardrail, else the
    leg is PASS-WITH-TAIL-BREACH (no shadow-lane authorization)."""
    verdicts = {}
    for leg in LEGS:
        passing = [z for z in ZETAS
                   if stats.get((leg, z), {}).get("t", float("nan")) >= T_PASS]
        if len(passing) < 2:
            verdicts[leg] = "FAIL"
            continue
        breach = False
        for z in passing:
            g = guard.get((leg, z), {})
            pol = g.get("policy_p95_adverse", float("nan"))
            pl = g.get("pool_p95_adverse", float("nan"))
            if not (pol <= pl + GUARDRAIL_BPS):
                breach = True
        verdicts[leg] = "PASS-WITH-TAIL-BREACH" if breach else "PASS"
    return verdicts


# 7 calendar buckets — IDENTICAL to scripts/bflow_phase1b_hist_evaluate.py
BUCKETS = (
    ("2023H1", "2023-01-01", "2023-06-30"),
    ("2023H2", "2023-07-01", "2023-12-31"),
    ("2024H1", "2024-01-01", "2024-06-30"),
    ("2024H2", "2024-07-01", "2024-12-31"),
    ("2025H1", "2025-01-01", "2025-06-30"),
    ("2025H2", "2025-07-01", "2025-12-31"),
    ("2026Q1", "2026-01-01", "2026-03-31"),
)


def diagnostics(scored_rows):
    """Spec §3 non-gating diagnostics per (leg, zeta): fallback rate, absolute
    tail P(delta_net < -tol) for tol in {5,10,25} (triggered rows), entry-minute
    deciles, and per-bucket mean session excess."""
    df = pd.DataFrame(scored_rows)
    out = {}
    if not len(df):
        return out
    for (leg, zeta), cell in df.groupby(["leg", "zeta"]):
        trig = cell[cell["triggered"]]
        d = {"fallback_rate": float(cell["fallback"].mean())}
        for tol in (5, 10, 25):
            d[f"p_adverse_{tol}"] = (
                float((trig["delta_net_bps"] < -tol).mean())
                if len(trig) else float("nan"))
        d["entry_minute_deciles"] = (
            [int(q) for q in np.quantile(
                trig["entry_minute"], [0.1, 0.5, 0.9])]
            if len(trig) else [])
        sm = cell.groupby("session")["excess_bps"].mean()
        buckets = {}
        for name, b0, b1 in BUCKETS:
            sub = sm[(sm.index >= b0) & (sm.index <= b1)]
            if len(sub):
                buckets[name] = {"mean_excess_bps": float(sub.mean()),
                                 "n_sessions": int(len(sub))}
        d["buckets"] = buckets
        out[(leg, float(zeta))] = d
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_mr_policy_null.py tests/test_bflow_mr_policy_session.py tests/test_bflow_mr_policy_pair.py -q`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw && git add src/research/bflow/mr_policy.py tests/test_bflow_mr_policy_null.py && git commit -m "bflow(1d): LOSO null accumulator + excess stats + guardrail + leg verdicts"
```

---

### Task 4: runner — two passes, report, verdict block

**Files:**
- Create: `scripts/run_bflow_phase1d.py`
- Test: `tests/test_bflow_phase1d_runner.py`

- [ ] **Step 1: Write failing test** — `tests/test_bflow_phase1d_runner.py`:

```python
"""End-to-end on a 3-session synthetic temp cache. The runner must (a) emit
policy_rows.parquet + report.md with a [bflow-p1d] VERDICT line, (b) never
fetch (poisoned-fetcher discipline lives in the cache layer; the runner only
reads files), (c) honor --limit."""
import os
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest


def _session_df(price=100.0, dip=None):
    rows = []
    for m in range(390):
        p = price * 0.95 if (dip and dip[0] <= m <= dip[1]) else price
        rows.append({"ticker": "AAA", "minute": m, "o": p, "h": p + 0.2,
                     "l": p - 0.2, "c": p, "v": 1000.0, "vw": p})
    return pd.DataFrame(rows)


@pytest.fixture
def tmp_cache(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for d, dip in [("2024-01-02", (60, 80)), ("2024-01-03", None),
                   ("2024-01-04", (200, 220))]:
        _session_df(dip=dip).to_parquet(cache / f"min_bars_{d}.parquet")
    return str(cache)


def test_runner_end_to_end(tmp_cache, tmp_path):
    analysis = str(tmp_path / "analysis")
    env = dict(os.environ, PYTHONPATH="src:.")
    proc = subprocess.run(
        [sys.executable, "scripts/run_bflow_phase1d.py",
         "--cache-dir", tmp_cache, "--analysis-dir", analysis],
        capture_output=True, text=True, env=env, cwd="/root/openclaw")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "[bflow-p1d] VERDICT" in proc.stdout
    assert os.path.exists(os.path.join(analysis, "report.md"))
    rows = pd.read_parquet(os.path.join(analysis, "policy_rows.parquet"))
    # 3 sessions x 1 ticker x 6 cells = 18 rows (3-session null is thin ->
    # triggered rows excluded, fallback rows survive; parquet keeps ALL
    # pre-scoring rows for audit)
    assert len(rows) == 18
    # with n-1 = 2 < 30 every triggered row must be excluded from scoring
    assert "excluded_thin_null" in proc.stdout
```

- [ ] **Step 2: Run to verify failure**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_phase1d_runner.py -x -q`
Expected: FAIL (script missing → returncode != 0)

- [ ] **Step 3: Implement** — `scripts/run_bflow_phase1d.py`:

```python
#!/usr/bin/env python3
"""SP-6 B-flow Phase-1d — MA-reversion entry policy runner (PRE-REGISTERED).

Spec (BINDING): docs/superpowers/specs/2026-06-06-sp6-bflow-phase1d-mr-policy-design.md
Two session-major passes over a minute-bar cache dir (CACHE-ONLY, never
fetches): pass 1 = policy rows; pass 2 = LOSO null records. Then scoring,
stats, guardrail, verdict, report. Zero free parameters at eval time.

Usage:
    PYTHONPATH=src:. python3 scripts/run_bflow_phase1d.py \
        --cache-dir data/cache/min_bars_hist \
        --analysis-dir analysis/bflow_phase1d [--limit N]
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "src"), _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def main(argv=None):
    p = argparse.ArgumentParser(prog="run_bflow_phase1d")
    p.add_argument("--cache-dir", required=True)
    p.add_argument("--analysis-dir", required=True)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args([] if argv is None else argv)

    import pandas as pd
    from research.bflow import mr_policy as mp
    from research.bflow.run_phase1b import (enumerate_cache_sessions,
                                            load_session_frame,
                                            _ticker_frames)

    sessions = enumerate_cache_sessions(args.cache_dir)
    if args.limit is not None:
        sessions = sessions[:args.limit]
    print(f"[bflow-p1d] cache {args.cache_dir}: {len(sessions)} sessions",
          flush=True)

    # ---- pass 1: policy rows ----
    all_rows = []
    for i, s in enumerate(sessions, 1):
        sdf = load_session_frame(args.cache_dir, s)
        if sdf is None or not len(sdf):
            continue
        rows = mp.simulate_session_rows(_ticker_frames(sdf), session=s)
        all_rows.extend(rows)
        print(f"[bflow-p1d][1] {s}: {len(rows)} rows ({i}/{len(sessions)})",
              flush=True)

    # ---- pass 2: LOSO null records ----
    acc = mp.NullAccumulator()
    for i, s in enumerate(sessions, 1):
        sdf = load_session_frame(args.cache_dir, s)
        if sdf is None or not len(sdf):
            continue
        acc.add_records(mp.session_delta_records(_ticker_frames(sdf),
                                                 session=s))
        print(f"[bflow-p1d][2] {s}: null accumulated ({i}/{len(sessions)})",
              flush=True)

    # ---- scoring + stats + verdict ----
    scored, excluded = mp.score_rows(all_rows, acc)
    stats = mp.cell_stats(scored)
    guard = mp.guardrail_stats(scored, acc)
    verdicts = mp.leg_verdicts(stats, guard)
    print(f"[bflow-p1d] excluded_thin_null={excluded}", flush=True)

    diag = mp.diagnostics(scored)

    os.makedirs(args.analysis_dir, exist_ok=True)
    rows_df = pd.DataFrame(all_rows)
    # entry_minute mixes None (fallback) with ints -> force float for parquet
    rows_df["entry_minute"] = rows_df["entry_minute"].astype("float64")
    rows_df.to_parquet(
        os.path.join(args.analysis_dir, "policy_rows.parquet"))

    lines = ["# Phase-1d MA-reversion policy — report", "",
             f"sessions: {len(sessions)}; rows: {len(all_rows)}; "
             f"excluded(thin null): {excluded}", "",
             "| leg | zeta | mean_excess_bps | t | n_sessions | trig_rate | "
             "mean_dvs_dump | pol_p95_adv | pool_p95_adv |",
             "|---|---|---|---|---|---|---|---|---|"]
    for leg in mp.LEGS:
        for z in mp.ZETAS:
            c = stats.get((leg, z), {})
            g = guard.get((leg, z), {})
            lines.append(
                f"| {leg} | {z} | {c.get('mean_excess_bps', float('nan')):.3f} "
                f"| {c.get('t', float('nan')):.2f} | {c.get('n_sessions', 0)} "
                f"| {c.get('trigger_rate', float('nan')):.3f} "
                f"| {c.get('mean_delta_vs_dump_bps', float('nan')):.2f} "
                f"| {g.get('policy_p95_adverse', float('nan')):.1f} "
                f"| {g.get('pool_p95_adverse', float('nan')):.1f} |")
    lines += ["", "## Diagnostics (non-gating, spec §3)", ""]
    for (leg, z), d in sorted(diag.items()):
        lines.append(
            f"- {leg} z={z}: fallback={d['fallback_rate']:.3f}; "
            f"P(adv>5/10/25bps)={d['p_adverse_5']:.3f}/"
            f"{d['p_adverse_10']:.3f}/{d['p_adverse_25']:.3f}; "
            f"entry-minute d10/50/90={d['entry_minute_deciles']}")
        for name, b in d["buckets"].items():
            lines.append(f"    - {name}: mean_excess="
                         f"{b['mean_excess_bps']:.3f}bps "
                         f"(n={b['n_sessions']})")
    lines += ["", f"**VERDICT: LONG={verdicts.get('LONG', 'FAIL')} "
                  f"SHORT={verdicts.get('SHORT', 'FAIL')}**", "",
              "Linkage (spec §0): PASS authorizes the FORWARD SHADOW LANE "
              "only — never live cutover. FAIL closes the idea at minute "
              "scale. PASS-WITH-TAIL-BREACH does not authorize the lane."]
    report = "\n".join(lines)
    with open(os.path.join(args.analysis_dir, "report.md"), "w") as fh:
        fh.write(report)
    print(report, flush=True)
    print(f"[bflow-p1d] VERDICT LONG={verdicts.get('LONG', 'FAIL')} "
          f"SHORT={verdicts.get('SHORT', 'FAIL')}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run to verify pass**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_phase1d_runner.py -x -q`
Expected: 1 passed

- [ ] **Step 5: Full Phase-1d suite + bflow regression**

Run: `cd /root/openclaw && PYTHONPATH=src:. nice -n 19 python3 -m pytest tests/test_bflow_mr_policy_pair.py tests/test_bflow_mr_policy_session.py tests/test_bflow_mr_policy_null.py tests/test_bflow_phase1d_runner.py tests/test_bflow_flow_policy.py tests/test_bflow_predictability.py -q`
Expected: all passed (18 new + existing bflow suites green; pre-existing failures OUTSIDE these files are not yours to fix)

- [ ] **Step 6: Commit**

```bash
cd /root/openclaw && git add scripts/run_bflow_phase1d.py tests/test_bflow_phase1d_runner.py && git commit -m "bflow(1d): runner — two-pass LOSO scoring, report + verdict block"
```

---

## Out of scope for builders (orchestrator-owned)

- Running against `data/cache/min_bars_hist/` (sequencing: kill-test evaluator FIRST — spec §4).
- Any change to the kill-test prereg, the live accrual cache, timers, or pushes to origin.

## Verification (orchestrator, after build)

1. All 4 task suites green sequentially.
2. Runner smoke on the 37-session LIVE cache is FORBIDDEN (it would peek at in-sample policy economics not previously observed — synthetic temp caches only).
3. After the kill-test evaluator has run: `PYTHONPATH=src:. nice -n 19 python3 scripts/run_bflow_phase1d.py --cache-dir data/cache/min_bars_hist --analysis-dir analysis/bflow_phase1d`.

---

## AMENDMENT A1 (post-Task-2 quality review) — Task 3/4 memory redesign

The original Task-3 `NullAccumulator` stored per-(ticker,minute) VALUE LISTS
(G/C/session per record). At full scale (~143M floats + 143M session strings)
that OOMs this box. SUPERSEDED design — spec §3 semantics unchanged:

- Accumulator stores ONLY per-(ticker,minute) running ΣG, ΣC, n (~179k keys)
  + per-cell weighted HISTOGRAMS for the guardrail pool (0.1bps bins,
  [−2000,+2000]bps clamped; weight = the cell's triggered-entry count at
  (ticker,minute), built from pass-1 rows BEFORE pass 2 via
  `build_cell_weights(rows)`).
- LOSO mean null per entry stays EXACT: ((ΣG−own_G)−(ΣC−own_C))/(n−1),
  leg-signed; floor MIN_NULL_OBS unchanged.
- Guardrail pool p95 read from the histogram AFTER subtracting 1 count at each
  triggered entry's own-value bin (own-session exclusion — algebra: weight
  contribution w·n minus one per entry = w·(n−1), exactly the spec pool).
  Quantization ≤ 0.1bps vs a pre-registered 10bps margin — accepted
  approximation, stated in the report.
- API deltas vs original Task-3 text: `NullAccumulator(cell_weights)`;
  `pool_p95_adverse(cell, own_values_bps)` replaces `pool_values()`;
  `guardrail_stats(scored, acc)` reads histograms; new `build_cell_weights`.
- Task-4 runner order: pass 1 (convert each session's rows to a compact
  DataFrame immediately; concat at end ≈ 200MB, never 2.4M live dicts) →
  `build_cell_weights` → pass 2 (`add_records` per session, transient dicts
  freed per iteration) → score → stats → guardrail → verdicts → report.
