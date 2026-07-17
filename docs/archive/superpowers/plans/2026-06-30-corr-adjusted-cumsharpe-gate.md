# Correlation-Adjusted Cumulative-Sharpe Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the naive cumulative-Sharpe conviction gate + block-heuristic deflation with a single, signed, correlation-adjusted (Sharpe-weighted) combination Sharpe that drives **both** the per-regime ticker-selection gate and the position-sizing weight, behind a default-OFF flag with a shadow rail.

**Architecture:** A pure function `corr_adjusted_net_sharpe` computes `S_adj(tkr)=Σwᵢ²dᵢ / √q` (inverse-free, signed, with an inert non-PSD backstop) from the live per-regime `strategy_similarity_matrix`. `regime_blended_sizer._sharpe_cadence_path` consumes it behind `OPENCLAW_STRATEGY_CORR_CUMSHARPE`: the **gate** quantity uses raw cadence-normalized `daily_weight` (size_scalar-exempt) and the **sizing** quantity uses `eff_weight` (size_scalar folded in) — identical when all scalars=1. A new per-regime floor `min_corr_cum_sharpe` (migration 140) is dashboard-controlled exactly like `min_cumulative_sharpe`. A shadow flag logs distribution / would-drop / Δ$ / backstop-fires / recommended-floor without routing.

**Tech Stack:** Python 3 (stdlib `math` only — no numpy on the hot path), PostgreSQL (`regime_sizer_params`, `pipeline_config`), Node/Express dashboard (`src/channels/api/server.js`), pytest.

## Global Constraints

- **NEVER DELETE FROM MASTER DB.** Migration 140 is **additive only** (`ADD COLUMN IF NOT EXISTS`); no DROP/DELETE/truncate. Leave legacy `min_cumulative_sharpe` + `deflated_net_sharpe` intact (rollback path).
- **Default-OFF, byte-identical when OFF.** `OPENCLAW_STRATEGY_CORR_CUMSHARPE` and `OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW` both default OFF; with both unset every code path must be byte-identical to current production. The existing sizer regression suite (`tests/test_regime_blended_sizer*.py`, `tests/test_orthogonalization_sizer.py`, `tests/test_sizer_per_ticker_cap.py`) must still pass.
- **Work only in the worktree** `/root/wt-corr-cumsharpe` (branch `feat/corr-adjusted-cumsharpe-gate`). Never edit `/root/openclaw` (live production tree). Subagents must `cd /root/wt-corr-cumsharpe`.
- **No live deploy in this plan.** No `git push`, no migration apply to the live DB, no johnbot restart. Those are operator-gated rollout steps after review.
- **Sparse default ρ = 0.05; off-diagonal clip ±0.95** (existing `strategy_similarity` conventions). `min_corr_cum_sharpe` bound `[0.0, 10.0]`; legacy `min_cumulative_sharpe` bound `[1.0, 10.0]` (do not change).
- ρ basis = cadence-normalized `daily_weight = effective_sharpe/√cadence` (operator decision); the result is labeled **approximate (similarity-proxy)**.

---

### Task 1: Pure function `corr_adjusted_net_sharpe`

**Files:**
- Modify: `src/execution/orthogonalization.py` (add `import math`, `SPARSE_DEFAULT`, new function)
- Test: `tests/test_corr_adjusted_net_sharpe.py` (create)

**Interfaces:**
- Produces: `corr_adjusted_net_sharpe(contribs_by_ticker: dict[str, list[tuple[str, int]]], sim: dict[str, dict[str, float]], weight_by_strat: dict[str, float], eps: float = 1e-9) -> tuple[dict[str, float], int]` — returns `({ticker: signed S_adj}, n_backstop_fires)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_corr_adjusted_net_sharpe.py`:

