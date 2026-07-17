# size_scalar Wiring + Per-Strategy Alpha-Contribution Display — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the human-approved per-(strategy,regime) `size_scalar` into the live sharpe-cadence sizer's allocation weight (allocation-only, shadow-gated), then surface the actual signed per-representative allocation term on open + closed positions (entry + current/close + contributions), dropping the historical IR/mean_pct stats.

**Architecture:** A new gate `OPENCLAW_STRATEGY_SIZE_SCALAR` (default OFF). When ON, `_sharpe_cadence_path` multiplies each strategy's `daily_weight` by its approved `size_scalar` (batch-loaded raw from `strategy_regime_params`, missing→1.0, explicit 0.0 honored) before the `ticker_w` sum; the cum-sharpe gate and fold representative-selection stay on raw `effective_sharpe`. When OFF, routing is byte-identical and a per-ticker dollar diff is logged (shadow). The signed per-representative contribution (`daily_weight × size_scalar × direction`) is persisted per cycle into `cycle_contributing_strategies.contributions` (JSONB, migration 134) and rendered by the rewritten `/api/portfolio/ticker-alpha` endpoint + modal.

**Tech Stack:** Python 3 (sizer, resolver — pytest), PostgreSQL (psycopg2; additive migration), Node/Express + vanilla-JS client (`server.js`).

**Worktree:** `/tmp/wt-trade-output` on branch `feat/trade-output-accuracy`. The worktree has **no `.env`** — DB-touching commands export `POSTGRES_URI` from `/root/openclaw/.env`. Pure unit tests need no DB. The VPS is 2-core/no-swap → run pytest sequentially with `nice -n 19`.

**Spec:** `docs/superpowers/specs/2026-06-09-size-scalar-wiring-and-alpha-contribution-design.md`

---

## File structure

- **Create** `src/database/migrations/134_cycle_contributing_contributions.sql` — additive `contributions JSONB` column.
- **Modify** `src/execution/regime_param_resolver.py` — add `size_scalars_for_regime()` (raw batch read; no PHASE1 fallback).
- **Modify** `src/execution/regime_blended_sizer.py` — add pure helpers `_apply_size_scalars`, `_scaled_target_diff`, `_build_contributions`; wire them into `_sharpe_cadence_path`.
- **Modify** `src/execution/regime_blended_sizer_live.py` — add pure `_collapse_contributions`; extend `_persist_contributing_strategies` to write the JSONB.
- **Modify** `src/channels/api/server.js` — rewrite the `/api/portfolio/ticker-alpha/:ticker` endpoint body + the client `_fetchTickerAlpha`/`_buildAlphaBarsHtml` renderer.
- **Modify** `src/execution/strategy_weights.py` — correct the stale "position-recs govern sizing" docstring.
- **Tests:** `tests/test_regime_param_resolver.py` (extend), `tests/test_size_scalar_wiring.py` (new), `tests/test_contributing_strategies_persist.py` (extend).

---

### Task 1: Migration 134 — `contributions` JSONB column

**Files:**
- Create: `src/database/migrations/134_cycle_contributing_contributions.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 2026-06-09: per-cycle, per-ticker SIGNED alpha contributions.
-- Extends migration 130 (cycle_contributing_strategies). For each corr-gate
-- representative the sizer keeps for a ticker, store its actual signed
-- allocation term daily_weight × size_scalar × direction (sharpe units; the
-- exact value summed into ticker_w). Read by /api/portfolio/ticker-alpha to
-- show signed contributions on open + closed positions. Additive only — the
-- existing `strategies TEXT[]` column is preserved for back-compat fallback.
ALTER TABLE cycle_contributing_strategies
    ADD COLUMN IF NOT EXISTS contributions JSONB;
```

- [ ] **Step 2: Apply to the live DB**

Run:
```bash
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2-) \
  psql "$POSTGRES_URI" -f /tmp/wt-trade-output/src/database/migrations/134_cycle_contributing_contributions.sql
```
Expected: `ALTER TABLE`

- [ ] **Step 3: Verify column exists**

Run:
```bash
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2-) \
  psql "$POSTGRES_URI" -c "\d cycle_contributing_strategies" | grep contributions
```
Expected: a line showing `contributions | jsonb`

