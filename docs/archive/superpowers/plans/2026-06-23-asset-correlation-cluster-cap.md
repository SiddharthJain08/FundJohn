# Asset-Correlation Cluster Cap — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cap each correlated same-direction asset cluster's gross at `cap_pct×NAV`, releasing the trimmed (lowest-conviction) budget, to de-concentrate the live book and free day-trading buying power.

**Architecture:** Two new pure modules — `asset_correlation.py` (63-day price-return Pearson via a sliced pyarrow read) and `asset_correlation_filter.py` (within-direction clustering + per-cluster gross cap with release-not-redistribute) — wired into `regime_blended_sizer.py` immediately after the existing SP-6 per-ticker conviction cap (post λ×NAV scaling, dollar terms), behind two default-OFF gates. A one-shot report script drives measure-first calibration.

**Tech Stack:** Python 3, numpy, scipy (`scipy.cluster.hierarchy` — already used in `strategy_similarity.py`), pyarrow (predicate pushdown), pytest.

**Spec:** `docs/superpowers/specs/2026-06-23-asset-correlation-cluster-cap-design.md`

## Global Constraints

- **NEVER load the full `prices.parquet`** (728 MB). Always sliced read via pyarrow predicate pushdown on `ticker` + `date`. Path: `/root/openclaw/data/master/prices.parquet`.
- **Fail-open everywhere:** any correlation read/compute error must leave `target_usd` unchanged (never block a trading cycle).
- **Gate-OFF byte-identical:** with both `OPENCLAW_ASSET_CORR_CAP` and `OPENCLAW_ASSET_CORR_CAP_SHADOW` unset, `target_usd` is unchanged and no correlation work is done.
- **Release, never redistribute:** `Σ|out| ≤ Σ|in|` for every call; a survivor's `|target_usd|` is never increased.
- **Direction-aware:** cluster only within one direction (sign of `target_usd`); opposite-direction correlated pairs (hedges) are never co-clustered or trimmed.
- **No master-data writes, no DB migration.** Correlation computed on-demand; audit goes to logs.
- **Compute discipline:** the box is 2-core/8 GB with a backtest sweep running; any standalone script runs serial, `nice -n 19`, RSS < 1 GB.
- **Conviction signal:** `gate_net_sharpe` (the dict the per-ticker cap already uses at the insertion point). Rank by `|conviction|`; fall back to `|target_usd|` if missing.
- **Default params (placeholders until §6 calibration):** `window=63`, `corr_thr=0.70`, `cap_pct=0.22`, `single_name_cap_pct=None`.

## File Structure

- Create `src/execution/asset_correlation.py` — price-return correlation (pure math + sliced read).
- Create `src/execution/asset_correlation_filter.py` — clustering + per-cluster cap (pure).
- Modify `src/execution/regime_blended_sizer.py` — add `_apply_asset_corr_cap(...)` helper + one call site (~L1170).
- Create `scripts/asset_corr_cap_report.py` — one-shot calibration report.
- Create `tests/test_asset_correlation.py`, `tests/test_asset_correlation_filter.py`, `tests/test_asset_corr_cap_wiring.py`.

---

### Task 1: Pure correlation math (`corr_from_returns` + `_pearson`)

**Files:**
- Create: `src/execution/asset_correlation.py`
- Test: `tests/test_asset_correlation.py`

**Interfaces:**
- Produces: `corr_from_returns(returns: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]` (symmetric nested dict, diagonal 1.0; pairs with `< MIN_OBS` overlapping dates → 0.0). `MIN_OBS = 20`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_asset_correlation.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import asset_correlation as ac


def test_corr_perfect_and_anti():
    # 30 obs: b == a (corr +1), c == -a (corr -1)
    a = {f'2026-01-{i:02d}': (i % 7) - 3 + 0.1 * i for i in range(1, 31)}
    b = dict(a)
    c = {d: -v for d, v in a.items()}
    m = ac.corr_from_returns({'A': a, 'B': b, 'C': c})
    assert abs(m['A']['A'] - 1.0) < 1e-9
    assert abs(m['A']['B'] - 1.0) < 1e-9
    assert abs(m['A']['C'] + 1.0) < 1e-9
    assert m['A']['B'] == m['B']['A']