```python
"""Pure-function tests for the correlation-adjusted cumulative-Sharpe gate."""
from __future__ import annotations
import math, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import pytest  # noqa: E402
from execution.orthogonalization import corr_adjusted_net_sharpe  # noqa: E402


def _sim(sids, rho=0.0):
    """Similarity matrix: diagonal 1.0, all off-diagonal = rho."""
    m = {a: {} for a in sids}
    for a in sids:
        m[a][a] = 1.0
        for b in sids:
            if a != b:
                m[a][b] = rho
    return m


def test_single_strategy_equals_weight_times_direction():
    out, nb = corr_adjusted_net_sharpe({'AAA': [('s1', 1)]}, _sim(['s1']), {'s1': 3.0})
    assert nb == 0
    assert out['AAA'] == pytest.approx(3.0)          # w1^2 / sqrt(w1^2) = w1


def test_single_short_is_negative():
    out, _ = corr_adjusted_net_sharpe({'AAA': [('s1', -1)]}, _sim(['s1']), {'s1': 4.0})
    assert out['AAA'] == pytest.approx(-4.0)


def test_n_independent_same_direction_sqrtN():
    sids = ['s1', 's2', 's3', 's4']
    contribs = {'AAA': [(s, 1) for s in sids]}
    out, nb = corr_adjusted_net_sharpe(contribs, _sim(sids, 0.0), {s: 2.0 for s in sids})
    assert nb == 0
    assert out['AAA'] == pytest.approx(4.0)          # sqrt(4) * 2 = 4


def test_rho_one_same_direction_no_double_count():
    sids = ['s1', 's2', 's3', 's4']
    contribs = {'AAA': [(s, 1) for s in sids]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(sids, 1.0), {s: 2.0 for s in sids})
    assert out['AAA'] == pytest.approx(2.0)          # duplicate gets zero extra credit


def test_unequal_uncorrelated_is_quadrature():
    contribs = {'AAA': [('s1', 1), ('s2', 1)]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(['s1', 's2'], 0.0), {'s1': 5.0, 's2': 1.0})
    assert out['AAA'] == pytest.approx(math.sqrt(26.0))   # 26 / sqrt(26)


def test_two_opposing_equal_cancels_to_zero():
    contribs = {'AAA': [('s1', 1), ('s2', -1)]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(['s1', 's2'], 0.0), {'s1': 5.0, 's2': 5.0})
    assert out['AAA'] == pytest.approx(0.0)


def test_opposing_unequal_signed_dominance():
    contribs = {'AAA': [('s1', 1), ('s2', -1)]}
    out, _ = corr_adjusted_net_sharpe(contribs, _sim(['s1', 's2'], 0.0), {'s1': 5.0, 's2': 4.0})
    assert out['AAA'] == pytest.approx(9.0 / math.sqrt(41.0))   # ~1.405, net long


def test_missing_pair_uses_sparse_default():
    # sim has only diagonals -> off-diagonal falls back to 0.05.
    sim = {'s1': {'s1': 1.0}, 's2': {'s2': 1.0}}
    out, _ = corr_adjusted_net_sharpe({'AAA': [('s1', 1), ('s2', 1)]}, sim, {'s1': 2.0, 's2': 2.0})
    assert out['AAA'] == pytest.approx(8.0 / math.sqrt(8.4))    # q = 8 + 2*2*2*0.05 = 8.4


def test_non_psd_backstop_no_nan():
    # 3 co-firing, sims {0.8,0.8,0.05}, dirs (L,S,S): q = -0.10 < eps -> diagonal backstop.
    sim = {'s1': {'s1': 1.0, 's2': 0.8, 's3': 0.8},
           's2': {'s2': 1.0, 's1': 0.8, 's3': 0.05},
           's3': {'s3': 1.0, 's1': 0.8, 's2': 0.05}}
    contribs = {'AAA': [('s1', 1), ('s2', -1), ('s3', -1)]}
    out, nb = corr_adjusted_net_sharpe(contribs, sim, {'s1': 1.0, 's2': 1.0, 's3': 1.0})
    assert nb == 1
    assert math.isfinite(out['AAA'])
    assert out['AAA'] == pytest.approx(-1.0 / math.sqrt(3.0))   # num=-1, den=sqrt(diag=3)


def test_zero_direction_skipped():
    out, nb = corr_adjusted_net_sharpe({'AAA': [('s1', 1), ('s2', 0)]}, _sim(['s1', 's2']),
                                       {'s1': 3.0, 's2': 9.9})
    assert out['AAA'] == pytest.approx(3.0)          # s2 (d=0) ignored
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /root/wt-corr-cumsharpe && python3 -m pytest tests/test_corr_adjusted_net_sharpe.py -q`
Expected: FAIL — `ImportError: cannot import name 'corr_adjusted_net_sharpe'`.

- [ ] **Step 3: Implement the function**

In `src/execution/orthogonalization.py`: change the import block at the top from
```python
from __future__ import annotations
```
to
```python
from __future__ import annotations
import math

SPARSE_DEFAULT = 0.05   # unknown strategy pair similarity (matches strategy_similarity/correlation_matrix)
```
Then append at end of file:
```python
def corr_adjusted_net_sharpe(contribs_by_ticker: dict[str, list[tuple]],
                             sim: dict[str, dict[str, float]],
                             weight_by_strat: dict[str, float],
                             eps: float = 1e-9) -> tuple[dict[str, float], int]:
    """Signed, correlation-adjusted (Sharpe-weighted) combination Sharpe per ticker.

    contribs_by_ticker: {ticker: [(strategy_id, direction_int), ...]} (post-fold survivors).
    sim:   per-regime strategy x strategy similarity matrix {sid: {sid: rho}}.
    weight_by_strat: the w_i basis (cadence-normalized daily_weight).
    Returns ({ticker: signed S_adj}, n_backstop_fires).

    APPROXIMATE (similarity-proxy): `sim` is a heuristic Jaccard-return-corr blend, not a true
    return-correlation matrix and NOT PSD-guaranteed -> the signed quadratic form q can go <= 0.
    The inert non-PSD backstop then falls back to the diagonal ("assume independent") denominator
    and is counted; it is a NaN guard, NOT a deflating floor.

        num = sum_i  w_i^2 * d_i                              (signed: opposing strategies cancel)
        q   = sum_ij w_i * w_j * d_i * d_j * rho_ij           (rho_ii = 1; missing -> SPARSE_DEFAULT)
        S_adj = num / sqrt(q)            if q >  eps          (no floor; full diversification credit)
              = num / sqrt(sum_i w_i^2)  if q <= eps          (backstop; counted)
    """
    out: dict[str, float] = {}
    n_backstop = 0
    for ticker, contribs in contribs_by_ticker.items():
        rows = []
        for sid, d in contribs:
            w = weight_by_strat.get(sid)
            if w is None or not d:
                continue
            rows.append((sid, int(d), float(w)))
        if not rows:
            continue
        num = sum(w * w * d for (_s, d, w) in rows)
        diag = sum(w * w for (_s, _d, w) in rows)
        q = diag                                  # i == j terms (rho_ii = 1)
        n = len(rows)
        for i in range(n):
            sid_i, d_i, w_i = rows[i]
            a_i = w_i * d_i
            row_i = sim.get(sid_i, {})
            for j in range(i + 1, n):
                sid_j, d_j, w_j = rows[j]
                rho = row_i.get(sid_j)
                if rho is None:
                    rho = sim.get(sid_j, {}).get(sid_i, SPARSE_DEFAULT)
                q += 2.0 * a_i * (w_j * d_j) * float(rho)
        if q > eps:
            den = math.sqrt(q)
        else:
            den = math.sqrt(diag) if diag > 0 else 0.0
            n_backstop += 1
        out[ticker] = (num / den) if den > 0 else 0.0
    return out, n_backstop
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /root/wt-corr-cumsharpe && python3 -m pytest tests/test_corr_adjusted_net_sharpe.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/wt-corr-cumsharpe
git add src/execution/orthogonalization.py tests/test_corr_adjusted_net_sharpe.py
git commit -m "feat(sizer): corr_adjusted_net_sharpe pure function (signed, inverse-free, non-PSD backstop)"
```

