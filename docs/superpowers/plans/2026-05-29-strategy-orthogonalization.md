# Strategy Orthogonalization Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop highly-correlated strategies from manufacturing false confidence in the regime-blended sizer, by folding near-identical strategies to one representative (Tier 1) and discounting partially-correlated strategy *sets* at the conviction gate via an effective-independent-bets factor (Tier 2).

**Architecture:** An OFFLINE weekly job builds a per-regime strategy×strategy similarity matrix (holdings co-firing Jaccard, blended with return-correlation under data-adaptive shrinkage), hierarchically clusters it at two cuts (fold-groups @0.85, factor-blocks @0.40), and persists groups + matrix. A LIVE per-cycle path inside `_sharpe_cadence_path` consumes those groups: Tier-1 folds duplicate contributions to the max-Sharpe representative *before* the aggregation sums; Tier-2 deflates within-block conviction at the gate only (sizing untouched). All transforms are pure functions in a new `src/execution/orthogonalization.py`; the sizer gains thin gated call-sites. Everything is default-OFF and byte-identical when OFF.

**Tech Stack:** Python 3, psycopg2, numpy, scipy.cluster.hierarchy (all present on VPS), PostgreSQL, pytest. Node `weekly_live_sharpe.js` for cron wiring. Spec: `docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md`.

**Conventions for the implementer:**
- Run tests from repo root with `PYTHONPATH=src python3 -m pytest <path> -v`.
- Test files mirror `tests/test_correlation_matrix.py`: insert `ROOT` and `ROOT/src` on `sys.path`, import the module under test, exercise pure functions with in-memory dicts. No live DB in unit tests.
- `conftest.py` auto-restores `os.environ` after each test, so gate-flipping tests are safe.
- Commit after every green task. You are on branch `feat/strategy-orthogonalization` (already created).

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `src/database/migrations/123_strategy_orthogonalization.sql` | 5 new operational tables | Create |
| `src/execution/strategy_returns.py` | Reconstruct + difference per-strategy daily return series; persist `strategy_daily_returns` | Create |
| `src/execution/strategy_similarity.py` | Strategy×strategy similarity (overlap+return-corr), per-regime + state-prob blend, clustering, persistence, CLI, live loader | Create |
| `src/execution/orthogonalization.py` | Pure live transforms: fold contributions; k_eff; block conviction; deflated net-Sharpe | Create |
| `src/execution/regime_blended_sizer.py` | Gated Tier-1 + Tier-2 call-sites; shadow logging | Modify (`309–433`) |
| `src/agent/curators/weekly_live_sharpe.js` | Invoke similarity rebuild after weights rebuild | Modify (`~49`) |
| `src/execution/fold_report.py` | Chronic-fold audit + `#strategy-memos` Discord post | Create |
| `tests/test_strategy_returns.py`, `tests/test_strategy_similarity.py`, `tests/test_orthogonalization.py`, `tests/test_orthogonalization_sizer.py` | Unit + regression tests | Create |

---

## Task 1: Migration 123 — operational tables

**Files:**
- Create: `src/database/migrations/123_strategy_orthogonalization.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 123_strategy_orthogonalization.sql
-- Operational tables for the strategy orthogonalization engine (NOT master data —
-- versioned current/historical rows like strategy_weights_by_regime).
-- Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md

-- Per-strategy daily return series (reconstructed: differenced live marks + backtest).
CREATE TABLE IF NOT EXISTS strategy_daily_returns (
  id               BIGSERIAL PRIMARY KEY,
  strategy_id      TEXT NOT NULL,
  ret_date         DATE NOT NULL,
  daily_return_pct NUMERIC NOT NULL,   -- differenced daily Δ (NOT cumulative level)
  regime_state     TEXT,
  source           TEXT NOT NULL,      -- 'live' | 'backtest'
  computed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (strategy_id, ret_date, source)
);
CREATE INDEX IF NOT EXISTS sdr_strategy_date_idx
  ON strategy_daily_returns (strategy_id, ret_date);

-- Per-regime strategy×strategy similarity matrix (JSONB blob; one current row per regime).
CREATE TABLE IF NOT EXISTS strategy_similarity_matrix (
  id            BIGSERIAL PRIMARY KEY,
  regime_state  TEXT NOT NULL,
  matrix        JSONB NOT NULL,        -- {strategy_id: {strategy_id: rho}}
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger       TEXT NOT NULL,
  is_current    BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS ssm_regime_current_idx
  ON strategy_similarity_matrix (regime_state) WHERE is_current;

-- Tight cut → fold-groups (near-identical). Representative = max effective_sharpe member.
CREATE TABLE IF NOT EXISTS strategy_fold_groups (
  id                BIGSERIAL PRIMARY KEY,
  regime_state      TEXT NOT NULL,
  group_id          INTEGER NOT NULL,
  strategy_id       TEXT NOT NULL,
  is_representative BOOLEAN NOT NULL DEFAULT FALSE,
  effective_sharpe  NUMERIC,
  computed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger           TEXT NOT NULL,
  is_current        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS sfg_regime_current_idx
  ON strategy_fold_groups (regime_state) WHERE is_current;

-- Loose cut → factor-blocks (same-factor family).
CREATE TABLE IF NOT EXISTS strategy_factor_blocks (
  id            BIGSERIAL PRIMARY KEY,
  regime_state  TEXT NOT NULL,
  block_id      INTEGER NOT NULL,
  strategy_id   TEXT NOT NULL,
  computed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  trigger       TEXT NOT NULL,
  is_current    BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS sfb_regime_current_idx
  ON strategy_factor_blocks (regime_state) WHERE is_current;

-- Append-only audit of fold-group persistence (feeds the chronic-fold report + future Tier-3).
CREATE TABLE IF NOT EXISTS strategy_fold_audit (
  id              BIGSERIAL PRIMARY KEY,
  run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  regime_state    TEXT NOT NULL,
  group_id        INTEGER NOT NULL,
  strategy_ids    TEXT[] NOT NULL,
  representative  TEXT,
  member_sharpes  JSONB
);
```

- [ ] **Step 2: Apply the migration to the live DB**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -c "import psycopg2,os; from dotenv import load_dotenv; load_dotenv(); c=psycopg2.connect(os.environ['DATABASE_URL']); cur=c.cursor(); cur.execute(open('src/database/migrations/123_strategy_orthogonalization.sql').read()); c.commit(); print('migration 123 applied')"`
Expected: `migration 123 applied`

- [ ] **Step 3: Verify the tables exist**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -c "import psycopg2,os; from dotenv import load_dotenv; load_dotenv(); c=psycopg2.connect(os.environ['DATABASE_URL']); cur=c.cursor(); cur.execute(\"SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'strategy_%' AND table_name IN ('strategy_daily_returns','strategy_similarity_matrix','strategy_fold_groups','strategy_factor_blocks','strategy_fold_audit') ORDER BY 1\"); print([r[0] for r in cur.fetchall()])"`
Expected: all 5 table names printed.

- [ ] **Step 4: Commit**

```bash
git add src/database/migrations/123_strategy_orthogonalization.sql
git commit -m "feat(ortho): migration 123 — orthogonalization operational tables"
```

---

## Task 2: Daily-return differencing (pure)

The single most important correctness detail: `signal_pnl.unrealized_pnl_pct` is a cumulative-since-entry **level**; we difference it to a daily Δ.