- [ ] **Step 4: Commit**

```bash
cd /tmp/wt-trade-output
git add src/database/migrations/134_cycle_contributing_contributions.sql
git commit -m "feat(migration 134): contributions JSONB on cycle_contributing_strategies"
```

---

### Task 2: Resolver — `size_scalars_for_regime()` (raw batch read)

**Files:**
- Modify: `src/execution/regime_param_resolver.py` (append a new function after `size_scalar`, ~line 128)
- Test: `tests/test_regime_param_resolver.py`

> **Why a new function, not `size_scalar()`:** `size_scalar()` falls back to `PHASE1_REGIME_SCALARS` (e.g. TRANSITIONING→0.55) on NULL — a regime dampener, NOT a neutral multiplier. The sizer needs **raw** per-strategy values with missing→1.0, so it must not use that getter.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_regime_param_resolver.py`:
```python
def test_size_scalars_for_regime_raw_only_no_phase1_fallback():
    from execution import regime_param_resolver as rpr
    # Injected fetch returns rows for LOW_VOL: one real scalar, one explicit 0.0.
    rows = [('momentum_12_1', 1.5), ('S_muted', 0.0)]
    out = rpr.size_scalars_for_regime('LOW_VOL', _fetch=lambda regime: rows)
    assert out == {'momentum_12_1': 1.5, 'S_muted': 0.0}   # explicit 0.0 honored
    # A strategy ABSENT from the dict must NOT get a PHASE1 dampener — the
    # caller defaults it to 1.0; the loader simply omits it.
    assert 'absent' not in out


def test_size_scalars_for_regime_coerces_bad_values_to_one():
    from execution import regime_param_resolver as rpr
    rows = [('a', float('nan')), ('b', -0.5), ('c', float('inf')), ('d', 1.25)]
    out = rpr.size_scalars_for_regime('LOW_VOL', _fetch=lambda regime: rows)
    assert out == {'d': 1.25}   # nan/inf/negative dropped → caller defaults them to 1.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /tmp/wt-trade-output && nice -n 19 python3 -m pytest tests/test_regime_param_resolver.py -k size_scalars_for_regime -v`
Expected: FAIL — `AttributeError: module 'execution.regime_param_resolver' has no attribute 'size_scalars_for_regime'`

- [ ] **Step 3: Implement**

Append to `src/execution/regime_param_resolver.py`:
```python
import math


def _fetch_size_scalars(regime_state: str) -> list:
    """Raw (strategy_id, size_scalar) rows for a regime where size_scalar is set."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT strategy_id, size_scalar
                  FROM strategy_regime_params
                 WHERE regime_state = %s AND size_scalar IS NOT NULL
            """, (regime_state,))
            return cur.fetchall()


def size_scalars_for_regime(regime_state: str, _fetch=None) -> dict:
    """Batch-load RAW per-strategy size_scalar for a regime → {strategy_id: float}.

    Distinct from size_scalar(): NO PHASE1_REGIME_SCALARS fallback. A strategy
    absent here is treated by the caller as 1.0 (no change). Only finite,
    non-negative values are kept; nan/inf/negative are dropped (caller → 1.0).
    An explicit 0.0 is kept (deliberate mute). Not cached — called once/cycle.
    """
    fetch = _fetch or _fetch_size_scalars
    out: dict = {}
    for sid, val in fetch(regime_state):
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v >= 0.0:
            out[sid] = v
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /tmp/wt-trade-output && nice -n 19 python3 -m pytest tests/test_regime_param_resolver.py -k size_scalars_for_regime -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd /tmp/wt-trade-output
git add src/execution/regime_param_resolver.py tests/test_regime_param_resolver.py
git commit -m "feat(resolver): size_scalars_for_regime raw batch read (missing→1.0, 0.0 honored)"
```

---