---

### Task 2: Migration 140 — per-regime `min_corr_cum_sharpe`

**Files:**
- Create: `src/database/migrations/140_per_regime_min_corr_cum_sharpe.sql`

**Interfaces:**
- Produces: column `regime_sizer_params.min_corr_cum_sharpe REAL NOT NULL DEFAULT 1.0`, CHECK `[0.0, 10.0]`, constraint name `regime_sizer_params_min_corr_cum_sharpe_check`.

- [ ] **Step 1: Write the migration**

Create `src/database/migrations/140_per_regime_min_corr_cum_sharpe.sql`:
```sql
-- Per-regime floor for the correlation-adjusted cumulative-Sharpe conviction
-- gate (OPENCLAW_STRATEGY_CORR_CUMSHARPE). Distinct from the legacy
-- min_cumulative_sharpe [1.0,10.0] floor (migration 108): the corr-adjusted
-- quantity is cadence-normalized AND diversification-deflated, so its scale is
-- smaller and is set empirically from the shadow run. Bound [0.0, 10.0] so the
-- operator can fully open the gate in a regime (e.g. CRISIS). Additive column;
-- master-DB invariant honored (no DELETE). Default 1.0 is a PLACEHOLDER — the
-- rollout REQUIRES setting per-regime values from shadow data before the live flip.
ALTER TABLE regime_sizer_params
  ADD COLUMN IF NOT EXISTS min_corr_cum_sharpe REAL NOT NULL DEFAULT 1.0;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'regime_sizer_params_min_corr_cum_sharpe_check'
  ) THEN
    ALTER TABLE regime_sizer_params
      ADD CONSTRAINT regime_sizer_params_min_corr_cum_sharpe_check
      CHECK (min_corr_cum_sharpe >= 0.0 AND min_corr_cum_sharpe <= 10.0);
  END IF;
END $$;
```

- [ ] **Step 2: Verify it parses (dry, no live apply)**

Run: `cd /root/wt-corr-cumsharpe && python3 -c "import pathlib,re; s=pathlib.Path('src/database/migrations/140_per_regime_min_corr_cum_sharpe.sql').read_text(); assert 'ADD COLUMN IF NOT EXISTS min_corr_cum_sharpe' in s and 'CHECK (min_corr_cum_sharpe >= 0.0 AND min_corr_cum_sharpe <= 10.0)' in s; print('ok')"`
Expected: `ok`. (Do NOT apply to the live DB — operator-gated.)

- [ ] **Step 3: Commit**

```bash
cd /root/wt-corr-cumsharpe
git add src/database/migrations/140_per_regime_min_corr_cum_sharpe.sql
git commit -m "feat(db): migration 140 — per-regime min_corr_cum_sharpe [0,10] additive column"
```

---

### Task 3: Resolver `_resolve_min_corr_cum_sharpe`

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (add resolver after `_resolve_min_cumulative_sharpe`, ~line 230)
- Test: `tests/test_resolve_min_corr_cum_sharpe.py` (create)

**Interfaces:**
- Consumes: nothing from prior tasks.
- Produces: `_resolve_min_corr_cum_sharpe(params: dict | None, default: float = 1.0) -> float` — per-regime `params['min_corr_cum_sharpe']` bound `[0.0, 10.0]`; fallback `pipeline_config['min_corr_cum_sharpe']`; then `default`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_resolve_min_corr_cum_sharpe.py`:
```python
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