def test_thin_pair_is_zero():
    # only 5 overlapping obs (< MIN_OBS=20) -> 0.0, never cluster on thin evidence
    a = {f'2026-02-{i:02d}': float(i) for i in range(1, 6)}
    b = {f'2026-02-{i:02d}': float(i) for i in range(1, 6)}
    m = ac.corr_from_returns({'A': a, 'B': b})
    assert m['A']['B'] == 0.0


def test_zero_variance_is_zero():
    a = {f'2026-03-{i:02d}': 0.01 for i in range(1, 25)}   # flat
    b = {f'2026-03-{i:02d}': float(i) for i in range(1, 25)}
    m = ac.corr_from_returns({'A': a, 'B': b})
    assert m['A']['B'] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation.py -q`
Expected: FAIL (`ModuleNotFoundError: execution.asset_correlation` / attribute error).

- [ ] **Step 3: Write minimal implementation**

```python
# src/execution/asset_correlation.py
"""Asset-level (ticker) price-return correlation for the cluster-cap filter.

Memory-safe sliced read of data/master/prices.parquet via pyarrow predicate
pushdown (NEVER loads the full panel). Pearson on daily close-to-close returns
over a trailing window. Pure correlation math is separated for unit testing.
"""
from __future__ import annotations
import math

PARQUET = "/root/openclaw/data/master/prices.parquet"
MIN_OBS = 20            # min overlapping returns to trust a pair; else 0.0


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def corr_from_returns(returns: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Pairwise Pearson on {ticker: {date: ret}}. Diagonal 1.0; symmetric.
    Pairs with < MIN_OBS overlapping dates -> 0.0 (never cluster on thin evidence)."""
    tickers = sorted(returns)
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for t in tickers:
        out[t][t] = 1.0
    for i, a in enumerate(tickers):
        da = returns[a]
        for b in tickers[i + 1:]:
            db = returns[b]
            common = sorted(set(da) & set(db))
            if len(common) < MIN_OBS:
                rho = 0.0
            else:
                r = _pearson([da[d] for d in common], [db[d] for d in common])
                rho = 0.0 if r is None else max(-1.0, min(1.0, r))
            out[a][b] = out[b][a] = rho
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/execution/asset_correlation.py tests/test_asset_correlation.py
git commit -m "feat(corr): pure price-return Pearson math for asset-correlation filter"
```

---

### Task 2: Sliced parquet read + `price_return_corr` (fail-open)

**Files:**
- Modify: `src/execution/asset_correlation.py` (add `_load_returns`, `price_return_corr`)
- Test: `tests/test_asset_correlation.py` (add cases)

**Interfaces:**
- Consumes: `corr_from_returns` (Task 1).
- Produces: `price_return_corr(tickers, window=63, as_of=None) -> dict[str, dict[str, float]]`. Reads only `tickers` over the last `window+1` bars via predicate pushdown; fail-open returns `{}`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_asset_correlation.py
def test_price_return_corr_failopen(monkeypatch):
    # force the loader to raise -> fail-open empty dict (never blocks a cycle)
    def boom(*a, **k):
        raise RuntimeError("parquet unavailable")
    monkeypatch.setattr(ac, "_load_returns", boom)
    assert ac.price_return_corr(["MU", "WDC"], window=63) == {}


def test_price_return_corr_real_semis_are_correlated():
    # integration: MU and WDC (memory complex) should be positively correlated
    # over the last ~63d; XLF (financials) should be far less correlated to MU.
    m = ac.price_return_corr(["MU", "WDC", "XLF"], window=63)
    if not m:                                  # data unavailable in this env -> skip
        import pytest; pytest.skip("prices.parquet slice unavailable")
    assert m["MU"]["WDC"] > m["MU"]["XLF"]
    assert -1.0 <= m["MU"]["WDC"] <= 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation.py -q`
Expected: FAIL (`AttributeError: module ... has no attribute '_load_returns'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/execution/asset_correlation.py
def _load_returns(tickers, window, as_of=None):
    """Sliced read: daily close-to-close returns for `tickers` over the last
    `window`+1 trading days up to `as_of`. pyarrow predicate pushdown; never
    materializes the full panel. Returns {ticker: {date_str: ret}}."""
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    tickers = list(tickers)
    if not tickers:
        return {}
    flt = pc.field("ticker").isin(tickers)
    if as_of is not None:
        flt = flt & (pc.field("date") <= str(as_of))
    tbl = pq.read_table(PARQUET, columns=["ticker", "date", "close"], filters=flt)
    df = tbl.to_pandas()
    df["date"] = df["date"].astype(str)
    out: dict[str, dict[str, float]] = {}
    need = window + 1
    for tk, g in df.groupby("ticker"):
        g = g.sort_values("date").tail(need)
        closes = g["close"].astype(float).tolist()
        dates = g["date"].tolist()
        rets: dict[str, float] = {}
        for k in range(1, len(closes)):
            p = closes[k - 1]
            if p and p == p and closes[k] == closes[k]:   # nonzero + non-NaN
                rets[dates[k]] = closes[k] / p - 1.0
        out[str(tk)] = rets
    return out


def price_return_corr(tickers, window=63, as_of=None):
    """Ticker x ticker Pearson correlation of daily returns over the trailing
    window. Fail-open: any read/compute error -> {} (caller applies no capping)."""
    try:
        return corr_from_returns(_load_returns(tickers, window, as_of))
    except Exception:
        return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation.py -q`
Expected: PASS (5 passed, or 4 passed + 1 skipped if the parquet slice is unavailable).

- [ ] **Step 5: Commit**

```bash
git add src/execution/asset_correlation.py tests/test_asset_correlation.py
git commit -m "feat(corr): sliced parquet loader + fail-open price_return_corr"
```

---

### Task 3: Within-direction clustering (`_cluster_same_direction`)

**Files:**
- Create: `src/execution/asset_correlation_filter.py`
- Test: `tests/test_asset_correlation_filter.py`

**Interfaces:**
- Produces: `_cluster_same_direction(tickers: list[str], sign: dict[str,int], corr: dict, corr_thr: float) -> list[list[str]]` — average-linkage clusters computed separately per direction; every ticker appears in exactly one cluster (singletons included).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_asset_correlation_filter.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import asset_correlation_filter as acf


def _corr(pairs, tickers):
    m = {a: {b: (1.0 if a == b else 0.0) for b in tickers} for a in tickers}
    for (a, b), v in pairs.items():
        m[a][b] = m[b][a] = v
    return m


def test_clusters_group_same_direction_correlated():
    tickers = ['MU', 'WDC', 'XLF']
    sign = {'MU': 1, 'WDC': 1, 'XLF': 1}
    corr = _corr({('MU', 'WDC'): 0.9, ('MU', 'XLF'): 0.1, ('WDC', 'XLF'): 0.1}, tickers)
    clusters = acf._cluster_same_direction(tickers, sign, corr, 0.70)
    sets = sorted([sorted(c) for c in clusters])
    assert ['MU', 'WDC'] in sets and ['XLF'] in sets


def test_opposite_direction_never_coclustered():
    # MU long + WDC short, highly correlated -> a hedge -> separate clusters
    tickers = ['MU', 'WDC']
    sign = {'MU': 1, 'WDC': -1}
    corr = _corr({('MU', 'WDC'): 0.95}, tickers)
    clusters = acf._cluster_same_direction(tickers, sign, corr, 0.70)
    assert sorted([sorted(c) for c in clusters]) == [['MU'], ['WDC']]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation_filter.py -q`
Expected: FAIL (module/attribute missing).

- [ ] **Step 3: Write minimal implementation**

```python
# src/execution/asset_correlation_filter.py
"""Per-cluster gross cap on correlated, same-direction assets. Pure.

Clusters same-direction tickers by price-return correlation (average-linkage,
cut at distance 1 - corr_thr), keeps the highest-conviction names until the
cluster's cumulative gross hits cap_pct * NAV (boundary name trimmed to fill),
and RELEASES the rest (target -> 0; never redistributes). Gross is monotonically
non-increasing. Mirrors the SP-6 per-ticker conviction cap (release, no renorm).
"""
from __future__ import annotations


def _cluster_same_direction(tickers, sign, corr, corr_thr):
    """Average-linkage clusters computed separately within each direction.
    Returns list[list[ticker]]; singletons included."""
    import numpy as np
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform
    out = []
    for d in (1, -1):
        grp = sorted([t for t in tickers if sign.get(t) == d])
        n = len(grp)
        if n == 0:
            continue
        if n == 1:
            out.append([grp[0]])
            continue
        dist = np.zeros((n, n))
        for i in range(n):
            for k in range(i + 1, n):
                c = max(-1.0, min(1.0, corr.get(grp[i], {}).get(grp[k], 0.0)))
                dist[i][k] = dist[k][i] = 1.0 - c
        Z = linkage(squareform(dist, checks=False), method='average')
        labels = fcluster(Z, t=1.0 - corr_thr, criterion='distance')
        groups = {}
        for idx, lab in enumerate(labels):
            groups.setdefault(int(lab), []).append(grp[idx])
        out.extend(groups.values())
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation_filter.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/execution/asset_correlation_filter.py tests/test_asset_correlation_filter.py
git commit -m "feat(corr-filter): within-direction average-linkage clustering"
```

---

### Task 4: Per-cluster cap with release (`cap_correlated_clusters`)

**Files:**
- Modify: `src/execution/asset_correlation_filter.py` (add `cap_correlated_clusters`)
- Test: `tests/test_asset_correlation_filter.py` (add cases)

**Interfaces:**
- Consumes: `_cluster_same_direction` (Task 3).
- Produces: `cap_correlated_clusters(target_usd, conviction, corr, nav, cap_pct=0.22, corr_thr=0.70, single_name_cap_pct=None) -> tuple[dict[str,float], dict]`. Returns `(capped_target_usd, audit)`. Invariants INV-1..5 from the spec.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_asset_correlation_filter.py
def test_cap_keeps_top_conviction_trims_boundary_releases_rest():
    # Three correlated longs, each $40 target, NAV=$100, cap=22% -> cluster cap $22.
    # Conviction A>B>C. Keep A ($40? no - cap is $22) -> A trimmed to $22, B and C released.
    tgt = {'A': 40.0, 'B': 40.0, 'C': 40.0}
    conv = {'A': 3.0, 'B': 2.0, 'C': 1.0}
    corr = _corr({('A', 'B'): 0.9, ('A', 'C'): 0.9, ('B', 'C'): 0.9}, list(tgt))
    out, audit = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert abs(out['A'] - 22.0) < 1e-6          # top conviction trimmed to fill cap
    assert out['B'] == 0.0 and out['C'] == 0.0  # released, not redistributed
    assert audit['total_gross_after'] <= audit['total_gross_before']  # INV-1
    assert abs(audit['released_usd'] - 98.0) < 1e-6  # 120 -> 22


def test_cap_keeps_multiple_until_cap():
    # Two correlated longs $10 each, NAV 100, cap 22% = $22 -> both fit ($20 < $22).
    tgt = {'A': 10.0, 'B': 10.0}
    conv = {'A': 2.0, 'B': 1.0}
    corr = _corr({('A', 'B'): 0.9}, list(tgt))
    out, _ = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert out['A'] == 10.0 and out['B'] == 10.0  # under cap -> untouched


def test_uncorrelated_untouched():
    tgt = {'A': 40.0, 'B': 40.0}
    conv = {'A': 2.0, 'B': 1.0}
    corr = _corr({('A', 'B'): 0.1}, list(tgt))   # not correlated -> separate singletons
    out, _ = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert out == tgt                              # singletons, no single_name cap -> unchanged


def test_failopen_empty_corr_is_noop():
    tgt = {'A': 40.0, 'B': 40.0}
    out, _ = acf.cap_correlated_clusters(tgt, {}, {}, nav=100.0, cap_pct=0.22)
    assert out == tgt                              # INV-5


def test_gross_never_increases_and_no_redistribution():
    tgt = {'A': 50.0, 'B': 30.0, 'C': 30.0}
    conv = {'A': 3.0, 'B': 2.0, 'C': 1.0}
    corr = _corr({('A', 'B'): 0.9, ('A', 'C'): 0.9, ('B', 'C'): 0.9}, list(tgt))
    out, _ = acf.cap_correlated_clusters(tgt, conv, corr, nav=100.0, cap_pct=0.22)
    assert sum(abs(v) for v in out.values()) <= sum(abs(v) for v in tgt.values())  # INV-1
    for t in tgt:
        assert abs(out[t]) <= abs(tgt[t]) + 1e-9   # INV-2 no survivor grows
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation_filter.py -q`
Expected: FAIL (`AttributeError: ... cap_correlated_clusters`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/execution/asset_correlation_filter.py
def cap_correlated_clusters(target_usd, conviction, corr, nav,
                            cap_pct=0.22, corr_thr=0.70, single_name_cap_pct=None):
    """Cap each correlated same-direction cluster's gross at cap_pct*nav by keeping
    top-|conviction| names (boundary trimmed to fill) and releasing the rest.
    Pure; gross never increases; no redistribution. Returns (capped, audit)."""
    gross_in = sum(abs(v) for v in target_usd.values())
    base_audit = {'clusters': [], 'total_gross_before': gross_in,
                  'total_gross_after': gross_in, 'released_usd': 0.0}
    if not target_usd or nav <= 0 or not corr:
        return dict(target_usd), base_audit            # INV-5 fail-open
    sign = {t: (1 if v > 0 else -1) for t, v in target_usd.items()}
    cap_usd = cap_pct * nav
    clusters = _cluster_same_direction(list(target_usd), sign, corr, corr_thr)
    out = dict(target_usd)
    audit_clusters = []

    def rank_key(t):
        c = conviction.get(t)
        mag = abs(c) if c is not None else abs(target_usd[t])
        return (mag, abs(target_usd[t]), t)            # deterministic tie-breaks

    for cl in clusters:
        if len(cl) == 1:
            if single_name_cap_pct is None:
                continue                               # singleton, no cluster cap
            eff_cap = single_name_cap_pct * nav
        else:
            eff_cap = cap_usd
        ordered = sorted(cl, key=rank_key, reverse=True)
        gross_before = sum(abs(target_usd[t]) for t in cl)
        kept, trimmed, released = [], [], []
        cum = 0.0
        for t in ordered:
            amt = abs(target_usd[t]); s = sign[t]
            if cum + amt <= eff_cap + 1e-9:
                cum += amt; kept.append((t, out[t]))
            elif cum < eff_cap:
                out[t] = s * (eff_cap - cum)           # boundary partial fill
                trimmed.append((t, target_usd[t], out[t])); cum = eff_cap
            else:
                released.append((t, target_usd[t])); out[t] = 0.0
        audit_clusters.append({'members': cl, 'direction': sign[cl[0]],
                               'gross_before': gross_before, 'gross_after': cum,
                               'kept': kept, 'trimmed': trimmed, 'released': released})
    gross_out = sum(abs(v) for v in out.values())
    return out, {'clusters': audit_clusters, 'total_gross_before': gross_in,
                 'total_gross_after': gross_out, 'released_usd': gross_in - gross_out}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_correlation_filter.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/execution/asset_correlation_filter.py tests/test_asset_correlation_filter.py
git commit -m "feat(corr-filter): per-cluster gross cap with release (INV-1..5)"
```

---

### Task 5: Wire the gated cap into the live sizer

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (add `_apply_asset_corr_cap` near the other private helpers; add one call after the SP-6 per-ticker cap block, before the option-hedge injection at ~L1171)
- Test: `tests/test_asset_corr_cap_wiring.py`

**Interfaces:**
- Consumes: `asset_correlation.price_return_corr`, `asset_correlation_filter.cap_correlated_clusters`.
- Produces: `_apply_asset_corr_cap(target_usd: dict, conviction: dict, nav: float) -> dict` — gate-aware (`OPENCLAW_ASSET_CORR_CAP` applies, `..._SHADOW` logs-only, both unset = identity), fail-open.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_asset_corr_cap_wiring.py
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import regime_blended_sizer as rbs
from execution import asset_correlation as ac


def _fake_corr(monkeypatch):
    # MU/WDC correlated longs; XLF separate. Deterministic, no parquet read.
    def fake(tickers, window=63, as_of=None):
        m = {a: {b: (1.0 if a == b else 0.0) for b in tickers} for a in tickers}
        if 'MU' in tickers and 'WDC' in tickers:
            m['MU']['WDC'] = m['WDC']['MU'] = 0.9
        return m
    monkeypatch.setattr(ac, 'price_return_corr', fake)


def test_gate_off_is_identity(monkeypatch):
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP', raising=False)
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP_SHADOW', raising=False)
    tgt = {'MU': 40.0, 'WDC': 40.0, 'XLF': 20.0}
    out = rbs._apply_asset_corr_cap(dict(tgt), {'MU': 2, 'WDC': 1, 'XLF': 3}, nav=100.0)
    assert out == tgt


def test_shadow_logs_but_does_not_change(monkeypatch):
    _fake_corr(monkeypatch)
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP', raising=False)
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_SHADOW', '1')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_PCT', '0.22')
    tgt = {'MU': 40.0, 'WDC': 40.0, 'XLF': 20.0}
    out = rbs._apply_asset_corr_cap(dict(tgt), {'MU': 2, 'WDC': 1, 'XLF': 3}, nav=100.0)
    assert out == tgt                              # shadow never changes targets


def test_apply_caps_correlated_cluster(monkeypatch):
    _fake_corr(monkeypatch)
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP', '1')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_PCT', '0.22')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_THR', '0.70')
    tgt = {'MU': 40.0, 'WDC': 40.0, 'XLF': 20.0}   # MU+WDC cluster $80 -> cap $22
    out = rbs._apply_asset_corr_cap(dict(tgt), {'MU': 2, 'WDC': 1, 'XLF': 3}, nav=100.0)
    assert abs(out['MU'] - 22.0) < 1e-6            # top conviction trimmed to cap
    assert out['WDC'] == 0.0                       # released
    assert out['XLF'] == 20.0                      # uncorrelated, untouched
    assert sum(abs(v) for v in out.values()) < sum(abs(v) for v in tgt.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_corr_cap_wiring.py -q`
Expected: FAIL (`AttributeError: ... _apply_asset_corr_cap`).

- [ ] **Step 3: Write minimal implementation**

Add this helper in `src/execution/regime_blended_sizer.py` (near the other module-level `_`-helpers, e.g. just above `_choose_bracket`). `os` and `logger` are already imported in this module.

```python
def _apply_asset_corr_cap(target_usd, conviction, nav):
    """Asset-correlation cluster cap (gated, post λ×NAV scaling, dollar terms).
    OPENCLAW_ASSET_CORR_CAP=1 applies the cap; OPENCLAW_ASSET_CORR_CAP_SHADOW=1
    logs the would-be cap without changing targets; both unset -> identity (no
    correlation work). Fail-open: any error returns target_usd unchanged."""
    apply_on = os.environ.get('OPENCLAW_ASSET_CORR_CAP') == '1'
    shadow_on = os.environ.get('OPENCLAW_ASSET_CORR_CAP_SHADOW') == '1'
    if not (apply_on or shadow_on):
        return target_usd
    try:
        from execution import asset_correlation as _ac
        from execution import asset_correlation_filter as _acf
        cap_pct = float(os.environ.get('OPENCLAW_ASSET_CORR_CAP_PCT', '0.22'))
        corr_thr = float(os.environ.get('OPENCLAW_ASSET_CORR_THR', '0.70'))
        window = int(os.environ.get('OPENCLAW_ASSET_CORR_WINDOW', '63'))
        corr = _ac.price_return_corr(list(target_usd), window=window)
        capped, audit = _acf.cap_correlated_clusters(
            target_usd, conviction, corr, nav, cap_pct=cap_pct, corr_thr=corr_thr)
        logger.info(
            'asset_corr_cap.%s: clusters>=2=%d gross %.0f->%.0f released=%.0f %s',
            'apply' if apply_on else 'shadow',
            sum(1 for c in audit['clusters'] if len(c['members']) >= 2),
            audit['total_gross_before'], audit['total_gross_after'], audit['released_usd'],
            [(c['members'], round(c['gross_before']), round(c['gross_after']))
             for c in audit['clusters'] if c['released'] or c['trimmed']][:10])
        return capped if apply_on else target_usd
    except Exception as e:
        logger.warning('asset_corr_cap failed (%s); fail-open', e)
        return target_usd
```

Then add the call site. Find the end of the SP-6 per-ticker conviction-cap block (the `if _capped:` logging, ~L1164-1169) and the option-hedge injection comment (`# SP-5.1b-ii: inject option delta-hedge ...`, ~L1171). Insert BETWEEN them:

```python
    # Asset-correlation cluster cap (gated, default-OFF). De-grosses correlated
    # same-direction clusters by releasing low-conviction redundancy (no renorm),
    # mirroring the per-ticker cap above but at the cluster level. Applied BEFORE
    # the cap-exempt option-hedge injection + broker netting.
    target_usd = _apply_asset_corr_cap(target_usd, gate_net_sharpe, nav)
```

- [ ] **Step 4: Run tests to verify they pass + no regressions**

Run: `cd /root/openclaw && python3 -m pytest tests/test_asset_corr_cap_wiring.py tests/test_asset_correlation.py tests/test_asset_correlation_filter.py -q`
Expected: PASS (all). Then a byte-identical smoke of the sizer path:
Run: `cd /root/openclaw && python3 -c "import sys; sys.path.insert(0,'src'); from execution import regime_blended_sizer"` (imports clean, no syntax error).
Expected: no output / exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/test_asset_corr_cap_wiring.py
git commit -m "feat(sizer): wire gated asset-correlation cluster cap (default-OFF)"
```

---

### Task 6: Measure-first calibration report

**Files:**
- Create: `scripts/asset_corr_cap_report.py`

**Interfaces:**
- Consumes: `asset_correlation.price_return_corr`, `asset_correlation_filter.cap_correlated_clusters`, the live broker book (Alpaca CLI) for the current-book view.

This task has no unit test; its deliverable is a runnable report that produces the numbers the operator uses to pick `cap_pct` / `corr_thr` / `window` before flipping the gate live.

- [ ] **Step 1: Write the report script**

```python
# scripts/asset_corr_cap_report.py
"""Measure-first calibration for the asset-correlation cluster cap.

For the CURRENT live book (Alpaca positions): build price-return correlation,
then sweep (corr_thr x cap_pct) and report, per cell: clusters found, gross
before->after, released $ (== DTBP freed at the position level), and the
sell-down (which currently-held names get trimmed). Read-only. Serial/nice.

Usage (with Alpaca + parquet env):
  nice -n 19 python3 scripts/asset_corr_cap_report.py
"""
import os, sys, json, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
from execution import asset_correlation as ac
from execution import asset_correlation_filter as acf

ALPACA = os.environ.get('ALPACA_BIN', '/root/go/bin/alpaca')


def _positions():
    r = subprocess.run([ALPACA, 'position', 'list'], capture_output=True, text=True)
    return json.loads(r.stdout)


def main():
    pos = _positions()
    # signed target == current market value (the book we'd be capping)
    target = {p['symbol']: float(p['market_value']) for p in pos}
    conv = {p['symbol']: abs(float(p['market_value'])) for p in pos}  # proxy until run live
    # NAV from account equity
    acct = json.loads(subprocess.run([ALPACA, 'account', 'get'],
                                     capture_output=True, text=True).stdout)
    nav = float(acct['equity'])
    window = int(os.environ.get('OPENCLAW_ASSET_CORR_WINDOW', '63'))
    corr = ac.price_return_corr(list(target), window=window)
    if not corr:
        print('no correlation (parquet unavailable)'); return 1
    print(f'book: {len(target)} names, gross=${sum(abs(v) for v in target.values()):,.0f}, '
          f'NAV=${nav:,.0f}, window={window}d')
    for corr_thr in (0.6, 0.7, 0.8):
        for cap_pct in (0.15, 0.20, 0.25):
            out, audit = acf.cap_correlated_clusters(target, conv, corr, nav,
                                                     cap_pct=cap_pct, corr_thr=corr_thr)
            sells = [(t, round(target[t]), round(out[t]))
                     for t in target if abs(out[t]) < abs(target[t]) - 1.0]
            mb = [c['members'] for c in audit['clusters'] if len(c['members']) >= 2]
            print(f'thr={corr_thr} cap={cap_pct:.0%}: clusters>=2={len(mb)} '
                  f'gross ${audit["total_gross_before"]:,.0f}->${audit["total_gross_after"]:,.0f} '
                  f'released(DTBP freed)=${audit["released_usd"]:,.0f} sells={len(sells)}')
            if corr_thr == 0.7 and cap_pct == 0.20:
                print('   clusters:', mb)
                print('   sell-downs:', sells)
    return 0


if __name__ == '__main__':
    sys.exit(main())
```

- [ ] **Step 2: Run the report (shadow / read-only)**

Run (with Alpaca + parquet env exported):
`cd /root/openclaw && nice -n 19 python3 scripts/asset_corr_cap_report.py`
Expected: a table of (thr × cap) cells with clusters / gross before→after / released $ / sell count, plus the detailed clusters + sell-downs for the thr=0.7/cap=20% cell. Sanity: the semis (SNDK/MU/WDC/STX/AMAT/AMD/COHR/LITE/CIEN/VRT) should land in one cluster and the released $ should be roughly the semi gross above the cap.

- [ ] **Step 3: Commit**

```bash
git add scripts/asset_corr_cap_report.py
git commit -m "feat(corr): measure-first calibration report for cluster cap"
```

---

## Self-Review

**1. Spec coverage:**
- D1 price-return correlation → Tasks 1–2. ✓
- D2 release/de-gross (no redistribute) → Task 4 (`cap_correlated_clusters`, INV-1/2 tests). ✓
- D3 per-cluster gross cap (keep top-conviction, boundary trim) → Task 4. ✓
- D4 full-book scope (caps the intended `target_usd` → netting emits sell-downs) → Task 5 wiring + Task 6 report quantifies the sell-down. ✓
- Direction-aware / hedges untouched → Task 3 + test_opposite_direction. ✓
- Post-scaling dollar-terms insertion → Task 5 call site (after L1169, before L1171). ✓
- Gates default-OFF + shadow + byte-identical → Task 5 tests. ✓
- Fail-open + never load full panel → Tasks 2 (`price_return_corr` try/except, pyarrow filter) + 4 (empty-corr no-op) + 5 (helper try/except). ✓
- Measure-first calibration (cap%, thr, window, Jun22→23) → Task 6. (Jun22→23 counterfactual is a follow-on use of the same script with historical `as_of`; current-book + sweep covered. ✓)
- Five invariants → Task 4 tests (INV-1,2,5 explicit; INV-3 Task 3; INV-4 deterministic tie-breaks in `rank_key`). ✓

**2. Placeholder scan:** No "TBD/TODO" in steps. `single_name_cap_pct` default `None` is an explicit, implemented value (not a placeholder); calibration of it is parked in the spec, code path tested via default. ✓

**3. Type consistency:** `price_return_corr(tickers, window, as_of)` and `cap_correlated_clusters(target_usd, conviction, corr, nav, cap_pct, corr_thr, single_name_cap_pct)` signatures match across Tasks 2/4/5/6. `corr` is `dict[str,dict[str,float]]` everywhere; `target_usd`/`out` signed dollar dicts; conviction dict keyed by ticker. `_cluster_same_direction(tickers, sign, corr, corr_thr)` consumed only by `cap_correlated_clusters`. ✓

---

## Notes for the executor

- The repo working tree currently has unrelated operator changes (manifest.json, registry.py) and the recent bracket-stack max-take edits, all uncommitted. **Stage only the files listed per task** in each commit — do not `git add -A`.
- Do not switch the `/root/openclaw` branch (johnbot runs from it). Commit on the current branch.
- The change goes live on the next sizer subprocess invocation once the gate is set; no johnbot restart is required. Keep both gates OFF until the Task 6 report is reviewed and `cap_pct`/`corr_thr` chosen.