### Task 3: Sizer pure helpers — apply, shadow-diff, build-contributions

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (add module-level helpers near the other `_*` helpers, e.g. just above `def _sharpe_cadence_path`, ~line 795)
- Test: `tests/test_size_scalar_wiring.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_size_scalar_wiring.py`:
```python
"""Pure-helper tests for size_scalar wiring + signed contributions."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import regime_blended_sizer as rbs  # noqa: E402


def test_apply_size_scalars_off_is_identity():
    w = {'A': 2.0, 'B': 1.0}
    out = rbs._apply_size_scalars(w, {'A': 1.5}, gate_on=False)
    assert out == {'A': 2.0, 'B': 1.0}          # OFF → untouched


def test_apply_size_scalars_on_scales_present_defaults_missing_to_one():
    w = {'A': 2.0, 'B': 1.0, 'C': 4.0}
    out = rbs._apply_size_scalars(w, {'A': 1.5, 'B': 0.0}, gate_on=True)
    assert out['A'] == 3.0                       # 2.0 × 1.5
    assert out['B'] == 0.0                       # explicit mute honored
    assert out['C'] == 4.0                       # missing → ×1.0


def test_build_contributions_signed_sums_to_ticker_w():
    # AAPL: A long (eff 3.0), B short (eff 1.0) → contributions +3, -1; net +2
    strategies = ['A', 'B']
    directions = [1, -1]
    eff_weight = {'A': 3.0, 'B': 1.0}
    contribs = rbs._build_contributions(strategies, directions, eff_weight)
    assert contribs == [
        {'strategy_id': 'A', 'contribution': 3.0, 'direction': 1},
        {'strategy_id': 'B', 'contribution': -1.0, 'direction': -1},
    ]
    assert round(sum(c['contribution'] for c in contribs), 9) == 2.0


def test_build_contributions_skips_synthetic_and_unknown():
    strategies = ['__close_orphan__', 'A']
    directions = [0, 1]
    eff_weight = {'A': 2.0}                       # synthetic not in eff_weight
    contribs = rbs._build_contributions(strategies, directions, eff_weight)
    assert contribs == [{'strategy_id': 'A', 'contribution': 2.0, 'direction': 1}]


def test_scaled_target_diff_reports_per_ticker_dollar_delta():
    # survivors: AAPL = [(A,+1)], MSFT = [(B,+1)]; raw weights A=1,B=1 → 50/50
    # scalar A=3 → AAPL share 3/4, MSFT 1/4 of lam_nav=100.
    survivors = {'AAPL': [('A', 1)], 'MSFT': [('B', 1)]}
    weight_by_strat = {'A': 1.0, 'B': 1.0}
    scalars = {'A': 3.0}
    raw_targets = {'AAPL': 50.0, 'MSFT': 50.0}
    diff = rbs._scaled_target_diff(survivors, weight_by_strat, scalars,
                                   raw_targets, lam_nav=100.0)
    assert round(diff['AAPL']['scaled_usd'], 6) == 75.0
    assert round(diff['MSFT']['scaled_usd'], 6) == 25.0
    assert round(diff['AAPL']['delta_usd'], 6) == 25.0
    assert round(diff['MSFT']['delta_usd'], 6) == -25.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /tmp/wt-trade-output && nice -n 19 python3 -m pytest tests/test_size_scalar_wiring.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_apply_size_scalars'`

- [ ] **Step 3: Implement the helpers**