def test_uses_per_regime_value_in_bounds():
    assert rbs._resolve_min_corr_cum_sharpe({'min_corr_cum_sharpe': 0.7}) == pytest.approx(0.7)


def test_clamps_to_bounds():
    assert rbs._resolve_min_corr_cum_sharpe({'min_corr_cum_sharpe': -5.0}) == pytest.approx(0.0)
    assert rbs._resolve_min_corr_cum_sharpe({'min_corr_cum_sharpe': 99.0}) == pytest.approx(10.0)


def test_missing_param_falls_back_to_default_when_no_db(monkeypatch):
    # No POSTGRES_URI -> DB lookup fails -> default.
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    assert rbs._resolve_min_corr_cum_sharpe({}, default=1.25) == pytest.approx(1.25)
    assert rbs._resolve_min_corr_cum_sharpe(None, default=1.25) == pytest.approx(1.25)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/wt-corr-cumsharpe && python3 -m pytest tests/test_resolve_min_corr_cum_sharpe.py -q`
Expected: FAIL — `AttributeError: module 'execution.regime_blended_sizer' has no attribute '_resolve_min_corr_cum_sharpe'`.

- [ ] **Step 3: Implement the resolver**

In `src/execution/regime_blended_sizer.py`, immediately AFTER the end of `_resolve_min_cumulative_sharpe` (after its final `return`/`except` block, ~line 230), add:
```python
def _resolve_min_corr_cum_sharpe(params: dict | None, default: float = 1.0) -> float:
    """Per-regime floor for the correlation-adjusted cumulative-Sharpe gate
    (OPENCLAW_STRATEGY_CORR_CUMSHARPE). Mirrors _resolve_min_cumulative_sharpe but
    reads regime_sizer_params.min_corr_cum_sharpe, bound [0.0, 10.0] (migration 140),
    falling back to pipeline_config['min_corr_cum_sharpe'], then `default`.

    NOTE the scale differs from the legacy floor: this quantity is cadence-normalized
    AND diversification-deflated, so values are smaller. The operator sets per-regime
    values from the shadow run before flipping the flag; `default` is a placeholder.
    """
    if isinstance(params, dict):
        v = params.get('min_corr_cum_sharpe')
        if v is not None:
            try:
                return max(0.0, min(10.0, float(v)))
            except (TypeError, ValueError):
                pass
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'min_corr_cum_sharpe'")
                row = cur.fetchone()
                if row is not None:
                    return max(0.0, min(10.0, float(row[0])))
    except Exception:
        pass
    return default
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/wt-corr-cumsharpe && python3 -m pytest tests/test_resolve_min_corr_cum_sharpe.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/wt-corr-cumsharpe
git add src/execution/regime_blended_sizer.py tests/test_resolve_min_corr_cum_sharpe.py
git commit -m "feat(sizer): _resolve_min_corr_cum_sharpe per-regime floor resolver [0,10]"
```

---

### Task 4: Sizer live wiring (gate + sizing rebuilt from one S_adj)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (`_sharpe_cadence_path` flags ~913, floor select ~936, gate block ~1061-1072; add helper `_corr_adjusted_maps`)
- Test: `tests/test_corr_cumsharpe_wiring.py` (create)

**Interfaces:**
- Consumes: `orthogonalization.corr_adjusted_net_sharpe` (Task 1), `_resolve_min_corr_cum_sharpe` (Task 3).
- Produces: `_corr_adjusted_maps(ticker_meta: dict, weight_by_strat: dict, eff_weight_by_strat: dict, sim: dict) -> tuple[dict, dict, int, int]` returning `(gate_net_sharpe, sizing_weight, nb_gate, nb_size)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_corr_cumsharpe_wiring.py`:
```python
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


def _meta(strats, dirs):
    return {'AAA': {'strategies': list(strats), 'directions': list(dirs),
                    'brackets': []}}


def test_flag_default_off():
    import os
    os.environ.pop('OPENCLAW_STRATEGY_CORR_CUMSHARPE', None)
    assert rbs._ortho_enabled('OPENCLAW_STRATEGY_CORR_CUMSHARPE') is False


def test_gate_uses_raw_sizing_uses_scaled():
    # daily_weight (raw) vs eff_weight (size_scalar folded in) must differ on sizing,
    # while the gate stays on raw. Single strategy: S_adj = w * d.
    meta = _meta(['s1'], [1])
    sim = {'s1': {'s1': 1.0}}
    raw = {'s1': 2.0}
    scaled = {'s1': 6.0}                       # size_scalar = 3x
    gate, size, nb_g, nb_s = rbs._corr_adjusted_maps(meta, raw, scaled, sim)
    assert gate['AAA'] == pytest.approx(2.0)   # raw basis
    assert size['AAA'] == pytest.approx(6.0)   # scaled basis
    assert nb_g == 0 and nb_s == 0


def test_identical_when_scalars_one():
    meta = _meta(['s1', 's2'], [1, 1])
    sim = {'s1': {'s1': 1.0, 's2': 0.0}, 's2': {'s2': 1.0, 's1': 0.0}}
    w = {'s1': 3.0, 's2': 4.0}
    gate, size, _, _ = rbs._corr_adjusted_maps(meta, w, w, sim)   # eff == raw
    assert gate['AAA'] == pytest.approx(size['AAA'])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/wt-corr-cumsharpe && python3 -m pytest tests/test_corr_cumsharpe_wiring.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_corr_adjusted_maps'`.