**Files:**
- Create: `src/execution/strategy_returns.py`
- Test: `tests/test_strategy_returns.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for strategy_returns — differencing cumulative marks to daily returns."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import strategy_returns as sr  # noqa: E402


def test_difference_marks_first_day_is_level_from_zero():
    # marks: list of (date_str, cumulative_unrealized_pct, realized_or_none)
    marks = [('2026-05-01', 0.02, None), ('2026-05-02', 0.05, None), ('2026-05-03', 0.04, None)]
    out = sr.difference_signal_marks(marks)
    assert out == {'2026-05-01': 0.02, '2026-05-02': 0.03, '2026-05-03': -0.01}


def test_difference_marks_close_uses_realized_minus_last_unrealized():
    marks = [('2026-05-01', 0.02, None), ('2026-05-02', 0.05, 0.06)]  # closed on day 2
    out = sr.difference_signal_marks(marks)
    # day1 Δ = 0.02; close-day Δ = realized(0.06) - last_unrealized(0.02) = 0.04
    assert out['2026-05-01'] == 0.02
    assert abs(out['2026-05-02'] - 0.04) < 1e-9


def test_aggregate_equal_weight_across_open_signals():
    # two signals' per-date daily Δ → strategy daily return = mean across signals present that day
    per_signal = {
        'sigA': {'2026-05-01': 0.02, '2026-05-02': 0.04},
        'sigB': {'2026-05-02': -0.02},
    }
    out = sr.aggregate_strategy_daily(per_signal)
    assert out['2026-05-01'] == 0.02            # only A present
    assert abs(out['2026-05-02'] - 0.01) < 1e-9  # mean(0.04, -0.02)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_returns.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.strategy_returns'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Reconstruct per-strategy daily return series.

signal_pnl.unrealized_pnl_pct is a CUMULATIVE-since-entry level; we difference
consecutive marks per signal to a daily delta, then aggregate equal-weight across
the strategy's open signals. Backtest series come from strategy_backtest_trades via
unified_backtest._portfolio_daily_returns. Persisted to strategy_daily_returns.
"""
from __future__ import annotations

import os
from typing import Optional


def difference_signal_marks(marks: list[tuple]) -> dict[str, float]:
    """marks: ordered list of (date_str, cumulative_unrealized_pct, realized_or_none).

    Returns {date_str: daily_delta}. First day = level from 0. A day with a non-None
    realized value is the close day: delta = realized - prior cumulative.
    """
    out: dict[str, float] = {}
    prev = 0.0
    for date_str, cum, realized in marks:
        cum = float(cum) if cum is not None else prev
        if realized is not None:
            out[date_str] = float(realized) - prev
            prev = float(realized)
        else:
            out[date_str] = cum - prev
            prev = cum
    return out


def aggregate_strategy_daily(per_signal: dict[str, dict[str, float]]) -> dict[str, float]:
    """Equal-weight mean of per-signal daily deltas across signals present each date."""
    by_date: dict[str, list[float]] = {}
    for _sig, series in per_signal.items():
        for d, v in series.items():
            by_date.setdefault(d, []).append(v)
    return {d: sum(vs) / len(vs) for d, vs in by_date.items() if vs}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_returns.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/strategy_returns.py tests/test_strategy_returns.py
git commit -m "feat(ortho): per-strategy daily-return differencing (pure)"
```

---

## Task 3: `strategy_daily_returns` builder + persistence

**Files:**
- Modify: `src/execution/strategy_returns.py`

- [ ] **Step 1: Add the DB loaders + builder (no new test — exercised by the live smoke in Task 18; pure logic already covered by Task 2)**

Append to `src/execution/strategy_returns.py`:

```python
def _db():
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()
    return psycopg2.connect(os.environ.get('DATABASE_URL')
                            or os.environ.get('POSTGRES_URI')
                            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _live_marks_by_strategy(window_days: int) -> dict[str, dict[str, dict[str, float]]]:
    """{strategy_id: {signal_id: {date: daily_delta}}} from differenced signal_pnl marks."""
    sql = """
        SELECT es.strategy_id, sp.signal_id::text, sp.pnl_date::text,
               sp.unrealized_pnl_pct::float, sp.realized_pnl_pct::float,
               es.regime_state
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE sp.pnl_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
         ORDER BY es.strategy_id, sp.signal_id, sp.pnl_date
    """
    raw: dict[str, dict[str, list]] = {}
    regime_of: dict[str, dict[str, str]] = {}
    with _db() as conn, conn.cursor() as cur:
        cur.execute(sql, (window_days,))
        for sid, sig, d, unreal, real, regime in cur.fetchall():
            raw.setdefault(sid, {}).setdefault(sig, []).append((d, unreal, real))
            regime_of.setdefault(sid, {})[d] = regime
    out: dict[str, dict[str, dict[str, float]]] = {}
    for sid, sigs in raw.items():
        out[sid] = {sig: difference_signal_marks(marks) for sig, marks in sigs.items()}
    return out, regime_of


def rebuild_daily_returns(window_days: int = 180, trigger: str = 'manual') -> int:
    """Reconstruct live (differenced) per-strategy daily returns and upsert. Returns row count."""
    live, regime_of = _live_marks_by_strategy(window_days)
    rows = []
    for sid, per_signal in live.items():
        agg = aggregate_strategy_daily(per_signal)
        for d, ret in agg.items():
            rows.append((sid, d, ret, regime_of.get(sid, {}).get(d), 'live'))
    if not rows:
        return 0
    with _db() as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO strategy_daily_returns
                 (strategy_id, ret_date, daily_return_pct, regime_state, source)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (strategy_id, ret_date, source) DO UPDATE
                 SET daily_return_pct = EXCLUDED.daily_return_pct,
                     regime_state = EXCLUDED.regime_state,
                     computed_at = NOW()""",
            rows)
        conn.commit()
    return len(rows)
```

- [ ] **Step 2: Smoke it against the live DB**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -c "from execution import strategy_returns as sr; print('rows:', sr.rebuild_daily_returns())"`
Expected: prints `rows: <N>` (N may be small/0 given ~3 weeks of live data — that is acceptable and expected; the engine is data-thin early by design).

- [ ] **Step 3: Commit**

```bash
git add src/execution/strategy_returns.py
git commit -m "feat(ortho): strategy_daily_returns builder (differenced live marks)"
```

---

## Task 4: Jaccard + co-firing emission sets (pure)

**Files:**
- Create: `src/execution/strategy_similarity.py`
- Test: `tests/test_strategy_similarity.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for strategy_similarity — co-firing Jaccard, return-corr blend, clustering."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import strategy_similarity as ss  # noqa: E402


def test_jaccard_identical_sets():
    assert ss.jaccard({1, 2, 3}, {1, 2, 3}) == 1.0


def test_jaccard_disjoint_sets():
    assert ss.jaccard({1, 2}, {3, 4}) == 0.0


def test_jaccard_half_overlap():
    assert ss.jaccard({1, 2}, {2, 3}) == 1.0 / 3.0  # |∩|=1, |∪|=3


def test_jaccard_empty_is_zero():
    assert ss.jaccard(set(), {1}) == 0.0


def test_overlap_similarity_matrix_diagonal_and_symmetry():
    sets = {
        'S1': {('2026W18', 'AAPL', 1), ('2026W18', 'MSFT', 1)},
        'S2': {('2026W18', 'AAPL', 1)},
    }
    m = ss.overlap_similarity(sets)
    assert m['S1']['S1'] == 1.0 and m['S2']['S2'] == 1.0
    assert m['S1']['S2'] == m['S2']['S1']
    assert abs(m['S1']['S2'] - 0.5) < 1e-9  # |∩|=1, |∪|=2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_similarity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.strategy_similarity'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Per-regime strategy×strategy similarity + clustering for orthogonalization.

The transpose of correlation_matrix.py (which is ticker-keyed). Lead signal =
holdings co-firing Jaccard over (ISO-week, ticker, direction) emissions; blended
with return-correlation under a data-adaptive weight that rises from 0 as joint
history accrues. Reuses correlation_matrix's clip/sparse conventions.

Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations

import os
from typing import Optional