Insert into `src/execution/regime_blended_sizer.py` just above `def _sharpe_cadence_path(` (~line 795):
```python
def _apply_size_scalars(weight_by_strat: dict, size_scalars: dict, gate_on: bool) -> dict:
    """Allocation-only size_scalar application. When gate_on, multiply each
    strategy's daily_weight by its approved size_scalar (missing → 1.0, so a
    strategy absent from `size_scalars` is unchanged; an explicit 0.0 mutes).
    When OFF, return weights untouched (byte-identical routing)."""
    if not gate_on:
        return dict(weight_by_strat)
    out = {}
    for sid, w in weight_by_strat.items():
        s = size_scalars.get(sid, 1.0)
        out[sid] = w * s
    return out


def _build_contributions(strategies: list, directions: list, eff_weight_by_strat: dict) -> list:
    """Signed per-representative allocation terms for one ticker:
    contribution = eff_weight × direction (the exact value summed into ticker_w).
    Skips synthetic markers / strategies absent from eff_weight_by_strat."""
    out = []
    for sid, d in zip(strategies, directions):
        if sid not in eff_weight_by_strat:
            continue
        out.append({'strategy_id': sid,
                    'contribution': eff_weight_by_strat[sid] * d,
                    'direction': d})
    return out


def _scaled_target_diff(survivors: dict, weight_by_strat: dict, size_scalars: dict,
                        raw_targets: dict, lam_nav: float) -> dict:
    """SHADOW: what the per-ticker dollar targets WOULD be with size_scalars
    applied (allocation-only), vs the raw targets actually routed this cycle.
    `survivors` = {ticker: [(strategy_id, direction_int), ...]} (post-gate).
    Returns {ticker: {raw_usd, scaled_usd, delta_usd}}. Renormalises the scaled
    weights to the same lam_nav gross (mirrors the live scale = lam·NAV/Σ|w|)."""
    scaled_w = {tkr: sum(weight_by_strat.get(sid, 0.0) * size_scalars.get(sid, 1.0) * d
                         for sid, d in members)
                for tkr, members in survivors.items()}
    gross = sum(abs(w) for w in scaled_w.values())
    if gross <= 0:
        return {}
    scale = lam_nav / gross
    out = {}
    for tkr, w in scaled_w.items():
        scaled_usd = w * scale
        raw_usd = raw_targets.get(tkr, 0.0)
        out[tkr] = {'raw_usd': raw_usd, 'scaled_usd': scaled_usd,
                    'delta_usd': scaled_usd - raw_usd}
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /tmp/wt-trade-output && nice -n 19 python3 -m pytest tests/test_size_scalar_wiring.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd /tmp/wt-trade-output
git add src/execution/regime_blended_sizer.py tests/test_size_scalar_wiring.py
git commit -m "feat(sizer): pure helpers for size_scalar apply, shadow-diff, signed contributions"
```

---

### Task 4: Wire the helpers into `_sharpe_cadence_path`

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` — `_sharpe_cadence_path` (weights at ~828, ticker_w at ~928, brackets at ~939, after target_usd at ~1026, order build at ~1173-1193)

> No new unit test (the logic lives in the Task-3 helpers, already tested). Verified by a regression run of the existing sizer suite + a manual gate-OFF byte-identical check.

- [ ] **Step 1: Load scalars + compute eff_weight (after line 830, after `cadence_by_strat`)**

Insert:
```python
    # size_scalar wiring (default-OFF). Approved per-(strategy,regime) scalars
    # (raw; missing → 1.0) multiply daily_weight = the ALLOCATION term only;
    # the cum-sharpe gate (sharpe_by_strat) + fold representative stay raw.
    _size_scalar_on = os.environ.get('OPENCLAW_STRATEGY_SIZE_SCALAR') == '1'
    try:
        from execution import regime_param_resolver as _rpr
        # Always load (raw): the ON path applies them; the OFF path logs a shadow diff.
        _size_scalars = _rpr.size_scalars_for_regime(regime_state)
    except Exception as e:
        logger.warning('size_scalar: load failed (%s); treating all as 1.0', e)
        _size_scalars = {}
    eff_weight_by_strat = _apply_size_scalars(weight_by_strat, _size_scalars, _size_scalar_on)
```

- [ ] **Step 2: Use `eff_weight_by_strat` in the ticker_w sum ONLY**

At line 928 change `weight_by_strat[sid] * d` → `eff_weight_by_strat[sid] * d` (the allocation sum).
**Leave the bracket `'weight': weight_by_strat[sid],` tuple on RAW `weight_by_strat`** — the bracket-leader
(whose entry/stop/targets anchor the order) is chosen by raw conviction, not the dollar adjustment; scaling
it would silently change which strategy's technical levels are used. *(Revised post-review 2026-06-09: an
earlier draft scaled the bracket weight too; reverted.)*
(Leave line 929 `ticker_net_sharpe[tkr] += sharpe_by_strat.get(sid, 0.0) * d` UNCHANGED — gate stays raw.)
The membership guard at line 923 (`sid not in weight_by_strat`) stays keyed on `weight_by_strat` (eff has identical keys).

- [ ] **Step 3: Shadow-diff log when gate OFF (after `target_usd` is built, after line 1026)**

Insert:
```python
    # SHADOW: when the gate is OFF, log what size_scalars WOULD do to per-ticker
    # dollar targets without moving money (mirrors OPENCLAW_STRATEGY_ORTHO_SHADOW).
    if not _size_scalar_on and _size_scalars:
        try:
            survivors = {tkr: list(zip(meta['strategies'], meta['directions']))
                         for tkr, meta in ticker_meta.items()}
            diff = _scaled_target_diff(survivors, weight_by_strat, _size_scalars,
                                       target_usd, lam_nav=lam * nav)
            moved = {t: round(v['delta_usd'], 2) for t, v in diff.items()
                     if abs(v['delta_usd']) >= 1.0}
            logger.info('size_scalar.shadow: %d scalars active; per-ticker Δusd=%s',
                        len(_size_scalars), dict(sorted(moved.items())))
        except Exception as e:
            logger.warning('size_scalar.shadow failed (%s)', e)