- [ ] **Step 3: Add the helper**

In `src/execution/regime_blended_sizer.py`, add (module scope, near the other private helpers, e.g. just after `_resolve_min_corr_cum_sharpe`):
```python
def _corr_adjusted_maps(ticker_meta, weight_by_strat, eff_weight_by_strat, sim):
    """Build the correlation-adjusted gate + sizing maps from per-ticker contributors.
    Gate uses RAW daily_weight (size_scalar-exempt — matches the legacy 'gate stays raw'
    intent); sizing uses eff_weight (size_scalar folded in). Identical when all scalars=1.
    Returns (gate_net_sharpe, sizing_weight, nb_gate, nb_size)."""
    from execution import orthogonalization as _og
    contribs_by_ticker = {tkr: list(zip(m['strategies'], m['directions']))
                          for tkr, m in ticker_meta.items()}
    gate_net_sharpe, nb_gate = _og.corr_adjusted_net_sharpe(contribs_by_ticker, sim, weight_by_strat)
    sizing_weight, nb_size = _og.corr_adjusted_net_sharpe(contribs_by_ticker, sim, eff_weight_by_strat)
    return gate_net_sharpe, sizing_weight, nb_gate, nb_size
```

- [ ] **Step 4: Wire the flags + floor selection + gate block**

(a) In `_sharpe_cadence_path`, where `_size_scalar_on` / `_cadence_stop_norm_on` are set (~line 913), add two flags:
```python
    _corr_cumsharpe_on = _ortho_enabled('OPENCLAW_STRATEGY_CORR_CUMSHARPE')
    _corr_cumsharpe_shadow = _ortho_enabled('OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW')
```

(b) Replace the floor line (~936) `min_cum_sharpe = _resolve_min_cumulative_sharpe(params)` with:
```python
    min_cum_sharpe = (_resolve_min_corr_cum_sharpe(params) if _corr_cumsharpe_on
                      else _resolve_min_cumulative_sharpe(params))
```

(c) Replace the existing gate block (currently lines ~1061-1072, the `gate_net_sharpe = ticker_net_sharpe` + `if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT'):` … `deflated_net_sharpe` … block) with the precedence version:
```python
    # Conviction adjustment (gate quantity). Precedence:
    #   CORR_CUMSHARPE (closed-form Sharpe-weighted combination Sharpe) supersedes
    #   CORR_WEIGHT (legacy within-block heuristic deflation).
    gate_net_sharpe = ticker_net_sharpe
    if _ortho_groups and _corr_cumsharpe_on:
        _sim = _ortho_groups.get('matrix') or {}
        gate_net_sharpe, _size_adj, _nb_g, _nb_s = _corr_adjusted_maps(
            ticker_meta, weight_by_strat, eff_weight_by_strat, _sim)
        # One quantity drives BOTH gate and sizing: rebuild ticker_w from S_adj.
        ticker_w = defaultdict(float, _size_adj)
        if _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT'):
            logger.info('corr_cumsharpe: superseding deflated_net_sharpe gate (CORR_WEIGHT bypassed)')
        logger.info('corr_cumsharpe: gate+sizing rebuilt from S_adj for %d tickers '
                    '(non-PSD backstop fires gate=%d size=%d)',
                    len(gate_net_sharpe), _nb_g, _nb_s)
    elif _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT'):
        from execution import orthogonalization as _og
        contribs_by_ticker = {
            tkr: list(zip(meta['strategies'], meta['directions']))
            for tkr, meta in ticker_meta.items()
        }
        gate_net_sharpe = _og.deflated_net_sharpe(
            contribs_by_ticker, _ortho_groups['block_map'],
            _ortho_groups['matrix'], sharpe_by_strat)
        logger.info('orthogonalization.corr_weight: deflated gate for %d tickers', len(gate_net_sharpe))
```

(Note: `defaultdict` and `logger` are already imported in this function/module. The per-ticker cap at ~line 1183 consumes `gate_net_sharpe` unchanged — it automatically uses the new S_adj scale; no edit. `target_usd ∝ ticker_w` at ~1131 now reads the rebuilt `ticker_w`.)

- [ ] **Step 5: Run the new test + the full sizer regression suite**

Run:
```bash
cd /root/wt-corr-cumsharpe
python3 -m pytest tests/test_corr_cumsharpe_wiring.py -q
python3 -m pytest tests/test_orthogonalization.py tests/test_orthogonalization_sizer.py \
  tests/test_regime_blended_sizer.py tests/test_regime_blended_sizer_live.py \
  tests/test_sizer_per_ticker_cap.py tests/test_sizer_dust_floor.py -q
```
Expected: new test PASS (3 passed); regression suite PASS (flag OFF ⇒ byte-identical). If any regression fails, the OFF path was altered — fix before commit.

- [ ] **Step 6: Commit**