REGIME_STATES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_WINDOW_DAYS = 90
FOLD_THRESHOLD  = float(os.environ.get('OPENCLAW_FOLD_THRESHOLD', '0.85'))
BLOCK_THRESHOLD = float(os.environ.get('OPENCLAW_BLOCK_THRESHOLD', '0.40'))
RETURN_CORR_ALPHA_CEIL = 0.6     # max weight return-corr ever takes in the blend
ALPHA_FULL_OBS = 60              # overlapping observations at which alpha reaches the ceiling
MAX_OFF_DIAGONAL = 0.95
SPARSE_DEFAULT = 0.05


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0


def overlap_similarity(sets_by_strat: dict[str, set]) -> dict[str, dict[str, float]]:
    """Pairwise Jaccard over co-firing emission sets. Diagonal 1.0; symmetric."""
    strats = sorted(sets_by_strat.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            j = jaccard(sets_by_strat[a], sets_by_strat[b])
            out[a][b] = j
            out[b][a] = j
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_similarity.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/strategy_similarity.py tests/test_strategy_similarity.py
git commit -m "feat(ortho): co-firing Jaccard similarity (pure)"
```

---

## Task 5: Return correlation + data-adaptive blend (pure)

**Files:**
- Modify: `src/execution/strategy_similarity.py`
- Modify: `tests/test_strategy_similarity.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_adaptive_alpha_zero_at_no_obs():
    assert ss.adaptive_alpha(0) == 0.0


def test_adaptive_alpha_reaches_ceiling():
    assert abs(ss.adaptive_alpha(ss.ALPHA_FULL_OBS) - ss.RETURN_CORR_ALPHA_CEIL) < 1e-9
    assert abs(ss.adaptive_alpha(10 * ss.ALPHA_FULL_OBS) - ss.RETURN_CORR_ALPHA_CEIL) < 1e-9  # capped


def test_blend_pure_overlap_when_no_return_history():
    overlap = {'A': {'A': 1.0, 'B': 0.5}, 'B': {'A': 0.5, 'B': 1.0}}
    retcorr = {'A': {'A': 1.0, 'B': 0.9}, 'B': {'A': 0.9, 'B': 1.0}}
    n_obs = {('A', 'B'): 0}  # no joint return history → alpha 0 → pure overlap
    blended = ss.blend_similarity(overlap, retcorr, n_obs)
    assert abs(blended['A']['B'] - 0.5) < 1e-9


def test_blend_weights_return_corr_when_history_ample():
    overlap = {'A': {'A': 1.0, 'B': 0.2}, 'B': {'A': 0.2, 'B': 1.0}}
    retcorr = {'A': {'A': 1.0, 'B': 0.9}, 'B': {'A': 0.9, 'B': 1.0}}
    n_obs = {('A', 'B'): ss.ALPHA_FULL_OBS}  # alpha = ceiling 0.6
    blended = ss.blend_similarity(overlap, retcorr, n_obs)
    # 0.4*0.2 + 0.6*0.9 = 0.62
    assert abs(blended['A']['B'] - 0.62) < 1e-9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_similarity.py -k "adaptive or blend" -v`
Expected: FAIL — `AttributeError: module 'execution.strategy_similarity' has no attribute 'adaptive_alpha'`

- [ ] **Step 3: Write minimal implementation (append to strategy_similarity.py)**

```python
def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    import math
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def return_correlation(returns_by_strat: dict[str, dict[str, float]]
                       ) -> tuple[dict[str, dict[str, float]], dict[tuple, int]]:
    """Pearson on per-strategy {date: daily_return}. Returns (matrix, n_obs_per_pair).
    Sparse / zero-variance pairs default to SPARSE_DEFAULT; off-diagonals clipped ±0.95."""
    strats = sorted(returns_by_strat.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    n_obs: dict[tuple, int] = {}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            da, db = returns_by_strat[a], returns_by_strat[b]
            paired = sorted(set(da) & set(db))
            n_obs[(a, b)] = n_obs[(b, a)] = len(paired)
            if len(paired) < 2:
                rho = SPARSE_DEFAULT
            else:
                r = _pearson([da[d] for d in paired], [db[d] for d in paired])
                rho = SPARSE_DEFAULT if r is None else max(-MAX_OFF_DIAGONAL, min(MAX_OFF_DIAGONAL, r))
            out[a][b] = out[b][a] = rho
    return out, n_obs


def adaptive_alpha(n_obs: int) -> float:
    """Weight on return-correlation: 0 at no joint history, rising linearly to the
    ceiling at ALPHA_FULL_OBS overlapping observations, then capped."""
    if n_obs <= 0:
        return 0.0
    return min(RETURN_CORR_ALPHA_CEIL, RETURN_CORR_ALPHA_CEIL * n_obs / ALPHA_FULL_OBS)


def blend_similarity(overlap: dict[str, dict[str, float]],
                     return_corr: dict[str, dict[str, float]],
                     n_obs_per_pair: dict[tuple, int]) -> dict[str, dict[str, float]]:
    """Per-pair convex blend: (1-alpha)*overlap + alpha*return_corr, alpha=adaptive_alpha(n_obs).
    Overlap LEADS; return-corr enters only as joint history accrues. Diagonal 1.0."""
    strats = sorted(overlap.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for a in strats:
        for b in strats:
            if a == b:
                out[a][b] = 1.0
                continue
            o = overlap.get(a, {}).get(b, 0.0)
            r = return_corr.get(a, {}).get(b, SPARSE_DEFAULT)
            al = adaptive_alpha(n_obs_per_pair.get((a, b), 0))
            out[a][b] = (1.0 - al) * o + al * r
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_similarity.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/strategy_similarity.py tests/test_strategy_similarity.py
git commit -m "feat(ortho): return-corr + data-adaptive overlap blend (pure)"
```

---

## Task 6: Hierarchical clustering — two cuts (pure)

**Files:**
- Modify: `src/execution/strategy_similarity.py`
- Modify: `tests/test_strategy_similarity.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_cluster_two_cuts_folds_near_identical_and_blocks_factor():
    # S1,S2 near-identical (0.9); S3 same factor as S1/S2 (0.5); S4 unrelated.
    strats = ['S1', 'S2', 'S3', 'S4']
    sim = {
        'S1': {'S1': 1.0, 'S2': 0.90, 'S3': 0.50, 'S4': 0.05},
        'S2': {'S1': 0.90, 'S2': 1.0, 'S3': 0.50, 'S4': 0.05},
        'S3': {'S1': 0.50, 'S2': 0.50, 'S3': 1.0, 'S4': 0.05},
        'S4': {'S1': 0.05, 'S2': 0.05, 'S3': 0.05, 'S4': 1.0},
    }
    fold, blocks = ss.cluster_two_cuts(sim, strats, fold_thr=0.85, block_thr=0.40)
    # Fold: S1+S2 together; S3 and S4 singletons.
    fold_of = {s: g for g, members in fold.items() for s in members}
    assert fold_of['S1'] == fold_of['S2']
    assert fold_of['S3'] != fold_of['S1'] and fold_of['S4'] != fold_of['S1']
    # Block: S1+S2+S3 together (factor family); S4 alone.
    block_of = {s: g for g, members in blocks.items() for s in members}
    assert block_of['S1'] == block_of['S2'] == block_of['S3']
    assert block_of['S4'] != block_of['S1']


def test_cluster_singletons_when_all_dissimilar():
    strats = ['A', 'B']
    sim = {'A': {'A': 1.0, 'B': 0.1}, 'B': {'A': 0.1, 'B': 1.0}}
    fold, blocks = ss.cluster_two_cuts(sim, strats, fold_thr=0.85, block_thr=0.40)
    assert len({g for g, m in fold.items() for _ in m}) == 2     # two singleton folds
    assert len({g for g, m in blocks.items() for _ in m}) == 2   # two singleton blocks
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_similarity.py -k cluster -v`
Expected: FAIL — `AttributeError: ... has no attribute 'cluster_two_cuts'`

- [ ] **Step 3: Write minimal implementation (append)**

```python
def cluster_two_cuts(sim: dict[str, dict[str, float]], strategies: list[str],
                     fold_thr: float = FOLD_THRESHOLD, block_thr: float = BLOCK_THRESHOLD
                     ) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Agglomerative average-linkage clustering on distance = 1 - similarity.
    Cut at two heights → (fold_groups, factor_blocks). Each maps group_id -> [strategy_id].
    <2 strategies → each its own singleton group."""
    strategies = sorted(strategies)
    n = len(strategies)
    if n < 2:
        groups = {i: [s] for i, s in enumerate(strategies)}
        return dict(groups), dict(groups)

    import numpy as np
    from scipy.cluster.hierarchy import linkage, fcluster
    from scipy.spatial.distance import squareform

    dist = np.zeros((n, n))
    for i, a in enumerate(strategies):
        for k, b in enumerate(strategies):
            if i < k:
                s = max(0.0, min(1.0, sim.get(a, {}).get(b, 0.0)))
                dist[i][k] = dist[k][i] = 1.0 - s
    Z = linkage(squareform(dist, checks=False), method='average')

    def _cut(thr: float) -> dict[int, list[str]]:
        labels = fcluster(Z, t=1.0 - thr, criterion='distance')  # distance cut = 1 - similarity
        groups: dict[int, list[str]] = {}
        for idx, lab in enumerate(labels):
            groups.setdefault(int(lab), []).append(strategies[idx])
        return groups

    return _cut(fold_thr), _cut(block_thr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_similarity.py -v`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/strategy_similarity.py tests/test_strategy_similarity.py
git commit -m "feat(ortho): two-cut hierarchical clustering (pure)"
```

---

## Task 7: Per-regime DB loaders + state-prob blend + rebuild/persist + CLI

**Files:**
- Modify: `src/execution/strategy_similarity.py`

This task wires the pure functions to the DB. The DB I/O mirrors `correlation_matrix.py`'s per-regime + `current_state_probabilities` pattern. No new unit test (pure pieces are covered; exercised by the Task-18 smoke).

- [ ] **Step 1: Add loaders, state-prob blend, representatives, persistence, and CLI (append to strategy_similarity.py)**

```python
def _db():
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()
    return psycopg2.connect(os.environ.get('DATABASE_URL')
                            or os.environ.get('POSTGRES_URI')
                            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _iso_week(d) -> str:
    iso = d.isocalendar()
    return f"{iso[0]}W{iso[1]:02d}"


def _cofiring_sets_by_regime(window_days: int) -> dict[str, dict[str, set]]:
    """{regime_state: {strategy_id: {(iso_week, ticker, direction_int), ...}}} from execution_signals."""
    sql = """
        SELECT regime_state, strategy_id, signal_date, ticker, direction
          FROM execution_signals
         WHERE signal_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
           AND strategy_id IS NOT NULL AND ticker IS NOT NULL
    """
    out: dict[str, dict[str, set]] = {r: {} for r in REGIME_STATES}
    with _db() as conn, conn.cursor() as cur:
        cur.execute(sql, (window_days,))
        for regime, sid, sdate, ticker, direction in cur.fetchall():
            if regime not in out:
                out.setdefault(regime, {})
            d = 1 if str(direction).upper().startswith('L') or str(direction).upper() in ('BUY', 'LONG') else -1
            out[regime].setdefault(sid, set()).add((_iso_week(sdate), ticker, d))
    return out


def _returns_by_regime(window_days: int) -> dict[str, dict[str, dict[str, float]]]:
    """{regime_state: {strategy_id: {date_str: daily_return}}} from strategy_daily_returns."""
    sql = """
        SELECT regime_state, strategy_id, ret_date::text, daily_return_pct::float
          FROM strategy_daily_returns
         WHERE ret_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    out: dict[str, dict[str, dict[str, float]]] = {r: {} for r in REGIME_STATES}
    with _db() as conn, conn.cursor() as cur:
        cur.execute(sql, (window_days,))
        for regime, sid, d, ret in cur.fetchall():
            if regime not in out:
                continue
            out[regime].setdefault(sid, {})[d] = float(ret)
    return out


def current_state_probabilities() -> dict[str, float]:
    """Mirror of correlation_matrix.current_state_probabilities (reads market_regime)."""
    with _db() as conn, conn.cursor() as cur:
        cur.execute("SELECT state, regime_data FROM market_regime ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    out = {r: 0.0 for r in REGIME_STATES}
    if not row:
        return out
    state, data = row
    if isinstance(data, dict):
        for r, v in (data.get('state_probabilities') or {}).items():
            if r in out:
                try:
                    out[r] = float(v)
                except (TypeError, ValueError):
                    pass
    total = sum(out.values())
    if total <= 0:
        if state in out:
            out[state] = 1.0
        return out
    return {r: out[r] / total for r in REGIME_STATES}


def _eff_sharpe_by_strat(regime_state: str) -> dict[str, float]:
    from execution import strategy_weights as sw
    return {r['strategy_id']: float(r['effective_sharpe']) for r in sw.load_current(regime_state)}


def similarity_for_regime(regime_state: str, window_days: int = DEFAULT_WINDOW_DAYS,
                          cofiring=None, returns=None) -> dict[str, dict[str, float]]:
    """Blended similarity matrix for one regime's strategy set (union of co-firing + returns keys)."""
    cofiring = cofiring if cofiring is not None else _cofiring_sets_by_regime(window_days).get(regime_state, {})
    returns = returns if returns is not None else _returns_by_regime(window_days).get(regime_state, {})
    overlap = overlap_similarity(cofiring) if cofiring else {}
    retcorr, n_obs = (return_correlation(returns) if returns else ({}, {}))
    strats = sorted(set(overlap) | set(retcorr))
    if not strats:
        return {}
    # Ensure both matrices cover the full key set (missing → defaults inside blend).
    return blend_similarity(
        {a: {b: overlap.get(a, {}).get(b, 1.0 if a == b else 0.0) for b in strats} for a in strats},
        {a: {b: retcorr.get(a, {}).get(b, 1.0 if a == b else SPARSE_DEFAULT) for b in strats} for a in strats},
        n_obs)


def representatives(fold_groups: dict[int, list[str]],
                    eff_sharpe: dict[str, float]) -> dict[int, str]:
    """group_id -> max-effective_sharpe member (ties broken by strategy_id for determinism)."""
    out: dict[int, str] = {}
    for gid, members in fold_groups.items():
        out[gid] = max(sorted(members), key=lambda s: eff_sharpe.get(s, float('-inf')))
    return out


def rebuild(trigger: str = 'manual', window_days: int = DEFAULT_WINDOW_DAYS, verbose: bool = False) -> dict:
    """Build per-regime similarity + clusters; persist matrix, fold-groups, factor-blocks, audit."""
    cof = _cofiring_sets_by_regime(window_days)
    rets = _returns_by_regime(window_days)
    summary: dict[str, dict] = {}
    with _db() as conn, conn.cursor() as cur:
        for regime in REGIME_STATES:
            sim = similarity_for_regime(regime, window_days,
                                        cofiring=cof.get(regime, {}), returns=rets.get(regime, {}))
            if not sim:
                summary[regime] = {'strategies': 0}
                continue
            strats = sorted(sim.keys())
            fold, blocks = cluster_two_cuts(sim, strats)
            eff = _eff_sharpe_by_strat(regime)
            reps = representatives(fold, eff)

            # Flip old current rows false, insert new.
            cur.execute("UPDATE strategy_similarity_matrix SET is_current=FALSE WHERE regime_state=%s AND is_current", (regime,))
            cur.execute("UPDATE strategy_fold_groups SET is_current=FALSE WHERE regime_state=%s AND is_current", (regime,))
            cur.execute("UPDATE strategy_factor_blocks SET is_current=FALSE WHERE regime_state=%s AND is_current", (regime,))
            import json
            cur.execute("INSERT INTO strategy_similarity_matrix (regime_state, matrix, trigger) VALUES (%s,%s,%s)",
                        (regime, json.dumps(sim), trigger))
            for gid, members in fold.items():
                for s in members:
                    cur.execute("""INSERT INTO strategy_fold_groups
                        (regime_state, group_id, strategy_id, is_representative, effective_sharpe, trigger)
                        VALUES (%s,%s,%s,%s,%s,%s)""",
                        (regime, gid, s, s == reps[gid], eff.get(s), trigger))
                if len(members) >= 2:
                    cur.execute("""INSERT INTO strategy_fold_audit
                        (regime_state, group_id, strategy_ids, representative, member_sharpes)
                        VALUES (%s,%s,%s,%s,%s)""",
                        (regime, gid, members, reps[gid],
                         json.dumps({s: eff.get(s) for s in members})))
            for bid, members in blocks.items():
                for s in members:
                    cur.execute("""INSERT INTO strategy_factor_blocks
                        (regime_state, block_id, strategy_id, trigger) VALUES (%s,%s,%s,%s)""",
                        (regime, bid, s, trigger))
            summary[regime] = {'strategies': len(strats),
                               'fold_groups': sum(1 for m in fold.values() if len(m) >= 2),
                               'factor_blocks': sum(1 for m in blocks.values() if len(m) >= 2)}
            if verbose:
                print(f"[{regime}] {summary[regime]}")
        conn.commit()
    return summary


def load_groups(regime_state: str) -> dict:
    """Live read for the sizer: {fold_map, rep_map, block_map, matrix}.
    fold_map: strategy_id -> group_id (multi-member groups only).
    rep_map:  group_id -> representative strategy_id.
    block_map: strategy_id -> block_id (multi-member blocks only).
    matrix:   {strategy_id: {strategy_id: rho}} (current row, or {})."""
    out = {'fold_map': {}, 'rep_map': {}, 'block_map': {}, 'matrix': {}}
    with _db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT group_id, strategy_id, is_representative FROM strategy_fold_groups
                       WHERE regime_state=%s AND is_current""", (regime_state,))
        members: dict[int, list[str]] = {}
        for gid, sid, is_rep in cur.fetchall():
            members.setdefault(gid, []).append(sid)
            if is_rep:
                out['rep_map'][gid] = sid
        for gid, ms in members.items():
            if len(ms) >= 2:
                for s in ms:
                    out['fold_map'][s] = gid
        cur.execute("""SELECT block_id, strategy_id FROM strategy_factor_blocks
                       WHERE regime_state=%s AND is_current""", (regime_state,))
        bmembers: dict[int, list[str]] = {}
        for bid, sid in cur.fetchall():
            bmembers.setdefault(bid, []).append(sid)
        for bid, ms in bmembers.items():
            if len(ms) >= 2:
                for s in ms:
                    out['block_map'][s] = bid
        cur.execute("""SELECT matrix FROM strategy_similarity_matrix
                       WHERE regime_state=%s AND is_current ORDER BY id DESC LIMIT 1""", (regime_state,))
        row = cur.fetchone()
        if row and isinstance(row[0], dict):
            out['matrix'] = row[0]
    return out


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--rebuild', action='store_true')
    p.add_argument('--trigger', default='manual')
    p.add_argument('--window-days', type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument('--verbose', action='store_true')
    args = p.parse_args()
    if args.rebuild:
        s = rebuild(trigger=args.trigger, window_days=args.window_days, verbose=args.verbose)
        print('similarity rebuild:', s)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
```

- [ ] **Step 2: Smoke the rebuild against live DB**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m execution.strategy_similarity --rebuild --trigger=manual --verbose`
Expected: prints a per-regime summary dict; no exception. (Group/block counts may be 0 early — expected; the engine is correctly inert until co-firing/return history reaches the cuts.)

- [ ] **Step 3: Run the full unit suite (no regressions)**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_similarity.py -v`
Expected: PASS (11 passed)

- [ ] **Step 4: Commit**

```bash
git add src/execution/strategy_similarity.py
git commit -m "feat(ortho): per-regime similarity rebuild + persistence + live loader + CLI"
```

---

## Task 8: Pure live transforms — fold contributions

**Files:**
- Create: `src/execution/orthogonalization.py`
- Test: `tests/test_orthogonalization.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for orthogonalization — Tier-1 fold + Tier-2 k_eff (pure)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import orthogonalization as og  # noqa: E402


def _sig(sid, ticker, direction):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction}


def test_fold_collapses_same_group_same_dir_to_representative():
    active = [_sig('S1', 'AAPL', 'LONG'), _sig('S2', 'AAPL', 'LONG'), _sig('S3', 'MSFT', 'LONG')]
    fold_map = {'S1': 1, 'S2': 1}          # S1,S2 same fold-group; S3 singleton (absent)
    rep_map = {1: 'S1'}                      # S1 is representative
    eff = {'S1': 2.0, 'S2': 1.0, 'S3': 1.5}
    out = og.fold_active_contributions(active, fold_map, rep_map, eff)
    sids = sorted(s['strategy_id'] for s in out)
    assert sids == ['S1', 'S3']              # S2 dropped (duplicate of S1 on AAPL/LONG)


def test_fold_fallback_to_highest_sharpe_when_representative_absent():
    active = [_sig('S2', 'AAPL', 'LONG'), _sig('S3', 'AAPL', 'LONG')]
    fold_map = {'S1': 1, 'S2': 1, 'S3': 1}   # all one group; rep S1 didn't fire
    rep_map = {1: 'S1'}
    eff = {'S1': 5.0, 'S2': 2.0, 'S3': 3.0}
    out = og.fold_active_contributions(active, fold_map, rep_map, eff)
    assert [s['strategy_id'] for s in out] == ['S3']   # highest-eff firing member


def test_fold_keeps_opposite_directions_in_same_group():
    active = [_sig('S1', 'AAPL', 'LONG'), _sig('S2', 'AAPL', 'SHORT')]
    fold_map = {'S1': 1, 'S2': 1}
    rep_map = {1: 'S1'}
    eff = {'S1': 2.0, 'S2': 1.0}
    out = og.fold_active_contributions(active, fold_map, rep_map, eff)
    assert len(out) == 2   # opposite directions are NOT duplicates — both kept


def test_fold_passes_through_ungrouped_strategies():
    active = [_sig('X', 'AAPL', 'LONG'), _sig('Y', 'AAPL', 'LONG')]
    out = og.fold_active_contributions(active, {}, {}, {'X': 1.0, 'Y': 1.0})
    assert len(out) == 2   # no fold-groups → unchanged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization.py -k fold -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.orthogonalization'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Pure live transforms for strategy orthogonalization, consumed by the sizer.

Tier-1 (fold): collapse same-fold-group / same-direction / same-ticker contributions
to a single representative BEFORE the ticker_w / ticker_net_sharpe sums.
Tier-2 (k_eff): deflate within-factor-block conviction at the GATE only.

Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations


def _dir_to_int(direction) -> int:
    d = str(direction or '').upper()
    if d.startswith('L') or d in ('BUY',):
        return 1
    if d.startswith('S') or d in ('SELL',):
        return -1
    return 0


def fold_active_contributions(active: list[dict], fold_map: dict[str, int],
                              rep_map: dict[int, str], eff_sharpe: dict[str, float]) -> list[dict]:
    """For each (ticker, direction, fold_group) bucket of grouped contributions, keep ONE:
    the representative if it fired, else the highest-effective_sharpe member that fired.
    Ungrouped (singleton) contributions pass through untouched."""
    kept: list[dict] = []
    buckets: dict[tuple, list[dict]] = {}
    for s in active:
        sid = s.get('strategy_id')
        gid = fold_map.get(sid)
        if gid is None:
            kept.append(s)                                  # ungrouped → keep
            continue
        key = (s.get('ticker'), _dir_to_int(s.get('direction')), gid)
        buckets.setdefault(key, []).append(s)
    for (ticker, d, gid), members in buckets.items():
        rep = rep_map.get(gid)
        chosen = next((m for m in members if m.get('strategy_id') == rep), None)
        if chosen is None:
            chosen = max(members, key=lambda m: eff_sharpe.get(m.get('strategy_id'), float('-inf')))
        kept.append(chosen)
    return kept
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization.py -k fold -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/orthogonalization.py tests/test_orthogonalization.py
git commit -m "feat(ortho): Tier-1 fold contributions (pure)"
```

---

## Task 9: Pure live transforms — k_eff + block conviction (the floor-preserving formula)

**Files:**
- Modify: `src/execution/orthogonalization.py`
- Modify: `tests/test_orthogonalization.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_k_eff_endpoints():
    assert abs(og.k_eff(5, 0.0) - 5.0) < 1e-9     # uncorrelated → full count
    assert abs(og.k_eff(5, 1.0) - 1.0) < 1e-9     # identical → one bet
    assert abs(og.k_eff(2, 0.9) - (2 / 1.9)) < 1e-9
    assert og.k_eff(1, 0.5) == 1.0                 # single member guard


def test_block_conviction_floor_never_below_max_member():
    # strong 3.5 + correlated weak 1.0 at rho 0.5: must stay ABOVE 3.5 (the strong standalone)
    conv = og.block_conviction([3.5, 1.0], 0.5)
    assert conv > 3.5
    assert abs(conv - 3.8333333) < 1e-4            # 3.5 + (4.5-3.5)*(k_eff-1)/(k-1)


def test_block_conviction_endpoints():
    assert abs(og.block_conviction([1.0, 1.0, 1.0], 0.0) - 3.0) < 1e-9   # rho 0 → sum
    assert abs(og.block_conviction([2.0, 1.0], 1.0) - 2.0) < 1e-9        # rho 1 → max
    assert og.block_conviction([4.0], 0.9) == 4.0                         # singleton → itself


def test_deflated_net_sharpe_gate_value():
    # AAPL: block B1 = {S1,S2} LONG (rho 0.5, sharpes 3.5 & 1.0); block B2 = {S3} LONG sharpe 2.0
    contribs = {'AAPL': [('S1', 1), ('S2', 1), ('S3', 1)]}
    block_map = {'S1': 10, 'S2': 10}              # S3 ungrouped → its own pseudo-block
    sim = {'S1': {'S2': 0.5}, 'S2': {'S1': 0.5}}
    eff = {'S1': 3.5, 'S2': 1.0, 'S3': 2.0}
    out = og.deflated_net_sharpe(contribs, block_map, sim, eff)
    # B1 conviction ~3.833 + B2 (singleton) 2.0 = ~5.833 (vs naive 6.5)
    assert abs(out['AAPL'] - 5.8333333) < 1e-3


def test_deflated_net_sharpe_cross_block_full_credit_and_signs():
    # Two uncorrelated single-strategy blocks, opposite directions → signed sum
    contribs = {'XYZ': [('A', 1), ('B', -1)]}
    out = og.deflated_net_sharpe(contribs, {}, {}, {'A': 4.0, 'B': 1.0})
    assert abs(out['XYZ'] - 3.0) < 1e-9            # +4 -1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization.py -k "k_eff or block or deflated" -v`
Expected: FAIL — `AttributeError: ... has no attribute 'k_eff'`

- [ ] **Step 3: Write minimal implementation (append to orthogonalization.py)**

```python
def k_eff(k: int, rho_bar: float) -> float:
    """Effective number of independent bets among k correlated members. ∈ [1, k]."""
    if k <= 1:
        return 1.0
    rho_bar = max(0.0, min(1.0, rho_bar))
    return k / (1.0 + (k - 1) * rho_bar)


def block_conviction(sharpes: list[float], rho_bar: float) -> float:
    """Floor-preserving deflation: never below the strongest single member.
        conviction = max + (sum - max) * (k_eff - 1)/(k - 1)
    rho_bar→1 ⇒ max (one bet); rho_bar→0 ⇒ sum (full credit)."""
    k = len(sharpes)
    if k == 0:
        return 0.0
    if k == 1:
        return sharpes[0]
    ke = k_eff(k, rho_bar)
    mx = max(sharpes)
    return mx + (sum(sharpes) - mx) * (ke - 1.0) / (k - 1.0)


def _mean_pairwise(members: list[str], sim: dict[str, dict[str, float]]) -> float:
    if len(members) < 2:
        return 0.0
    vals = []
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            vals.append(sim.get(a, {}).get(b, sim.get(b, {}).get(a, 0.05)))
    return sum(vals) / len(vals) if vals else 0.0


def deflated_net_sharpe(contribs_by_ticker: dict[str, list[tuple]],
                        block_map: dict[str, int],
                        sim: dict[str, dict[str, float]],
                        eff_sharpe: dict[str, float]) -> dict[str, float]:
    """contribs_by_ticker: {ticker: [(strategy_id, direction_int), ...]} (post-fold survivors).
    Returns {ticker: signed deflated net-Sharpe}. Within each (block, direction): floor-preserving
    block_conviction with rho_bar = mean pairwise similarity among that block's firing members.
    Ungrouped strategies are their own singleton block (no deflation). Cross-block = full signed credit."""
    out: dict[str, float] = {}
    singleton_seq = -1
    for ticker, contribs in contribs_by_ticker.items():
        # group (block_id, direction) -> [strategy_id]
        groups: dict[tuple, list[str]] = {}
        local_singleton = {}
        for sid, d in contribs:
            bid = block_map.get(sid)
            if bid is None:
                bid = local_singleton.setdefault(sid, singleton_seq)
                singleton_seq -= 1
            groups.setdefault((bid, d), []).append(sid)
        net = 0.0
        for (bid, d), members in groups.items():
            rho = _mean_pairwise(members, sim)
            conv = block_conviction([eff_sharpe.get(s, 0.0) for s in members], rho)
            net += conv * d
        out[ticker] = net
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add src/execution/orthogonalization.py tests/test_orthogonalization.py
git commit -m "feat(ortho): Tier-2 k_eff + floor-preserving block conviction (pure)"
```

---

## Task 10: Wire Tier-1 fold into the sizer (gated) + byte-identical regression

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:334–384`
- Test: `tests/test_orthogonalization_sizer.py`

- [ ] **Step 1: Write the byte-identical regression test**

This test asserts that with both gates unset, `fold_active_contributions` is a no-op on a representative input, and (integration) that the sizer module imports cleanly. The deep sizer math is covered by `test_regime_blended_sizer.py` (unchanged behavior when gates off).

```python
"""Sizer-integration tests for orthogonalization gates (default-OFF byte-identical)."""
from __future__ import annotations
import os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import orthogonalization as og  # noqa: E402


def test_fold_noop_when_no_groups():
    active = [{'strategy_id': 'A', 'ticker': 'AAPL', 'direction': 'LONG'},
              {'strategy_id': 'B', 'ticker': 'AAPL', 'direction': 'LONG'}]
    # empty fold_map (gates off ⇒ load_groups not called ⇒ no folding)
    assert og.fold_active_contributions(active, {}, {}, {}) == active


def test_gate_env_default_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_STRATEGY_FOLD', raising=False)
    monkeypatch.delenv('OPENCLAW_STRATEGY_CORR_WEIGHT', raising=False)
    from execution import regime_blended_sizer as rbs
    assert rbs._ortho_enabled('OPENCLAW_STRATEGY_FOLD') is False
    assert rbs._ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT') is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization_sizer.py -v`
Expected: FAIL — `AttributeError: module 'execution.regime_blended_sizer' has no attribute '_ortho_enabled'` (the first test passes already).

- [ ] **Step 3: Add the gate helper + Tier-1 call-site**

In `src/execution/regime_blended_sizer.py`, near the top (after existing imports), add:

```python
def _ortho_enabled(gate: str) -> bool:
    import os
    return os.environ.get(gate) == '1'
```

Then inside `_sharpe_cadence_path`, immediately AFTER the `active = _load_active_window_signals(...)` / fallback block (after line ~365, before the `from collections import defaultdict` aggregation at line ~372), insert:

```python
    # Strategy orthogonalization (default-OFF; byte-identical when both gates unset).
    _ortho_groups = None
    if _ortho_enabled('OPENCLAW_STRATEGY_FOLD') or _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT') \
            or _ortho_enabled('OPENCLAW_STRATEGY_ORTHO_SHADOW'):
        try:
            from execution import strategy_similarity as _ss
            _ortho_groups = _ss.load_groups(regime_state)
        except Exception as e:
            logger.warning('orthogonalization: load_groups failed (%s); proceeding without', e)
            _ortho_groups = None

    if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_FOLD'):
        from execution import orthogonalization as _og
        before = len(active)
        active = _og.fold_active_contributions(
            active, _ortho_groups['fold_map'], _ortho_groups['rep_map'], sharpe_by_strat)
        logger.info('orthogonalization.fold: %d → %d contributions', before, len(active))
```

(Note: `sharpe_by_strat` is already defined at line 342 as `{strategy_id: effective_sharpe}`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization_sizer.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Run the existing sizer suite to confirm no regression**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_regime_blended_sizer.py -v`
Expected: PASS (all existing tests still green — gates default-OFF ⇒ unchanged path)

- [ ] **Step 6: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/test_orthogonalization_sizer.py
git commit -m "feat(ortho): wire Tier-1 fold into sizer (gated default-OFF)"
```

---

## Task 11: Wire Tier-2 gate-deflation into the sizer (gated)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:405–417`
- Modify: `tests/test_orthogonalization_sizer.py`

- [ ] **Step 1: Write the failing test (append)**

```python
def test_gate_uses_deflated_when_corr_weight_on(monkeypatch):
    # Build the per-ticker contributor map the sizer passes to deflated_net_sharpe,
    # and confirm the deflated gate value < naive sum for a correlated block.
    contribs = {'AAPL': [('S1', 1), ('S2', 1)]}
    block_map = {'S1': 1, 'S2': 1}
    sim = {'S1': {'S2': 0.9}, 'S2': {'S1': 0.9}}
    eff = {'S1': 2.0, 'S2': 2.0}
    deflated = og.deflated_net_sharpe(contribs, block_map, sim, eff)['AAPL']
    naive = 2.0 + 2.0
    assert deflated < naive          # correlated pair counts as < 2 independent
    assert deflated >= 2.0           # floor: never below the strongest member
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization_sizer.py -k deflated -v`
Expected: PASS already (pure function exists) — this test guards the property the wiring relies on. (If it fails, the formula is wrong; stop and fix Task 9.)

- [ ] **Step 3: Add the Tier-2 gate call-site**

In `_sharpe_cadence_path`, the existing cumulative-sharpe gate is at lines 409–417 and reads `ticker_net_sharpe.get(tkr, 0.0)`. Replace the gate's source value with a deflated one when the gate is enabled. Insert BEFORE the gate block (before line 409):

```python
    # Tier-2: deflate the conviction gate by effective-independent-bets (gate only; sizing untouched).
    gate_net_sharpe = ticker_net_sharpe
    if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT'):
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

Then change the gate test at line 410–411 from:

```python
        gated_out = [tkr for tkr in list(ticker_w.keys())
                     if abs(ticker_net_sharpe.get(tkr, 0.0)) < min_cum_sharpe]
```

to:

```python
        gated_out = [tkr for tkr in list(ticker_w.keys())
                     if abs(gate_net_sharpe.get(tkr, 0.0)) < min_cum_sharpe]
```

(`ticker_w` — the sizing weight — is NOT modified; only the gate's input changes. When `OPENCLAW_STRATEGY_CORR_WEIGHT` is unset, `gate_net_sharpe is ticker_net_sharpe`, so behavior is byte-identical.)

- [ ] **Step 4: Run tests**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_orthogonalization_sizer.py tests/test_regime_blended_sizer.py -v`
Expected: PASS (all green; gate-off unchanged)

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/test_orthogonalization_sizer.py
git commit -m "feat(ortho): wire Tier-2 gate deflation into sizer (gated default-OFF)"
```

---

## Task 12: Shadow mode — compute, log delta + similarity histogram, route nothing

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (after the gate block, ~line 421)

- [ ] **Step 1: Add the shadow comparison (no new unit test — log-only, verified by Task-18 smoke)**

Inside `_sharpe_cadence_path`, AFTER the cumulative-sharpe gate block (after line ~421, where `ticker_w` reflects survivors) insert:

```python
    # Shadow: log what orthogonalization WOULD change without affecting routing.
    if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_ORTHO_SHADOW') \
            and not (_ortho_enabled('OPENCLAW_STRATEGY_FOLD') or _ortho_enabled('OPENCLAW_STRATEGY_CORR_WEIGHT')):
        try:
            from execution import orthogonalization as _og
            shadow_contribs = {
                tkr: list(zip(meta['strategies'], meta['directions']))
                for tkr, meta in ticker_meta.items()
            }
            shadow_gate = _og.deflated_net_sharpe(
                shadow_contribs, _ortho_groups['block_map'],
                _ortho_groups['matrix'], sharpe_by_strat)
            would_drop = [t for t in ticker_w
                          if abs(shadow_gate.get(t, 0.0)) < min_cum_sharpe]
            mat = _ortho_groups.get('matrix') or {}
            offdiag = [mat[a][b] for a in mat for b in mat.get(a, {}) if a != b]
            hist = {}
            for v in offdiag:
                bucket = round(float(v) * 10) / 10
                hist[bucket] = hist.get(bucket, 0) + 1
            logger.info('orthogonalization.shadow: would_drop=%s similarity_histogram=%s '
                        'fold_pairs=%d block_pairs=%d',
                        would_drop, dict(sorted(hist.items())),
                        len(_ortho_groups['fold_map']), len(_ortho_groups['block_map']))
        except Exception as e:
            logger.warning('orthogonalization.shadow failed (%s)', e)
```

- [ ] **Step 2: Sanity import + lint**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -c "import execution.regime_blended_sizer; print('import ok')"`
Expected: `import ok`

- [ ] **Step 3: Commit**

```bash
git add src/execution/regime_blended_sizer.py
git commit -m "feat(ortho): shadow mode — log gate delta + similarity histogram"
```

---

## Task 13: Chronic-fold report → `#strategy-memos`

**Files:**
- Create: `src/execution/fold_report.py`

The report reads `strategy_fold_audit`, finds groups that persisted ≥ N weekly runs, and posts to Discord. Reuse the existing webhook pattern (find it: `grep -rn "strategy-memos\|STRATEGY_MEMOS\|webhook" src/agent/curators/comprehensive_review.js` — mirror how memos are posted).

- [ ] **Step 1: Implement the report**

```python
#!/usr/bin/env python3
"""Weekly chronic-fold report: strategy groups that fold together repeatedly.
Posts a recommendation digest to #strategy-memos for MANUAL retirement decisions.
Feeds the future Tier-3 monthly convergence process (spec §10)."""
from __future__ import annotations

import os
from collections import defaultdict


def _db():
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv()
    return psycopg2.connect(os.environ.get('DATABASE_URL')
                            or os.environ.get('POSTGRES_URI')
                            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def chronic_folds(min_runs: int = 3, lookback_days: int = 35) -> list[dict]:
    """Groups (by sorted strategy_ids) appearing in ≥ min_runs distinct audit runs."""
    sql = """
        SELECT regime_state, strategy_ids, representative, member_sharpes, run_at::date
          FROM strategy_fold_audit
         WHERE run_at >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    runs_by_key: dict[tuple, set] = defaultdict(set)
    meta: dict[tuple, dict] = {}
    with _db() as conn, conn.cursor() as cur:
        cur.execute(sql, (lookback_days,))
        for regime, sids, rep, sharpes, run_date in cur.fetchall():
            key = (regime, tuple(sorted(sids)))
            runs_by_key[key].add(run_date)
            meta[key] = {'regime': regime, 'strategy_ids': sorted(sids),
                         'representative': rep, 'member_sharpes': sharpes}
    return [{**meta[k], 'runs': len(v)} for k, v in runs_by_key.items() if len(v) >= min_runs]


def format_report(rows: list[dict]) -> str:
    if not rows:
        return 'No chronic fold-groups this week.'
    lines = ['**Chronic fold-groups** (candidates for manual retirement):']
    for r in rows:
        members = ', '.join(f"{s}({(r['member_sharpes'] or {}).get(s, '?')})" for s in r['strategy_ids'])
        lines.append(f"- [{r['regime']}] {members} — folded {r['runs']}× → keep **{r['representative']}**")
    return '\n'.join(lines)


def main():
    rows = chronic_folds()
    msg = format_report(rows)
    print(msg)
    # Discord post (mirror comprehensive_review.js webhook usage; gated to avoid noise in dev)
    if os.environ.get('OPENCLAW_FOLD_REPORT_POST') == '1':
        import json, urllib.request
        url = os.environ.get('DISCORD_STRATEGY_MEMOS_WEBHOOK')
        if url:
            req = urllib.request.Request(url, data=json.dumps({'content': msg[:1900]}).encode(),
                                         headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=10)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
```

- [ ] **Step 2: Verify the webhook env name**

Run: `cd /root/openclaw && grep -rn "STRATEGY_MEMOS\|strategy-memos" src/ .env.example | head`
Expected: confirm the correct webhook env var name; if it differs from `DISCORD_STRATEGY_MEMOS_WEBHOOK`, update the constant in `fold_report.py` to match.

- [ ] **Step 3: Smoke (prints report; does not post unless gate set)**

Run: `cd /root/openclaw && PYTHONPATH=src python3 -m execution.fold_report`
Expected: prints `No chronic fold-groups this week.` (early, before history accrues) — no exception.

- [ ] **Step 4: Commit**

```bash
git add src/execution/fold_report.py
git commit -m "feat(ortho): chronic-fold report for manual retirement (#strategy-memos)"
```

---

## Task 14: Wire weekly rebuild into `weekly_live_sharpe.js`

**Files:**
- Modify: `src/agent/curators/weekly_live_sharpe.js:~49`

- [ ] **Step 1: Add the similarity rebuild + fold report after the weights rebuild**

Find the existing block (around line 49):

```javascript
    execSync(`cd ${ROOT} && PYTHONPATH=src python3 -m execution.strategy_weights --rebuild --trigger=weekly_cron --verbose`,
```

Immediately AFTER that `execSync(...)` statement completes (after its closing `);` and any surrounding try/catch), add:

```javascript
  // Orthogonalization: rebuild per-regime similarity + fold/block groups, then chronic-fold report.
  try {
    console.log('rebuilding strategy similarity (orthogonalization)…');
    execSync(`cd ${ROOT} && PYTHONPATH=src python3 -m execution.strategy_similarity --rebuild --trigger=weekly_cron --verbose`,
      { stdio: 'inherit' });
    execSync(`cd ${ROOT} && OPENCLAW_FOLD_REPORT_POST=1 PYTHONPATH=src python3 -m execution.fold_report`,
      { stdio: 'inherit' });
  } catch (e) {
    console.error('similarity rebuild / fold report failed (non-fatal):', e.message);
  }
```

(Place the `strategy_daily_returns` rebuild first so similarity sees fresh returns:)

```javascript
  try {
    execSync(`cd ${ROOT} && PYTHONPATH=src python3 -c "from execution import strategy_returns as s; print('daily_returns rows:', s.rebuild_daily_returns(trigger='weekly_cron'))"`,
      { stdio: 'inherit' });
  } catch (e) {
    console.error('strategy_daily_returns rebuild failed (non-fatal):', e.message);
  }
```

(Insert the `strategy_daily_returns` block BEFORE the similarity rebuild block.)

- [ ] **Step 2: Syntax-check the JS**

Run: `cd /root/openclaw && node --check src/agent/curators/weekly_live_sharpe.js && echo "syntax ok"`
Expected: `syntax ok`

- [ ] **Step 3: Commit**

```bash
git add src/agent/curators/weekly_live_sharpe.js
git commit -m "feat(ortho): wire daily-returns + similarity rebuild + fold report into weekly cron"
```

---

## Task 15: End-to-end smoke + full regression

**Files:** none (verification only)

- [ ] **Step 1: Run the daily-returns → similarity → groups → report chain on the live DB**

Run:
```bash
cd /root/openclaw && PYTHONPATH=src python3 -c "
from execution import strategy_returns as sr, strategy_similarity as ss, fold_report as fr
print('daily_returns rows:', sr.rebuild_daily_returns(trigger='smoke'))
print('similarity:', ss.rebuild(trigger='smoke', verbose=True))
print('groups LOW_VOL:', {k: (len(v) if isinstance(v, dict) else v) for k, v in ss.load_groups('LOW_VOL').items()})
print(fr.format_report(fr.chronic_folds()))
"
```
Expected: no exceptions; prints per-regime summaries and a (likely empty) group map. Early inertness is expected and correct.

- [ ] **Step 2: Confirm shadow mode logs without routing**

Run:
```bash
cd /root/openclaw && OPENCLAW_STRATEGY_ORTHO_SHADOW=1 PYTHONPATH=src python3 -c "import execution.regime_blended_sizer as r; print('shadow import ok')"
```
Expected: `shadow import ok` (full shadow output appears in the next live cycle's sizer logs).

- [ ] **Step 3: Run the full orthogonalization + sizer + correlation test suites**

Run:
```bash
cd /root/openclaw && PYTHONPATH=src python3 -m pytest tests/test_strategy_returns.py tests/test_strategy_similarity.py tests/test_orthogonalization.py tests/test_orthogonalization_sizer.py tests/test_regime_blended_sizer.py tests/test_correlation_matrix.py -v
```
Expected: ALL PASS. The pre-existing sizer + correlation tests passing confirms the byte-identical (gates-OFF) invariant.

- [ ] **Step 4: Final commit (if any cleanup)**

```bash
git add -A && git commit -m "test(ortho): end-to-end smoke + regression green" --allow-empty
```

---

## Deployment notes (post-merge, operator-gated — NOT part of TDD tasks)

1. Apply migration 123 on the VPS (Task 1 Step 2 against prod `DATABASE_URL`).
2. Soak with `OPENCLAW_STRATEGY_ORTHO_SHADOW=1` only — watch the similarity histogram in sizer logs to see whether any pairs reach the 0.85 / 0.40 cuts (per spec §5.3 / §10). Tune `OPENCLAW_FOLD_THRESHOLD` / `OPENCLAW_BLOCK_THRESHOLD` from what you observe.
3. Flip `OPENCLAW_STRATEGY_FOLD=1`, soak, then `OPENCLAW_STRATEGY_CORR_WEIGHT=1`.
4. The weekly cron (`weekly_live_sharpe.js`, Sun 06:00 ET) now also rebuilds returns + similarity + posts the fold report. Confirm the first Sunday run in `#strategy-memos`.

## Out of scope (follow-on specs)

- Tier-3 monthly stack convergence / pruning (spec §10).
- Options & sector/ETF corroboration.
- Information ratio (the `strategy_daily_returns` table this plan lands is its prerequisite).
- Tier-2 sizing de-concentration sub-gate.