```

- [ ] **Step 4: Attach signed contributions to each order (in the order dict, ~line 1190)**

Add a key to the `_order` dict alongside `'contributing_strategies'`:
```python
            'contributing_strategies': ticker_meta[tkr]['strategies'],
            'contributions':           _build_contributions(
                                           ticker_meta[tkr]['strategies'],
                                           ticker_meta[tkr]['directions'],
                                           eff_weight_by_strat),
```

- [ ] **Step 5: Regression — existing sizer suite still green**

Run:
```bash
cd /tmp/wt-trade-output
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2-) \
  nice -n 19 python3 -m pytest tests/test_regime_blended_sizer.py tests/test_orthogonalization_sizer.py tests/test_sizer_dust_floor.py -q
```
Expected: PASS (no regressions). If a test imports the path with the gate unset, behavior is unchanged (eff_weight == raw).

- [ ] **Step 6: Manual gate-OFF byte-identical + spot check**

Run (confirms the resolver path end-to-end and that OFF doesn't alter weights):
```bash
cd /tmp/wt-trade-output
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2-) \
  nice -n 19 python3 -c "from execution import regime_param_resolver as r; print('momentum_12_1/LOW_VOL =', r.size_scalars_for_regime('LOW_VOL').get('momentum_12_1'))"
```
Expected: `momentum_12_1/LOW_VOL = 1.5`

- [ ] **Step 7: Commit**

```bash
cd /tmp/wt-trade-output
git add src/execution/regime_blended_sizer.py
git commit -m "feat(sizer): wire size_scalar into ticker_w (alloc-only, shadow-gated) + signed contributions on orders"
```

---

### Task 5: Persist `contributions` JSONB

**Files:**
- Modify: `src/execution/regime_blended_sizer_live.py` — add `_collapse_contributions` (pure) near `_collapse_contributing` (~line 307); extend `_persist_contributing_strategies` (~line 324) to write the JSONB.
- Test: `tests/test_contributing_strategies_persist.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_contributing_strategies_persist.py`:
```python
def test_collapse_contributions_keeps_signed_terms_per_ticker():
    from execution import regime_blended_sizer_live as live
    orders = [
        {'ticker': 'AAPL', 'contributions': [
            {'strategy_id': 'A', 'contribution': 3.0, 'direction': 1},
            {'strategy_id': 'B', 'contribution': -1.0, 'direction': -1}]},
        {'ticker': 'MSFT', 'contributions': [
            {'strategy_id': 'C', 'contribution': 2.0, 'direction': 1}]},
        {'ticker': 'NVDA', 'contributions': []},          # orphan close → skipped
    ]
    out = live._collapse_contributions(orders)
    assert out['AAPL'] == [
        {'strategy_id': 'A', 'contribution': 3.0, 'direction': 1},
        {'strategy_id': 'B', 'contribution': -1.0, 'direction': -1}]
    assert out['MSFT'] == [{'strategy_id': 'C', 'contribution': 2.0, 'direction': 1}]
    assert 'NVDA' not in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /tmp/wt-trade-output && nice -n 19 python3 -m pytest tests/test_contributing_strategies_persist.py -k collapse_contributions -v`
Expected: FAIL — `AttributeError: ... has no attribute '_collapse_contributions'`

- [ ] **Step 3: Implement the pure helper + extend the writer**

Add near `_collapse_contributing` in `src/execution/regime_blended_sizer_live.py`:
```python
def _collapse_contributions(orders) -> dict:
    """Collapse sized orders → {ticker: [{strategy_id, contribution, direction}]}.
    Keeps only orders that carry a non-empty `contributions` list (sizing
    emissions; orphan/flip closes have none). Pure — DB upsert is separate."""
    by_ticker: dict = {}
    for o in orders:
        tk = o.get('ticker')
        contribs = o.get('contributions') or []
        if not tk or not contribs:
            continue
        by_ticker[tk] = contribs
    return by_ticker