```bash
cd /root/wt-corr-cumsharpe
git add src/execution/regime_blended_sizer.py tests/test_corr_cumsharpe_wiring.py
git commit -m "feat(sizer): wire corr-adjusted S_adj into gate+sizing behind OPENCLAW_STRATEGY_CORR_CUMSHARPE"
```

---

### Task 5: Shadow observability

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (add `_corr_cumsharpe_shadow_metrics` + shadow call before the gate-drop block)
- Test: `tests/test_corr_cumsharpe_shadow.py` (create)

**Interfaces:**
- Consumes: `_corr_adjusted_maps` (Task 4).
- Produces: `_corr_cumsharpe_shadow_metrics(gate_adj: dict, size_adj: dict, legacy_gate: dict, live_ticker_w: dict, legacy_floor: float, lam: float, nav: float, nb_g: int, nb_s: int) -> dict` — distribution / would-keep / live-keep / rec_floor / backstop fires / top Δ$ moves.

- [ ] **Step 1: Write the failing test**

Create `tests/test_corr_cumsharpe_shadow.py`:
```python
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


def test_metrics_shape_and_rec_floor():
    gate_adj = {'A': 4.0, 'B': 2.0, 'C': 0.5}
    size_adj = {'A': 4.0, 'B': 2.0, 'C': 0.5}
    legacy_gate = {'A': 9.0, 'B': 5.0, 'C': 1.0}    # legacy floor 3.0 keeps A,B (2 names)
    live_w = {'A': 3.0, 'B': 1.0, 'C': 0.2}
    m = rbs._corr_cumsharpe_shadow_metrics(gate_adj, size_adj, legacy_gate, live_w,
                                           legacy_floor=3.0, lam=2.0, nav=100000.0, nb_g=0, nb_s=1)
    assert m['live_keep'] == 2
    # rec_floor keeps ~2 names under |S_adj| -> the 2nd largest = 2.0
    assert m['rec_floor'] == pytest.approx(2.0)
    assert m['backstop_fires'] == {'gate': 0, 'size': 1}
    assert set(m['dist'].keys()) == {'min', 'p25', 'median', 'p75', 'max'}
    assert m['dist']['max'] == pytest.approx(4.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/wt-corr-cumsharpe && python3 -m pytest tests/test_corr_cumsharpe_shadow.py -q`
Expected: FAIL — `AttributeError: ... has no attribute '_corr_cumsharpe_shadow_metrics'`.

- [ ] **Step 3: Implement metrics + shadow call**

(a) Add the pure metrics helper (module scope, near `_corr_adjusted_maps`):
```python
def _corr_cumsharpe_shadow_metrics(gate_adj, size_adj, legacy_gate, live_ticker_w,
                                   legacy_floor, lam, nav, nb_g, nb_s):
    """Pure shadow metrics for the corr-adjusted gate vs the live gate. Routes nothing."""
    mags = sorted(abs(v) for v in gate_adj.values())
    def _pct(p):
        if not mags:
            return 0.0
        i = max(0, min(len(mags) - 1, int(round(p * (len(mags) - 1)))))
        return mags[i]
    dist = {'min': (mags[0] if mags else 0.0), 'p25': _pct(0.25), 'median': _pct(0.5),
            'p75': _pct(0.75), 'max': (mags[-1] if mags else 0.0)}
    live_keep = {t for t in gate_adj if abs(legacy_gate.get(t, 0.0)) >= legacy_floor}
    desc = sorted((abs(v) for v in gate_adj.values()), reverse=True)
    k = min(len(desc), len(live_keep))
    rec_floor = desc[k - 1] if k > 0 else 0.0
    def _alloc(m):
        g = sum(abs(v) for v in m.values())
        return {t: (v / g * lam * nav) for t, v in m.items()} if g > 0 else {}
    new_alloc, live_alloc = _alloc(size_adj), _alloc(live_ticker_w)
    moved = {t: round(new_alloc.get(t, 0.0) - live_alloc.get(t, 0.0))
             for t in set(new_alloc) | set(live_alloc)}
    top_moves = dict(sorted(((t, d) for t, d in moved.items() if abs(d) >= 1000),
                            key=lambda kv: -abs(kv[1]))[:10])
    return {'dist': dist, 'live_keep': len(live_keep),
            'would_keep': sum(1 for v in gate_adj.values() if abs(v) >= rec_floor),
            'rec_floor': rec_floor, 'backstop_fires': {'gate': nb_g, 'size': nb_s},
            'top_dollar_moves': top_moves}
```

(b) Insert the shadow call in `_sharpe_cadence_path` immediately BEFORE the gate-drop block (just before `if min_cum_sharpe > 0:` at ~line 1078), so `ticker_w`/`ticker_meta`/`gate_net_sharpe` still hold the live (legacy) values:
```python
    if _ortho_groups and _corr_cumsharpe_shadow and not _corr_cumsharpe_on:
        try:
            _sim = _ortho_groups.get('matrix') or {}
            _g_adj, _s_adj, _nbg, _nbs = _corr_adjusted_maps(
                ticker_meta, weight_by_strat, eff_weight_by_strat, _sim)
            _m = _corr_cumsharpe_shadow_metrics(
                _g_adj, _s_adj, gate_net_sharpe, dict(ticker_w),
                min_cum_sharpe, lam, nav, _nbg, _nbs)
            logger.info('corr_cumsharpe.shadow: dist=%s live_keep=%d would_keep=%d '
                        'rec_floor=%.4f backstop=%s top_dollar_moves=%s',
                        {k: round(v, 4) for k, v in _m['dist'].items()},
                        _m['live_keep'], _m['would_keep'], _m['rec_floor'],
                        _m['backstop_fires'], _m['top_dollar_moves'])
        except Exception as e:
            logger.warning('corr_cumsharpe.shadow failed (%s)', e)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/wt-corr-cumsharpe && python3 -m pytest tests/test_corr_cumsharpe_shadow.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
cd /root/wt-corr-cumsharpe
git add src/execution/regime_blended_sizer.py tests/test_corr_cumsharpe_shadow.py
git commit -m "feat(sizer): corr_cumsharpe shadow metrics (dist/would-drop/Δ\$/backstop/rec-floor)"
```

---

### Task 6: Dashboard — per-regime `min_corr_cum_sharpe` control + active-gate badge

**Files:**
- Modify: `src/channels/api/server.js` (GET ~550/590, PUT ~630, UI ~4102 + ~7905-7999)

**Interfaces:**
- Consumes: migration 140 column `min_corr_cum_sharpe`.
- Produces: GET `/api/config/regime-sizing` returns `regimes[].min_corr_cum_sharpe` + top-level `active_conviction_gate`; PUT accepts `min_corr_cum_sharpe ∈ [0.0,10.0]`.

- [ ] **Step 1: GET — add column to SELECT + response + active-gate flag**

In the `regimeRes` query (~line 550-551), add the column:
```js
      dbQuery(`SELECT regime_state, liquidity_param, position_circuit_breaker_pct,
                      min_cumulative_sharpe, min_corr_cum_sharpe, updated_at
               FROM regime_sizer_params
```
In the response `regimes.map` object (~line 590-599), add after `min_cumulative_sharpe`:
```js
        // Per-regime floor for the correlation-adjusted gate (migration 140),
        // bound [0.0, 10.0]. Active only when OPENCLAW_STRATEGY_CORR_CUMSHARPE=1.
        min_corr_cum_sharpe:            r.min_corr_cum_sharpe != null ? parseFloat(r.min_corr_cum_sharpe) : 1.0,
```
In the top-level `res.json({...})` (after `current_regime`), add:
```js
      // Which conviction gate is live: the corr-adjusted floor (min_corr_cum_sharpe)
      // when the sizer flag is on, else the legacy min_cumulative_sharpe.
      active_conviction_gate: (['1','true','on'].includes(String(process.env.OPENCLAW_STRATEGY_CORR_CUMSHARPE || '').trim().toLowerCase()) ? 'corr_cumsharpe' : 'legacy'),
```

- [ ] **Step 2: PUT — accept + validate `min_corr_cum_sharpe`**

In `PUT /api/config/regime-sizing/:regime`, after the `min_cumulative_sharpe` block (~line 638), add:
```js
  if (body.min_corr_cum_sharpe !== undefined) {
    const v = parseFloat(body.min_corr_cum_sharpe);
    // Range matches regime_sizer_params_min_corr_cum_sharpe_check (migration 140).
    if (!isFinite(v) || v < 0.0 || v > 10.0) {
      return res.status(400).json({ error: 'min_corr_cum_sharpe must be a number in [0.0, 10.0]' });
    }
    updates.min_corr_cum_sharpe = v;
  }