```

In `_persist_contributing_strategies`, replace the per-ticker upsert loop so it also writes `contributions`:
```python
    by_ticker = _collapse_contributing(orders)
    contribs_by_ticker = _collapse_contributions(orders)
    if not by_ticker:
        return 0
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return 0
    import json as _json
    n = 0
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor()
        for tk, strats in by_ticker.items():
            contribs = contribs_by_ticker.get(tk)
            cur.execute(
                """
                INSERT INTO cycle_contributing_strategies
                    (run_date, ticker, strategies, contributions, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (run_date, ticker) DO UPDATE SET
                    strategies = EXCLUDED.strategies,
                    contributions = EXCLUDED.contributions,
                    updated_at = NOW()
                """,
                (run_date_str, tk, list(strats),
                 _json.dumps(contribs) if contribs is not None else None),
            )
            n += 1
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f'[regime_blended_sizer_live] persist contributing_strategies '
              f'failed (non-fatal): {e}')
    return n
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /tmp/wt-trade-output && nice -n 19 python3 -m pytest tests/test_contributing_strategies_persist.py -v`
Expected: PASS (existing + new test)

- [ ] **Step 5: Commit**

```bash
cd /tmp/wt-trade-output
git add src/execution/regime_blended_sizer_live.py tests/test_contributing_strategies_persist.py
git commit -m "feat(sizer-live): persist signed contributions JSONB per cycle"
```

---

### Task 6: Rewrite `/api/portfolio/ticker-alpha` endpoint

**Files:**
- Modify: `src/channels/api/server.js` — replace the endpoint body lines 414-599 (inside the `try`)

> The endpoint no longer computes historical IR/mean_pct. It returns the persisted signed `contributions` for the ticker's latest sizing cycle, plus a net. Entry/current/close prices are already on the client's position object (rendered from `/api/portfolio/positions`), so the endpoint does not re-fetch them.

- [ ] **Step 1: Replace the endpoint body**

Replace everything between `try {` (line 414) and the closing `} catch (err) {` (line 597) with:
```javascript
    const now = Date.now();
    const hit = _tickerAlphaCache.get(ticker);
    if (hit && now - hit.ts < _TICKER_ALPHA_TTL_MS) return res.json(hit.payload);

    // Read the latest cycle's SIGNED per-representative contributions for this
    // ticker (migration 134). Open positions → latest run_date; closed → the
    // last run_date that sized the ticker (same row, most-recent). Each entry:
    // {strategy_id, contribution = daily_weight × size_scalar × direction,
    //  direction}. Graceful fallback to the legacy strategies[] list (no values).
    const cr = await dbQuery(
      `SELECT strategies, contributions, run_date
         FROM cycle_contributing_strategies
        WHERE ticker = $1 ORDER BY run_date DESC LIMIT 1`, [ticker]);
    const row = cr.rows[0] || null;
    let contributions = [];
    if (row && Array.isArray(row.contributions)) {
      contributions = row.contributions
        .map(c => ({
          strategy_id: c.strategy_id,
          contribution: Number(c.contribution),
          direction: Number(c.direction),
        }))
        .filter(c => isFinite(c.contribution))
        .sort((a, b) => b.contribution - a.contribution);   // longs (green) top, shorts (red) bottom
    } else if (row && Array.isArray(row.strategies)) {
      // Back-compat: pre-migration-134 row with names only, no signed values.
      contributions = row.strategies.map(s => ({ strategy_id: s, contribution: null, direction: null }));
    }
    const net = contributions.reduce((s, c) => s + (c.contribution || 0), 0);
    const payload = {
      ticker,
      contributions,
      net,
      run_date: row ? row.run_date : null,
      has_values: contributions.some(c => c.contribution != null),
      computed_at: new Date().toISOString(),
    };
    if (_tickerAlphaCache.size >= _TICKER_ALPHA_CAP) {
      const oldestKey = _tickerAlphaCache.keys().next().value;
      _tickerAlphaCache.delete(oldestKey);
    }
    _tickerAlphaCache.set(ticker, { ts: now, payload });
    return res.json(payload);
```

- [ ] **Step 2: Node syntax check**

Run: `cd /tmp/wt-trade-output && node --check src/channels/api/server.js`
Expected: no output (exit 0)

- [ ] **Step 3: Commit**

```bash
cd /tmp/wt-trade-output
git add src/channels/api/server.js
git commit -m "feat(dashboard): ticker-alpha returns signed cycle contributions (drops historical IR)"
```

---

### Task 7: Rewrite the client modal renderer

**Files:**
- Modify: `src/channels/api/server.js` — `_buildAlphaBarsHtml(group)` (~line 6986) and its catch-fallback in `_fetchTickerAlpha` (~line 6974)

> Render signed contribution bars (long green/positive, short red/negative), centered on a zero line, scaled to max |contribution|, with a net total. Entry + current/close come from the `group` object (already loaded): the group builder computes `g.avg_entry` (weighted entry) and `g.price` (already resolved to current when open / close when closed via `priceKey`). The builder knows `isOpen` but does not store it — Step 1 adds `g.is_open = isOpen` so the modal can label "current" vs "close".

- [ ] **Step 1: Store the open/closed flag on the group (builder, ~line 6889)**

In the group-building function, where `isOpen` is in scope (next to `g.avg_days = ...`), add:
```javascript
    g.is_open = isOpen;
```

- [ ] **Step 2: Update the fetch-error fallback shape (line ~6974)**

Change the `.catch` fallback to the new shape:
```javascript
    .catch(_ => { delete _alphaInflight[ticker]; return { contributions: [], reason: 'fetch_error' }; });
```

- [ ] **Step 3: Replace `_buildAlphaBarsHtml`**

Replace the function body with:
```javascript
function _buildAlphaBarsHtml(group) {
  const alpha = _alphaCache[group.ticker];
  if (!alpha) {
    return \`<div class="alpha-bars empty">Loading contributions for <b>\${group.ticker}</b>…</div>\`;
  }
  const contributions = alpha.contributions || [];
  // Price header (entry + current/close) — from the already-loaded position.
  const isOpen = group.is_open !== false;
  const entry  = group.avg_entry != null ? parseFloat(group.avg_entry) : null;
  const nowPx  = group.price != null ? parseFloat(group.price) : null;  // already resolved: current if open, close if closed
  const pxFmt  = (v) => v == null || !isFinite(v) ? '—' : '$' + v.toFixed(2);
  const priceHdr = \`<div class="ab-prices">
      <span class="ab-px">entry <b>\${pxFmt(entry)}</b></span>
      <span class="ab-px">\${isOpen ? 'current' : 'close'} <b>\${pxFmt(nowPx)}</b></span>
    </div>\`;

  if (!contributions.length) {
    return \`<div class="alpha-bars">\${priceHdr}
      <div class="alpha-bars empty">No corr-gate contributors recorded for <b>\${group.ticker}</b> yet.</div>
    </div>\`;
  }
  const haveValues = contributions.some(c => c.contribution != null && isFinite(c.contribution));
  const maxAbs = haveValues
    ? (Math.max(...contributions.map(c => Math.abs(c.contribution || 0))) || 1)
    : 1;

  const rows = contributions.map(c => {
    const val   = (c.contribution != null && isFinite(c.contribution)) ? c.contribution : null;
    if (val == null) {
      return \`<div class="ab-row ab-unranked">
        <div class="ab-label">\${c.strategy_id}</div>
        <div class="ab-track muted"><div class="ab-zero"></div></div>
        <div class="ab-value muted">n/a</div>
      </div>\`;
    }
    const widthPct = (Math.abs(val) / maxAbs) * 50;
    const sign     = val >= 0 ? 'pos' : 'neg';
    const valCls   = val >= 0 ? 'pf-pnl-pos' : 'pf-pnl-neg';
    const dirNorm  = c.direction > 0 ? 'LONG' : (c.direction < 0 ? 'SHORT' : '');
    const valTxt   = (val >= 0 ? '+' : '') + val.toFixed(2);
    return \`<div class="ab-row">
      <div class="ab-label" title="\${c.strategy_id} · signed sharpe contribution">\${c.strategy_id}</div>
      <div class="ab-dir \${_dirCls(dirNorm)}">\${dirNorm}</div>
      <div class="ab-track">
        <div class="ab-zero"></div>
        <div class="ab-fill \${sign}" style="width:\${widthPct.toFixed(2)}%"></div>
      </div>
      <div class="ab-value \${valCls}">\${valTxt}</div>
    </div>\`;
  }).join('');

  const net = alpha.net;
  let badge = '';
  if (net != null && isFinite(net)) {
    const netTxt = (net >= 0 ? '+' : '') + Number(net).toFixed(2);
    const netCls = net >= 0 ? 'pf-pnl-pos' : 'pf-pnl-neg';
    badge = \`<span class="ab-badge">net = <span class="\${netCls}">\${netTxt}</span></span>\`;
  }
  return \`<div class="alpha-bars">
    \${priceHdr}
    <div class="ab-title"><span class="ab-ticker">\${group.ticker}</span>\${badge}</div>
    \${rows}
  </div>\`;
}
```

- [ ] **Step 4: Node syntax check**

Run: `cd /tmp/wt-trade-output && node --check src/channels/api/server.js`
Expected: no output (exit 0)

- [ ] **Step 5: Commit**

```bash
cd /tmp/wt-trade-output
git add src/channels/api/server.js
git commit -m "feat(dashboard): modal shows entry/current·close + signed contribution bars"
```

---

### Task 8: Fix the stale `strategy_weights.py` docstring

**Files:**
- Modify: `src/execution/strategy_weights.py:73-74` and `:98-100`

- [ ] **Step 1: Correct the comments**

At line 73-74, change `corroboration + position-recs govern sizing` → `corroboration governs gating; the per-(strategy,regime) size_scalar governs allocation when OPENCLAW_STRATEGY_SIZE_SCALAR=1 (applied downstream in the sizer, not baked here)`.

At line 98-100, replace `the weekly position-recommendations (own multiplier + stop changes), so an additional OUE-derived scaling was redundant.` → `the approved per-(strategy,regime) size_scalar (applied in the sizer when its gate is ON), so an additional OUE-derived scaling here was redundant.`

- [ ] **Step 2: Syntax check**

Run: `cd /tmp/wt-trade-output && nice -n 19 python3 -c "import ast,sys; ast.parse(open('src/execution/strategy_weights.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
cd /tmp/wt-trade-output
git add src/execution/strategy_weights.py
git commit -m "docs(strategy_weights): correct stale 'position-recs govern sizing' comment"
```

---

## Final verification (after all tasks)

- [ ] **Full new-test sweep + sizer regression (sequential)**

Run:
```bash
cd /tmp/wt-trade-output
POSTGRES_URI=$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2-) \
  nice -n 19 python3 -m pytest tests/test_size_scalar_wiring.py tests/test_regime_param_resolver.py \
    tests/test_contributing_strategies_persist.py tests/test_regime_blended_sizer.py -q
```
Expected: all PASS.

- [ ] **Node syntax**: `cd /tmp/wt-trade-output && node --check src/channels/api/server.js` → exit 0.

## Rollout notes (operator)
- Ships with `OPENCLAW_STRATEGY_SIZE_SCALAR` **unset/OFF** → live sizing byte-identical; the sizer logs `size_scalar.shadow: … per-ticker Δusd=…` each EOD cycle. Operator reviews a cycle or two, then sets `OPENCLAW_STRATEGY_SIZE_SCALAR=1` to activate the 25 approved scalars.
- Branch `feat/trade-output-accuracy`: operator owes merge + johnbot restart (already pending for this branch's earlier commits). Migration 134 is additive and already applied to the live DB by Task 1.
- Migration-number divergence (`130` differs across branches) is reconciled by the operator at merge.