```

- [ ] **Step 3: UI — second per-regime slider + active-gate badge**

In the conviction-gate card renderer (~line 7916, inside `host.innerHTML = regimes.map(r => {…})`), after the existing `min_cumulative_sharpe` slider markup (the block ending `…id="st-sg-status-${r.state}"></div>`), add a second slider for the corr-adjusted floor and an active-gate badge. Replace the `return \`<div …>\`` body with:
```js
    const v = (r.min_cumulative_sharpe != null && isFinite(r.min_cumulative_sharpe))
      ? Number(r.min_cumulative_sharpe) : 3.0;
    const vc = (r.min_corr_cum_sharpe != null && isFinite(r.min_corr_cum_sharpe))
      ? Number(r.min_corr_cum_sharpe) : 1.0;
    const activeGate = (cfg.active_conviction_gate === 'corr_cumsharpe') ? 'corr' : 'legacy';
    const tag = r.state === current ? '<span class="st-sharpe-card-tag">CURRENT</span>' : '';
    const klass = 'st-sharpe-card' + (r.state === current ? ' current-regime' : '');
    const liveBadge = g => (activeGate === g)
      ? ' <span class="st-sharpe-card-tag" style="background:#2563eb">LIVE GATE</span>' : '';
    return \`<div class="\${klass}" data-regime="\${r.state}">
      <div class="st-sharpe-card-head">
        <span class="st-sharpe-card-regime">\${r.state}</span>
        \${tag}
      </div>
      <div style="font-size:11px;opacity:.7;margin-top:4px">legacy cum-sharpe\${liveBadge('legacy')}</div>
      <div class="st-sharpe-card-value" id="st-sg-val-\${r.state}">\${v.toFixed(2)}</div>
      <input type="range" class="st-sharpe-card-slider" id="st-sg-slider-\${r.state}"
             min="1" max="10" step="0.1" value="\${v}" data-regime="\${r.state}" />
      <div class="st-sharpe-card-range"><span>1.0</span><span>10.0</span></div>
      <div class="st-sharpe-card-status" id="st-sg-status-\${r.state}"></div>
      <div style="font-size:11px;opacity:.7;margin-top:8px">corr-adjusted\${liveBadge('corr')}</div>
      <div class="st-sharpe-card-value" id="st-cg-val-\${r.state}">\${vc.toFixed(2)}</div>
      <input type="range" class="st-sharpe-card-slider" id="st-cg-slider-\${r.state}"
             min="0" max="10" step="0.05" value="\${vc}" data-regime="\${r.state}" />
      <div class="st-sharpe-card-range"><span>0.0</span><span>10.0</span></div>
      <div class="st-sharpe-card-status" id="st-cg-status-\${r.state}"></div>
    </div>\`;
```
Then in the slider-wiring loop (~line 7936, `for (const r of regimes) {…}`), after the existing legacy slider wiring, add the corr-adjusted slider wiring:
```js
    const cslider = document.getElementById('st-cg-slider-' + r.state);
    const cvalEl  = document.getElementById('st-cg-val-' + r.state);
    const cstatEl = document.getElementById('st-cg-status-' + r.state);
    if (cslider && cvalEl) {
      cslider.addEventListener('input', () => {
        cvalEl.textContent = parseFloat(cslider.value).toFixed(2);
      });
      cslider.addEventListener('change', () => {
        if (_sharpeGateDebounce['corr_' + r.state]) clearTimeout(_sharpeGateDebounce['corr_' + r.state]);
        _sharpeGateDebounce['corr_' + r.state] = setTimeout(
          () => _corrSharpeGatePut(r.state, parseFloat(cslider.value), cstatEl), 300);
      });
    }
```
And add a `_corrSharpeGatePut` function next to `_sharpeGatePut` (~line 7979):
```js
async function _corrSharpeGatePut(regime, value, statEl) {
  if (_sharpeGateInflight['corr_' + regime]) return;
  _sharpeGateInflight['corr_' + regime] = true;
  if (statEl) statEl.textContent = 'saving…';
  try {
    const resp = await fetch('/api/config/regime-sizing/' + encodeURIComponent(regime), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ min_corr_cum_sharpe: value }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (statEl) statEl.textContent = '✗ ' + (err.error || resp.statusText);
    } else if (statEl) {
      statEl.textContent = '✓ saved · next cycle picks up ' + value.toFixed(2);
      setTimeout(() => { if (statEl) statEl.textContent = ''; }, 4000);
    }
  } catch (e) {
    if (statEl) statEl.textContent = '✗ ' + (e.message || 'network error');
  } finally {
    _sharpeGateInflight['corr_' + regime] = false;
  }
}
```

- [ ] **Step 4: Syntax-check the server bundle**

Run: `cd /root/wt-corr-cumsharpe && node --check src/channels/api/server.js`
Expected: no output (exit 0). If it errors, fix the edited region.

- [ ] **Step 5: Commit**

```bash
cd /root/wt-corr-cumsharpe
git add src/channels/api/server.js
git commit -m "feat(dashboard): per-regime min_corr_cum_sharpe slider + active-gate badge"
```

---

## Post-implementation (operator-gated — NOT in this plan's execution)

1. Apply migration 140 to the live DB (operator).
2. Deploy in **shadow**: set `OPENCLAW_STRATEGY_CORR_CUMSHARPE_SHADOW=1` (leave `CORR_CUMSHARPE` OFF), restart johnbot, watch `corr_cumsharpe.shadow:` logs for N cycles.
3. Set per-regime `min_corr_cum_sharpe` from the shadow `rec_floor` (dashboard sliders).
4. **Flip**: `OPENCLAW_STRATEGY_CORR_CUMSHARPE=1`, retire the shadow flag, restart johnbot, verify dashboard "LIVE GATE" badge moved + monitor 1 cycle.
5. Rollback: unset `OPENCLAW_STRATEGY_CORR_CUMSHARPE` → instant revert.

## Self-Review

- **Spec coverage:** §2 quantity → Task 1; §2.1 backstop → Task 1 (`test_non_psd_backstop_no_nan`); §3 pure fn → Task 1; §4 wiring/flag/supersede/size_scalar → Task 4; §5 floor+migration+resolver+dashboard → Tasks 2,3,6; §6 shadow → Task 5; §7 tests → every task; §8 rollback → Global Constraints + post-impl. All covered.
- **Placeholder scan:** none — every step has concrete code/commands.
- **Type consistency:** `corr_adjusted_net_sharpe(...) -> (dict, int)` consumed identically in Task 4/5 helpers; `_corr_adjusted_maps -> (gate, size, nb_g, nb_s)` matches its call sites; `_resolve_min_corr_cum_sharpe(params, default=1.0)` matches Task 4 floor-select call.
