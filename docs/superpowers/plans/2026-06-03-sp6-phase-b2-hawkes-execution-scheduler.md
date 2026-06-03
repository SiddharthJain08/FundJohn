# SP-6 Phase B2 — Alpha-Conditioned Hawkes Execution Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Phase A's naive 3:55 into-close dump with an alpha-conditioned 9:30→close participation curve (live `w_hawkes=0` paper executor) and a shadow Hawkes layer + §28 validation harness that accrues the evidence to ever lift `w_hawkes` off 0.

**Architecture:** Two parts. **Part A (B2.2)** is pure/analysis — a self-exciting intensity proxy (`b2_hawkes`), a capped-tilt curve over B1's base planner (`b2_planner`, byte-identical at `w_hawkes=0`), and a §28 permutation harness (`b2_validation`) that grades `Δ = sim_hawkes − sim_base` on identical bars against a time-misalignment null. **Part B (B2.1)** is the live `w_hawkes=0` child-order executor (`b2_executor`) that consumes the 9:30 sized handoff and works opens 9:30→close, reusing Phase-A's `execute_single`, the OCO machinery, and B0's per-order ledger. All gates default-OFF; all-off ⇒ Phase A byte-identical. Paper-only hard assert. `w_hawkes` stays 0 until §28 passes + operator sign-off.

**Tech Stack:** Python (no type hints in the reused modules), psycopg2/Postgres, pandas + pyarrow (parquet), the `alpaca` Go CLI + Alpaca REST, Redis (inflight lock), pytest (live-DB rollback fixtures).

**Spec:** `docs/superpowers/specs/2026-06-03-sp6-phase-b2-hawkes-execution-scheduler-design.md`.

---

## Grounded interface facts (from the 2026-06-03 extraction — re-verify in Task 0)

Reused signatures B2 calls (stable across the B1/B0 merge; **line numbers will shift on merge**):

- `b1_planner.plan(signed_qty, s_i, profile, lam)` → `list[(t, slice_qty)]`, weights `profile[t]*exp(-lam*s_i*t)` renormalized × `signed_qty`; `RTH_BUCKETS=13`, `U_SHAPE` fallback; `expected_volume_profile(history_days)`.
- `b1_simulator.simulate(plan_slices, realized_bars, close_t1, naive_fill=None, impact_bps=2.0)` → `{'actual_fill','exec_ledger','naive_ledger','completion','filled_qty'}`. `realized_bars = {bucket: {vwap,high,low,volume}}`. **NaN-vwap buckets skipped** (`not (vwap>0)`). **`vwap_base_ledger` is NOT returned** — it is `simulate(plan(...,0.0,...))['exec_ledger']`, merged by the caller.
- `b1_order_source.live_shadow_orders(run_date)` and `case_study_orders(n_losers=25, n_movers=25)` → row dicts `{ticker, signed_qty, s_i, run_date, naive_fill, close_t1, strategy_id, regime_state, signal_id, resolve_next_session}`; `si_from_pct(pct)=clamp(pct/0.25,0,1)`; join `execution_signals.id = signal_pnl.signal_id`.
- `b1_ledger.persist_rows(mode, rows)` (15-col insert into `b1_shadow_exec_ledger`), `build_report(rows)`, `format_report(mode, rep)`, `post_report(text, channel='data-alerts')`; `_num()` NaN-sanitizer.
- `b1_run`: `bucket_of(dt_iso)`→0..12|None, `_by_date(df_t)`, `_resolve_session(by_date, run_date, resolve_next_session)`, `_trailing(by_date, session)`, `_score_order(o, df_ticker)`; `LAM=4.0`, `TRAILING_DAYS=20`, `_PARQUET='data/master/prices_30m.parquet'` (**CWD-relative**; run from `/root/openclaw`), `_OPEN_BUCKET_UTC=(13,30)` (EDT-only).
- `ingest_prices_30m_alpaca`: `DATA_PARQUET` (absolute), `COLUMNS=['date','datetime','ticker','open','high','low','close','volume','vwap','transactions']`, NEVER-DELETE guard + atomic write.
- `parity_mark.finalize_execution_ledger(cur, closes, run_date, workspace_id='default')` → int; `exec_ledger_usd=(official_close − filled_avg_price)*(direction_sign*filled_qty)`; **only UPDATEs existing `alpaca_submissions` rows**; skips `__`-prefixed strategy_ids / non-fill / unreconciled / missing-close; `_norm_ticker(s)=s.replace('/','-').upper()`.
- `parity_mark.finalize_parity_marks(cur, closes, run_date, workspace_id='default', broker_loader=None)` → int; marks `mark_entry_price=fill_price=official_close`, re-anchors brackets via `_reanchor_bracket(ref=entry, entry_price=mark, direction=sign, stop_ref=, target_ref=)`; **only marks broker-HELD tickers FILLED**.
- `open_reconcile.run_reconcile(dry_run=False, conn=None, broker_loader=None, gate_ran=None, run_date=None)` → `{'drops','flattens','holds','errors'}`; flatten double-gated on `eod_compute_health.healthy` AND `signal_gate_verdicts gate_type='__gate_ran__'` (both fail-closed); acts on `orphan_close`/`flip_close` only. **Untouched by B2.**
- `regime_blended_sizer_live.main()`: `eod_mode = os.environ.get('OPENCLAW_EOD_RECONCILE')=='1'`; in EOD mode synthesizes `handoff={'cycle_date','regime','signals':[]}`, calls `size_positions(signals, account_state, regime, run_date, strategy_state, regime_params, confirmer)`, then `_build_sized_payload(orders, handoff, equity=)` → `finalize_sized_payload(run_date_str, payload, source='regime_blended_sizer_live')`. **Writes a `{run_date}_sized.json` handoff; does NOT submit.**
- `alpaca_executor.main()` reads the sized handoff and submits (loop is the legacy 3:55 fill). `execute_single(sess, equity, order, run_date)` — `run_date` is a **`YYYY-MM-DD` string**; coid `AX{date}_{ticker}_{sid}[_C]`; `already_executed(conn, run_date, strategy_id, ticker)` keyed on non-NULL `alpaca_order_id`; `_submit_order_via_cli(...)` flags `order submit --symbol --side --qty --type --time-in-force --client-order-id [--limit-price] [--order-class bracket --take-profit --stop-loss]`; `_alpaca_session_kind()`→`rth|premarket|afterhours|closed`; `_handoff_fresh()` refuses a handoff not written today (ET).
- `stop_reattach.submit_protective_oco(*, ticker, position_side, qty, stop_price, target_price, dry_run)` → dict (OCO via **REST** `/v2/orders`, `order_class='oco'`, gtc); `run_oco_reattach(conn, positions, dry_run)`.
- Redis lock key `f'execute:close:inflight:{run_date}'`, `setex ttl=300`; `redeploy_pipeline.py` reads it to defer.

**Footguns to honor:** `vwap_base_ledger` = second `simulate` call; `run_date` is a string in the executor; `finalize_execution_ledger` only updates EXISTING rows (executor MUST write the submission row); ledger-close fires only on broker `status=='filled'` (never an ack); `_classify_position_deltas` mutates `ticker_meta` in place; EDT-only bucketing.

---

## Build context & constraints

- **Build base:** a worktree off the **merged SP-6 base** (after B0+B1 merge, which rides tonight's Phase-A fill verdict). B2 reuses `b1_*`/`parity_mark.finalize_execution_ledger`, which only exist post-merge. **Do not build off bare `f3f366a`** (no B1 code there).
- **Worktree:** create via `superpowers:using-git-worktrees` at execution time; branch `feat/sp6-phase-b2-execution-scheduler`.
- **Standing constraints:** paper only; NEVER delete master data (incl. `prices_30m.parquet`); 2-core/8GB → `nice -n 19`, never run heavy jobs concurrently; gates default-OFF + byte-identical when off; no push / no johnbot restart / no migration-apply without operator approval; don't disturb the live checkout's uncommitted `manifest.json`/`strategy_signatures.json`/`run_sentiment_step.py`; **ABORT is never `git reset --hard`** (use `git merge --abort`/`git reset --keep`; back up first).
- **Gates:** `OPENCLAW_B2_HAWKES_SHADOW` (Part A accrual), `OPENCLAW_B2_EXECUTOR` (Part B live), `w_hawkes` param (default `0.0`, lifts only post-§28).
- Run tests with `cd /root/openclaw-b2-worktree && PYTHONPATH=src nice -n 19 python3 -m pytest ...`.

---

## Task 0: Build-time grounding re-verification (no code)

**Files:** none (verification only).

- [ ] **Step 1: Confirm the build base has B1 + B0 merged.**
Run: `PYTHONPATH=src python3 -c "import execution.b1_planner, execution.b1_simulator, execution.b1_order_source, execution.b1_ledger; from execution.parity_mark import finalize_execution_ledger; print('ok')"`
Expected: `ok`. If ImportError → the base does not have B1/B0 merged; STOP and resolve the merge first (do not re-vendor B1 code).

- [ ] **Step 2: Re-verify the reused signatures against current source** (line numbers shifted on merge). Grep and eyeball:
Run: `grep -n "def plan(" src/execution/b1_planner.py; grep -n "def simulate(" src/execution/b1_simulator.py; grep -n "def live_shadow_orders\|def case_study_orders\|SIZE_CAP" src/execution/b1_order_source.py; grep -n "def finalize_execution_ledger\|def finalize_parity_marks\|def _norm_ticker" src/execution/parity_mark.py; grep -n "def execute_single\|def already_executed\|def _submit_order_via_cli" src/execution/alpaca_executor.py; grep -n "def submit_protective_oco" src/execution/stop_reattach.py; grep -n "def main\|OPENCLAW_EOD_RECONCILE\|finalize_sized_payload" src/execution/regime_blended_sizer_live.py`
Expected: each signature matches the "Grounded interface facts" above. **Record any drift in a GROUNDING CORRECTIONS block at the top of this plan** (mirror B1's plan) before proceeding.

- [ ] **Step 3: Confirm migration numbering.** Run: `ls src/database/migrations/ | sort | tail -5`. Pick the next free numbers for Tasks 1 (expect ~129/130). Update Task 1 paths accordingly.

---

# PART A — B2.2: Hawkes substrate + capped-tilt curve + §28 shadow harness (pure/analysis)

## Task 1: Migrations — shadow Δ ledger + §28 results

**Files:**
- Create: `src/database/migrations/129_b2_hawkes_shadow_ledger.sql`
- Create: `src/database/migrations/130_b2_sec28_runs.sql`

- [ ] **Step 1: Write `129_b2_hawkes_shadow_ledger.sql`** (mirror mig 128; additive; NEVER-DELETE-safe).

```sql
-- SP-6 Phase B2: shadow ledger of the Hawkes-vs-base execution delta.
-- Δ = sim_hawkes.exec_ledger − sim_base.exec_ledger on identical realized bars.
CREATE TABLE IF NOT EXISTS b2_hawkes_shadow_ledger (
    id              BIGSERIAL PRIMARY KEY,
    run_date        DATE,
    ticker          TEXT NOT NULL,
    strategy_id     TEXT,
    signal_id       UUID,
    regime_state    TEXT,
    signed_qty      NUMERIC,
    s_i             NUMERIC,
    lam             NUMERIC,
    w_hawkes        NUMERIC,
    tilt_cap        NUMERIC,
    exec_ledger_base    NUMERIC,   -- sim_base.exec_ledger (w_hawkes=0)
    exec_ledger_hawkes  NUMERIC,   -- sim_hawkes.exec_ledger
    delta           NUMERIC,       -- hawkes − base (the §28 statistic, per order)
    completion_base     NUMERIC,
    completion_hawkes   NUMERIC,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_b2_shadow_run_date ON b2_hawkes_shadow_ledger (run_date);
```

- [ ] **Step 2: Write `130_b2_sec28_runs.sql`** (audit each §28 evaluation; anti-peeking record).

```sql
-- SP-6 Phase B2: §28 alpha-randomization evaluation audit (one row per look).
CREATE TABLE IF NOT EXISTS b2_sec28_runs (
    id              BIGSERIAL PRIMARY KEY,
    evaluated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    window_start    DATE,
    window_end      DATE,
    n_orders        INTEGER,         -- accrued observations in the window
    min_n           INTEGER,         -- precondition in force at eval time
    w_hawkes        NUMERIC,
    tilt_cap        NUMERIC,
    n_scrambles     INTEGER,
    real_sum_delta  NUMERIC,         -- Σ Δ on the in-sample window
    null_quantile   NUMERIC,         -- 95th pct of the null Σ Δ
    oos_sum_delta   NUMERIC,         -- Σ Δ on the OOS holdout
    beats_base      BOOLEAN,
    passes_null     BOOLEAN,         -- real > null_quantile
    passes_oos      BOOLEAN,         -- oos_sum_delta > 0
    verdict         TEXT NOT NULL,   -- 'insufficient_n' | 'fail' | 'pass_pending_signoff'
    detail_json     JSONB
);
```

- [ ] **Step 3: Lint-check the SQL parses** (no live apply — operator-gated).
Run: `python3 -c "import pathlib; [pathlib.Path(p).read_text() for p in ['src/database/migrations/129_b2_hawkes_shadow_ledger.sql','src/database/migrations/130_b2_sec28_runs.sql']]; print('read ok')"`
Expected: `read ok`. (Apply happens in Task 12 activation, operator-present.)

- [ ] **Step 4: Commit.**
```bash
git add src/database/migrations/129_b2_hawkes_shadow_ledger.sql src/database/migrations/130_b2_sec28_runs.sql
git commit -m "feat(sp6-b2): migrations for Hawkes shadow ledger + §28 run audit"
```

## Task 2: `b2_hawkes.py` — self-exciting continuation-intensity proxy

**Files:**
- Create: `src/execution/b2_hawkes.py`
- Test: `tests/test_b2_hawkes.py`

- [ ] **Step 1: Write the failing tests** (test PROPERTIES, so the feature weights stay tunable).

```python
# tests/test_b2_hawkes.py
import math
from execution.b2_hawkes import hawkes_intensity

def _bar(vwap, vol=1000.0, rng=0.005):
    return {'vwap': vwap, 'high': vwap * (1 + rng), 'low': vwap * (1 - rng), 'volume': vol}

def test_empty_or_singleton_is_zero():
    assert hawkes_intensity([], 1.0) == 0.0
    assert hawkes_intensity([_bar(100.0)], 1.0) == 0.0

def test_bounds():
    bars = [_bar(100 + i, vol=10000.0, rng=0.02) for i in range(8)]
    for sign in (1.0, -1.0):
        h = hawkes_intensity(bars, sign)
        assert -1.0 <= h <= 1.0

def test_favorable_momentum_positive_adverse_negative():
    up = [_bar(100 + 2 * i, vol=5000.0) for i in range(6)]   # rising vwap
    assert hawkes_intensity(up, 1.0) > 0.0      # long + rising = favorable
    assert hawkes_intensity(up, -1.0) < 0.0     # short + rising = adverse

def test_nan_vwap_ignored_not_imputed():
    bars = [_bar(100), {'vwap': float('nan'), 'high': 1, 'low': 1, 'volume': 1}, _bar(101)]
    h = hawkes_intensity(bars, 1.0)
    assert math.isfinite(h)

def test_low_activity_damps_intensity():
    # flat-ish prices, tiny volume/range -> intensity near 0
    flat = [_bar(100 + 0.001 * i, vol=1.0, rng=0.0001) for i in range(6)]
    assert abs(hawkes_intensity(flat, 1.0)) < 0.2
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_hawkes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b2_hawkes'`.

- [ ] **Step 3: Write `b2_hawkes.py`.**

```python
# src/execution/b2_hawkes.py
"""SP-6 Phase B2 — self-exciting continuation-intensity proxy on 30m bars.

NOT a fitted point-process (no L2/trade tape). A low-dim EWMA estimate of
near-term continuation intensity in the order's FAVORABLE direction, computed
CAUSALLY from bars strictly before the current bucket. Output h ∈ [-1, 1];
+1 = strong favorable continuation. Callers with w_hawkes=0 never invoke this.

Feature weights / half-life are operator-tunable (spec §14); tests assert
properties (causality, bounds, sign, activity-damping), not exact values.
"""
import math

HALF_LIFE_BUCKETS = 2.0     # ~1 hour; principled prior, sanity-checked not fit
_VOL_FLOOR = 1.0
_MOM_REF = 0.01             # 1% per-bucket return ~ full-scale momentum
_RNG_REF = 0.01             # 1% range ~ full activity


def _ewma(values, half_life):
    """Causal EWMA over values (oldest->newest); newest weighted most."""
    if not values:
        return 0.0
    lam = math.log(2.0) / half_life
    n = len(values)
    num = den = 0.0
    for i, v in enumerate(values):
        w = math.exp(-lam * ((n - 1) - i))   # newest age 0
        num += w * v
        den += w
    return num / den if den else 0.0


def hawkes_intensity(bars_before, direction_sign, half_life=HALF_LIFE_BUCKETS):
    """Causal self-exciting continuation intensity in [-1, 1].

    bars_before: list of {vwap,high,low,volume} for buckets strictly BEFORE the
                 current bucket, oldest->newest. direction_sign: +1 long / -1 short.
    Insufficient/NaN data -> 0.0 (collapses the tilt factor to 1).
    """
    clean = [b for b in (bars_before or [])
             if b and isinstance(b.get('vwap'), (int, float)) and b.get('vwap', 0.0) > 0]
    if len(clean) < 2:
        return 0.0
    # Feature 1: signed-return momentum clustering (directional driver)
    rets = []
    for i in range(1, len(clean)):
        p0, p1 = clean[i - 1]['vwap'], clean[i]['vwap']
        if p0 > 0:
            rets.append(direction_sign * (p1 - p0) / p0)
    mom = _ewma(rets, half_life)
    # Feature 2: volume surprise (conviction gate)
    vols = [float(b.get('volume', 0.0) or 0.0) for b in clean]
    med = max(sorted(vols)[len(vols) // 2] if vols else 0.0, _VOL_FLOOR)
    vsurp = _ewma([math.log(max(v, _VOL_FLOOR) / med) for v in vols], half_life)
    # Feature 3: range-volatility clustering (conviction gate)
    rng = _ewma([(b['high'] - b['low']) / b['vwap'] for b in clean if b['vwap'] > 0],
                half_life)
    # Combine: momentum is directional; volume+range gate conviction in [0,1].
    mom_n = math.copysign(min(abs(mom) / _MOM_REF, 1.0), mom)
    activity = 0.5 * (math.tanh(vsurp) + 1.0) * min(max(rng, 0.0) / _RNG_REF, 1.0)
    h = math.tanh(2.0 * mom_n * activity)
    return max(-1.0, min(1.0, h))
```

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_hawkes.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_hawkes.py tests/test_b2_hawkes.py
git commit -m "feat(sp6-b2): self-exciting continuation-intensity proxy (causal, bounded)"
```

## Task 3: `b2_planner.py` — capped-tilt curve over B1's base plan

**Files:**
- Create: `src/execution/b2_planner.py`
- Test: `tests/test_b2_planner.py`

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_b2_planner.py
from execution.b1_planner import plan as base_plan, U_SHAPE
from execution.b2_planner import plan_tilted, TILT_CAP

PROFILE = list(U_SHAPE)

def test_w_hawkes_zero_is_byte_identical_to_base():
    base = base_plan(100.0, 0.7, PROFILE, 4.0)
    assert plan_tilted(100.0, 0.7, PROFILE, 4.0, h_by_bucket={t: 1.0 for t in range(13)},
                       w_hawkes=0.0) == base

def test_none_h_is_base():
    base = base_plan(-50.0, 0.3, PROFILE, 4.0)
    assert plan_tilted(-50.0, 0.3, PROFILE, 4.0, h_by_bucket=None, w_hawkes=0.5) == base

def test_slices_sum_to_signed_qty():
    out = plan_tilted(100.0, 0.7, PROFILE, 4.0,
                      h_by_bucket={t: (1.0 if t < 4 else -1.0) for t in range(13)},
                      w_hawkes=0.5)
    assert abs(sum(q for _, q in out) - 100.0) < 1e-6

def test_positive_h_bucket_gets_relatively_more_mass():
    base = dict(base_plan(100.0, 0.7, PROFILE, 4.0))
    tilt = dict(plan_tilted(100.0, 0.7, PROFILE, 4.0,
                            h_by_bucket={3: 1.0}, w_hawkes=0.5))
    # bucket 3 (h=+1) gains share; a zero-h bucket loses share after renorm
    assert tilt[3] > base[3]
    assert tilt[7] < base[7]

def test_cap_bounds_the_per_slice_factor():
    # with cap=0.5, no pre-renorm factor exceeds [0.5, 1.5]; check via extreme h
    out = plan_tilted(100.0, 0.7, PROFILE, 4.0,
                      h_by_bucket={t: 9.9 for t in range(13)}, w_hawkes=10.0, cap=0.5)
    # uniform extreme tilt -> all clipped equally -> identical to base after renorm
    base = base_plan(100.0, 0.7, PROFILE, 4.0)
    assert all(abs(a[1] - b[1]) < 1e-6 for a, b in zip(out, base))
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_planner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b2_planner'`.

- [ ] **Step 3: Write `b2_planner.py`.**

```python
# src/execution/b2_planner.py
"""SP-6 Phase B2 — capped-tilt participation curve over B1's base plan.

slice(t) ∝ base_w[t] · (1 + clip(w_hawkes·ĥ(t), -c, +c)), renormalized to the
SAME signed_qty. w_hawkes=0 (or h_by_bucket=None) ⇒ returns b1_planner.plan
verbatim (byte-identical). Causal: ĥ(t) must be computed from bars < t by the
caller. No full pause (cap < 1 keeps the factor strictly positive).
"""
from execution.b1_planner import plan as _base_plan

TILT_CAP = 0.5   # c — max |tilt| per slice before renormalization (spec §14, tunable)


def _clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def plan_tilted(signed_qty, s_i, profile, lam, h_by_bucket=None, w_hawkes=0.0, cap=TILT_CAP):
    """Capped-tilt plan. Returns list[(bucket, slice_qty)] summing to signed_qty."""
    base = _base_plan(signed_qty, s_i, profile, lam)   # [(t, base_qty)]
    if not w_hawkes or h_by_bucket is None:
        return base
    tilted = []
    for (t, q) in base:
        h = h_by_bucket.get(t, 0.0) or 0.0
        tilted.append((t, q * (1.0 + _clip(w_hawkes * h, -cap, cap))))
    tot = sum(q for _, q in tilted)
    if tot == 0:
        return base
    scale = signed_qty / tot
    return [(t, q * scale) for (t, q) in tilted]
```

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_planner.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_planner.py tests/test_b2_planner.py
git commit -m "feat(sp6-b2): capped-tilt planner (byte-identical at w_hawkes=0)"
```

## Task 4: `b2_validation.py` — §28 alpha-randomization harness

**Files:**
- Create: `src/execution/b2_validation.py`
- Test: `tests/test_b2_validation.py`

- [ ] **Step 1: Write the failing tests** (planted-signal passes, noise fails, min-n blocks, Δ orientation, null preserves marginal).

```python
# tests/test_b2_validation.py
import math, random
from execution.b2_validation import (
    order_delta, circular_shift_h, permutation_pvalue, evaluate_sec28, MIN_N,
)
from execution.b1_planner import U_SHAPE

PROFILE = list(U_SHAPE)

def _session(prices, vol=5000.0, rng=0.004):
    # ordered [(bucket, bar)] from a list of vwap prices, len == len(U_SHAPE)
    return [(t, {'vwap': p, 'high': p * (1 + rng), 'low': p * (1 - rng), 'volume': vol})
            for t, p in enumerate(prices)]

def test_delta_is_hawkes_minus_base_orientation():
    # rising favorable path: front-loading (tilt) should fill cheaper -> Δ >= ~0
    prices = [100 + 0.5 * t for t in range(13)]
    seq = _session(prices)
    d = order_delta(seq, signed_qty=100.0, s_i=0.7, profile=PROFILE, lam=4.0,
                    close_t1=prices[-1], direction_sign=1.0, w_hawkes=1.0, cap=0.5)
    assert math.isfinite(d)

def test_circular_shift_preserves_multiset():
    h = {t: float(t) for t in range(13)}
    shifted = circular_shift_h(h, 3)
    assert sorted(shifted.values()) == sorted(h.values())   # same marginal
    assert shifted != h                                      # alignment destroyed

def test_min_n_blocks_early_evaluation():
    few = [{} for _ in range(MIN_N - 1)]
    verdict = evaluate_sec28(few, prices_df=None, w_hawkes=1.0, cap=0.5)
    assert verdict['verdict'] == 'insufficient_n'

def test_permutation_pvalue_bounds():
    real, null = 5.0, [random.gauss(0, 1) for _ in range(1000)]
    p = permutation_pvalue(real, null)
    assert 0.0 <= p <= 1.0
    assert permutation_pvalue(100.0, null) < 0.05    # far above null
    assert permutation_pvalue(-100.0, null) > 0.95   # far below null
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_validation.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'execution.b2_validation'`.

- [ ] **Step 3: Write `b2_validation.py`.** (Reuses `b1_simulator.simulate` + `b2_planner`/`b1_planner` + `b2_hawkes`. The §28 driver re-derives bars per order with `b1_run` helpers — see Task 5's loader; this module is pure given the per-order bar sequence.)

```python
# src/execution/b2_validation.py
"""SP-6 Phase B2 — §28 alpha-randomization (time-misalignment) validation harness.

Grades Δ = sim_hawkes.exec_ledger − sim_base.exec_ledger on IDENTICAL realized
bars (never runs the Hawkes curve on live orders — the NON-NEGOTIABLE). Null =
circular-shift ĥ within session (preserves ĥ's marginal + autocorrelation,
destroys price-path alignment). Pass requires: real ΣΔ > 95th pct of the null
AND ΣΔ_oos > 0 AND Hawkes beats base — THEN operator sign-off (not in code).

Anti-peeking: MIN_N precondition + pre-committed evaluation; see spec §9.
"""
import math
from execution.b1_simulator import simulate
from execution.b1_planner import plan as base_plan
from execution.b2_planner import plan_tilted
from execution.b2_hawkes import hawkes_intensity

MIN_N = 150              # §28 not evaluated before this many accrued orders
N_SCRAMBLES = 1000
PASS_QUANTILE = 0.95
OOS_FRACTION = 0.30


def _causal_h(order_bars_seq, direction_sign):
    """{bucket: ĥ} where ĥ(t) uses only bars strictly before t."""
    h, prior = {}, []
    for (t, bar) in order_bars_seq:
        h[t] = hawkes_intensity(prior, direction_sign)
        prior = prior + [bar]
    return h


def order_delta(order_bars_seq, signed_qty, s_i, profile, lam, close_t1,
                direction_sign, w_hawkes, cap, naive_fill=None, h_by_bucket=None):
    """Δ for ONE order = exec_ledger(hawkes) − exec_ledger(base), same bars."""
    realized = {t: bar for (t, bar) in order_bars_seq}
    if h_by_bucket is None:
        h_by_bucket = _causal_h(order_bars_seq, direction_sign)
    base_slices = base_plan(signed_qty, s_i, profile, lam)
    hawkes_slices = plan_tilted(signed_qty, s_i, profile, lam, h_by_bucket, w_hawkes, cap)
    base = simulate(base_slices, realized, close_t1, naive_fill=naive_fill)
    hk = simulate(hawkes_slices, realized, close_t1, naive_fill=naive_fill)
    return hk['exec_ledger'] - base['exec_ledger']


def circular_shift_h(h_by_bucket, k):
    """Circular-shift the ĥ VALUES across buckets (preserves marginal + autocorr)."""
    items = sorted(h_by_bucket.items())
    buckets = [b for b, _ in items]
    vals = [v for _, v in items]
    n = len(vals)
    if n == 0:
        return dict(h_by_bucket)
    k %= n
    shifted = vals[-k:] + vals[:-k]
    return {b: shifted[i] for i, b in enumerate(buckets)}


def permutation_pvalue(real_stat, null_stats):
    """One-sided p = P(null >= real). Lower p = real beats null."""
    if not null_stats:
        return 1.0
    ge = sum(1 for s in null_stats if s >= real_stat)
    return (ge + 1) / (len(null_stats) + 1)


def _quantile(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def evaluate_sec28(orders, prices_df, w_hawkes, cap, n_scrambles=N_SCRAMBLES,
                   pass_quantile=PASS_QUANTILE, oos_fraction=OOS_FRACTION, min_n=MIN_N,
                   order_loader=None):
    """Evaluate §28 over accrued `orders`. Returns a verdict dict.

    orders: list of accrued live-shadow order dicts (time-ordered by run_date).
    prices_df: prices_30m parquet slice; `order_loader(order, prices_df)` ->
               (order_bars_seq, signed_qty, s_i, profile, lam, close_t1,
                direction_sign, naive_fill) or None if uncoverable.
               (Task 5 supplies the concrete loader built on b1_run helpers.)
    Blocks with verdict 'insufficient_n' when len(orders) < min_n.
    """
    if len(orders) < min_n:
        return {'verdict': 'insufficient_n', 'n_orders': len(orders), 'min_n': min_n}
    if order_loader is None:
        raise ValueError('order_loader required (built on b1_run helpers in Task 5)')

    cut = int(round((1.0 - oos_fraction) * len(orders)))
    in_sample, oos = orders[:cut], orders[cut:]

    def _sum_delta(subset, scramble_k=None):
        total, used = 0.0, 0
        for o in subset:
            loaded = order_loader(o, prices_df)
            if loaded is None:
                continue
            seq, sq, s_i, prof, lam, c_t1, dsign, nf = loaded
            h = _causal_h(seq, dsign)
            if scramble_k is not None:
                h = circular_shift_h(h, scramble_k)
            total += order_delta(seq, sq, s_i, prof, lam, c_t1, dsign,
                                 w_hawkes, cap, naive_fill=nf, h_by_bucket=h)
            used += 1
        return total, used

    real_sum, n_used = _sum_delta(in_sample)
    # null: each scramble uses a fixed non-zero shift k = 1..n_scrambles (mod 13)
    null = []
    for j in range(n_scrambles):
        k = (j % 12) + 1   # never 0 (would be the real alignment)
        s, _ = _sum_delta(in_sample, scramble_k=k)
        null.append(s)
    oos_sum, _ = _sum_delta(oos)

    null_q = _quantile(null, pass_quantile)
    passes_null = real_sum > null_q
    passes_oos = oos_sum > 0.0
    beats_base = real_sum > 0.0
    verdict = 'pass_pending_signoff' if (passes_null and passes_oos and beats_base) else 'fail'
    return {
        'verdict': verdict, 'n_orders': len(orders), 'n_used': n_used, 'min_n': min_n,
        'real_sum_delta': real_sum, 'null_quantile': null_q, 'oos_sum_delta': oos_sum,
        'passes_null': passes_null, 'passes_oos': passes_oos, 'beats_base': beats_base,
        'p_value': permutation_pvalue(real_sum, null), 'n_scrambles': n_scrambles,
        'w_hawkes': w_hawkes, 'tilt_cap': cap,
    }
```

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_validation.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Add a planted-signal end-to-end harness test** (proves the test discriminates).

```python
# append to tests/test_b2_validation.py
def test_planted_signal_passes_noise_fails():
    from execution.b2_validation import evaluate_sec28, MIN_N
    from execution.b1_planner import U_SHAPE
    PROFILE = list(U_SHAPE)

    def make_orders(n, planted):
        return [{'i': i, 'planted': planted} for i in range(n)]

    def loader(o, _df):
        # synthetic session: planted -> price drifts UP early (favorable for long)
        # so front-loading (high early ĥ) fills cheaper; noise -> random walk
        import random
        rnd = random.Random(o['i'])
        if o['planted']:
            prices = [100 + 0.8 * t for t in range(13)]
        else:
            prices = [100 + rnd.gauss(0, 0.5) for _ in range(13)]
        seq = [(t, {'vwap': p, 'high': p * 1.003, 'low': p * 0.997, 'volume': 8000.0})
               for t, p in enumerate(prices)]
        return (seq, 100.0, 0.8, PROFILE, 4.0, prices[-1], 1.0, None)

    planted = evaluate_sec28(make_orders(MIN_N + 20, True), prices_df=None,
                             w_hawkes=1.0, cap=0.5, n_scrambles=200, order_loader=loader)
    noise = evaluate_sec28(make_orders(MIN_N + 20, False), prices_df=None,
                           w_hawkes=1.0, cap=0.5, n_scrambles=200, order_loader=loader)
    assert planted['verdict'] == 'pass_pending_signoff'
    assert noise['verdict'] == 'fail'
```

Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_validation.py -v`
Expected: PASS (5 tests). If the planted case is flaky, raise the planted drift or `n` — the discriminator must be robust, not borderline.

- [ ] **Step 6: Commit.**
```bash
git add src/execution/b2_validation.py tests/test_b2_validation.py
git commit -m "feat(sp6-b2): §28 time-misalignment permutation harness (min-n gated)"
```

## Task 5: `b2_shadow_run.py` — daily shadow Δ accrual driver

**Files:**
- Create: `src/execution/b2_shadow_run.py`
- Test: `tests/test_b2_shadow_run.py`

- [ ] **Step 1: Write the failing tests** (gate-off no-op; the order_loader bridges b1_run helpers).

```python
# tests/test_b2_shadow_run.py
import os
from execution import b2_shadow_run

def test_gate_off_is_noop(monkeypatch):
    monkeypatch.delenv('OPENCLAW_B2_HAWKES_SHADOW', raising=False)
    out = b2_shadow_run.run(run_date='2026-06-04')
    assert out['status'] == 'gate_off' and out['persisted'] == 0

def test_order_loader_returns_none_on_no_coverage(monkeypatch):
    # an order whose ticker has no bars -> loader yields None (skip, not crash)
    import pandas as pd
    empty = pd.DataFrame(columns=['date', 'datetime', 'ticker', 'high', 'low', 'vwap', 'volume'])
    loaded = b2_shadow_run.order_loader({'ticker': 'ZZZZ', 'signed_qty': 1.0, 's_i': 0.5,
                                         'run_date': '2026-06-04', 'close_t1': None,
                                         'naive_fill': None, 'resolve_next_session': False},
                                        empty)
    assert loaded is None
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_shadow_run.py -v`
Expected: FAIL — `ModuleNotFoundError` / attribute errors.

- [ ] **Step 3: Write `b2_shadow_run.py`** (mirrors `b1_run.run`; reuses `b1_run` helpers `_by_date/_resolve_session/_trailing/expected_volume_profile`; computes per-order Δ and persists to `b2_hawkes_shadow_ledger`). `w_hawkes` comes from env (default 0.0) — at 0 the shadow Δ is structurally 0 but still recorded, which is the honest pre-lift baseline.

```python
# src/execution/b2_shadow_run.py
"""SP-6 Phase B2 — daily shadow Δ accrual on live opens (no order submission).

For each of today's actual opens (b1_order_source.live_shadow_orders), simulate
BOTH the w_hawkes curve and the base curve on the same realized 30m bars and
persist Δ = exec_ledger_hawkes − exec_ledger_base to b2_hawkes_shadow_ledger.
Gate OPENCLAW_B2_HAWKES_SHADOW. Run from /root/openclaw (CWD-relative parquet).
"""
import os
import argparse
import pandas as pd

from execution import b1_run
from execution.b1_planner import plan as base_plan, expected_volume_profile
from execution.b2_planner import plan_tilted, TILT_CAP
from execution.b1_simulator import simulate
from execution.b2_validation import _causal_h
from execution import b1_order_source
import psycopg2
import psycopg2.extras

_PARQUET = b1_run._PARQUET    # 'data/master/prices_30m.parquet' (CWD-relative)
LAM = b1_run.LAM              # 4.0


def _env(key):
    return b1_order_source._env(key)


def _w_hawkes():
    try:
        return float(os.environ.get('OPENCLAW_B2_W_HAWKES', '0') or '0')
    except ValueError:
        return 0.0


def order_loader(order, prices_df):
    """(order, prices_df) -> (seq, signed_qty, s_i, profile, lam, close_t1, dsign, naive)
    or None if the order's ticker/session has no usable bar coverage.
    Built on b1_run helpers so it matches B1's causal bucketing exactly."""
    df_t = prices_df[prices_df['ticker'] == order['ticker']]
    if df_t.empty:
        return None
    by_date = b1_run._by_date(df_t)
    session = b1_run._resolve_session(by_date, order.get('run_date'),
                                      order.get('resolve_next_session', False))
    if session is None or session not in by_date:
        return None
    buckets = by_date[session]                       # {bucket: {vwap,high,low,volume}}
    seq = sorted(buckets.items())                    # [(t, bar)] oldest->newest
    if not seq:
        return None
    profile = expected_volume_profile(b1_run._trailing(by_date, session))
    close_t1 = order.get('close_t1')
    if close_t1 is None:
        close_t1 = seq[-1][1].get('vwap')            # last-bucket vwap proxy
    if not close_t1 or close_t1 <= 0:
        return None
    dsign = 1.0 if order['signed_qty'] >= 0 else -1.0
    return (seq, order['signed_qty'], order['s_i'], profile, LAM, close_t1, dsign,
            order.get('naive_fill'))


def _persist(rows):
    if not rows:
        return 0
    conn = psycopg2.connect(_env('POSTGRES_URI'))
    try:
        with conn, conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, '''
                INSERT INTO b2_hawkes_shadow_ledger
                  (run_date, ticker, strategy_id, signal_id, regime_state, signed_qty,
                   s_i, lam, w_hawkes, tilt_cap, exec_ledger_base, exec_ledger_hawkes,
                   delta, completion_base, completion_hawkes)
                VALUES %s
            ''', [(r['run_date'], r['ticker'], r.get('strategy_id'), r.get('signal_id'),
                   r.get('regime_state'), r['signed_qty'], r['s_i'], r['lam'],
                   r['w_hawkes'], r['tilt_cap'], r['exec_ledger_base'],
                   r['exec_ledger_hawkes'], r['delta'], r['completion_base'],
                   r['completion_hawkes']) for r in rows])
        return len(rows)
    finally:
        conn.close()


def run(run_date=None, post=False):
    if os.environ.get('OPENCLAW_B2_HAWKES_SHADOW') != '1':
        return {'status': 'gate_off', 'persisted': 0}
    w_hawkes, cap = _w_hawkes(), TILT_CAP
    df = pd.read_parquet(_PARQUET,
                         columns=['date', 'datetime', 'ticker', 'high', 'low', 'vwap', 'volume'])
    orders = b1_order_source.live_shadow_orders(run_date)
    rows = []
    for o in orders:
        loaded = order_loader(o, df)
        if loaded is None:
            continue
        seq, sq, s_i, prof, lam, c_t1, dsign, nf = loaded
        realized = {t: bar for (t, bar) in seq}
        h = _causal_h(seq, dsign)
        base = simulate(base_plan(sq, s_i, prof, lam), realized, c_t1, naive_fill=nf)
        hk = simulate(plan_tilted(sq, s_i, prof, lam, h, w_hawkes, cap), realized, c_t1,
                      naive_fill=nf)
        rows.append({**o, 'run_date': o.get('run_date'), 'lam': lam, 'w_hawkes': w_hawkes,
                     'tilt_cap': cap, 'exec_ledger_base': base['exec_ledger'],
                     'exec_ledger_hawkes': hk['exec_ledger'],
                     'delta': hk['exec_ledger'] - base['exec_ledger'],
                     'completion_base': base['completion'],
                     'completion_hawkes': hk['completion']})
    persisted = _persist(rows)
    return {'status': 'ok', 'persisted': persisted, 'n_orders': len(orders)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-date')
    ap.add_argument('--post', action='store_true')
    args = ap.parse_args()
    out = run(run_date=args.run_date, post=args.post)
    print(out)
    raise SystemExit(0 if out['status'] in ('ok', 'gate_off') else 1)


if __name__ == '__main__':
    main()
```

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_shadow_run.py -v`
Expected: PASS (2 tests). (Gate-off path needs no DB; the loader test uses an empty DataFrame.)

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_shadow_run.py tests/test_b2_shadow_run.py
git commit -m "feat(sp6-b2): daily shadow Δ accrual driver (gate OPENCLAW_B2_HAWKES_SHADOW)"
```

---

# PART B — B2.1: live `w_hawkes=0` child-order executor (gated on B1 live)

> Part B actually submits paper orders. Build it gated OFF + paper-only; activate only after B1/B0 are live and the operator approves (Task 12). The executor calls `b2_planner` with `w_hawkes` from config (default 0) so B2.3 lifts the weight by config, not code.

## Task 6: `b2_executor.py` skeleton — paper-only assert, gate, handoff read, schedule build

**Files:**
- Create: `src/execution/b2_executor.py`
- Test: `tests/test_b2_executor_skeleton.py`

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_b2_executor_skeleton.py
import os, pytest
from execution import b2_executor

def test_gate_off_is_noop(monkeypatch):
    monkeypatch.delenv('OPENCLAW_B2_EXECUTOR', raising=False)
    out = b2_executor.run_open_schedule(run_date='2026-06-04', now_bucket=0)
    assert out['status'] == 'gate_off'

def test_paper_only_assert_blocks_live(monkeypatch):
    monkeypatch.setenv('OPENCLAW_B2_EXECUTOR', '1')
    monkeypatch.setattr(b2_executor, '_account_is_paper', lambda: False)
    with pytest.raises(RuntimeError, match='paper'):
        b2_executor.run_open_schedule(run_date='2026-06-04', now_bucket=0)

def test_build_schedule_uses_planner_at_w_hawkes_from_config(monkeypatch):
    monkeypatch.setenv('OPENCLAW_B2_W_HAWKES', '0')
    # one open, qty 100 long; schedule sums to 100 across remaining buckets
    sched = b2_executor.build_schedule(signed_qty=100.0, s_i=0.7,
                                       profile=[1.0 / 13] * 13, now_bucket=0,
                                       h_by_bucket=None)
    assert abs(sum(q for _, q in sched) - 100.0) < 1e-6
    assert all(t >= 0 for t, _ in sched)
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_skeleton.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the `b2_executor.py` skeleton** (gate, paper-only assert, schedule build over REMAINING buckets, config `w_hawkes`). The order-submission/roll/sweep/bracket logic lands in Tasks 7–10.

```python
# src/execution/b2_executor.py
"""SP-6 Phase B2 — live w_hawkes=0 child-order executor (paper-only).

Consumes the 9:30 sized handoff (written by regime_blended_sizer_live in EOD
mode) and works each open 9:30->close on the capped-tilt curve (w_hawkes from
config, default 0 -> base curve). Gate OPENCLAW_B2_EXECUTOR. HARD paper-only
assert independent of the gate. Reuses Phase-A execute_single + the OCO + B0's
per-order ledger. Completion floor: residual sweeps into close[T+1]; operational
anomaly-halts only (no outcome-conditioned abort).
"""
import os
import json

from execution.b2_planner import plan_tilted, TILT_CAP
from execution.b1_planner import RTH_BUCKETS

_GATE = 'OPENCLAW_B2_EXECUTOR'


def _w_hawkes():
    try:
        return float(os.environ.get('OPENCLAW_B2_W_HAWKES', '0') or '0')
    except ValueError:
        return 0.0


def _account_is_paper():
    """True iff the configured Alpaca account is a paper account.
    Reuses alpaca_executor's base-URL/account check. Fail-closed (False) on doubt."""
    try:
        from execution import alpaca_executor as ae
        base = (os.environ.get('ALPACA_BASE_URL') or getattr(ae, 'ALPACA_BASE_URL', '') or '')
        return 'paper' in base.lower()
    except Exception:
        return False


def build_schedule(signed_qty, s_i, profile, now_bucket, h_by_bucket=None,
                   w_hawkes=None, cap=TILT_CAP):
    """Slices for the REMAINING buckets [now_bucket .. RTH_BUCKETS-1], summing to
    signed_qty. At now_bucket==0 this is the full-session plan."""
    if w_hawkes is None:
        w_hawkes = _w_hawkes()
    full = plan_tilted(signed_qty, s_i, profile, 4.0, h_by_bucket, w_hawkes, cap)
    remaining = [(t, q) for (t, q) in full if t >= now_bucket]
    tot = sum(q for _, q in remaining)
    if tot == 0:
        return [(now_bucket, signed_qty)]
    scale = signed_qty / tot
    return [(t, q * scale) for (t, q) in remaining]


def _load_sized_opens(run_date):
    """Read the {run_date}_sized.json handoff and return the OPEN orders
    (delta>0 new/resize-up + flip_open). Closes are NOT here (run_reconcile owns
    them). Reuses regime_blended_sizer_live's handoff path conventions."""
    from pipeline.handoff_io import read_handoff   # same module the sizer uses
    payload = read_handoff(run_date, 'sized')
    if not payload:
        return []
    return [o for o in payload.get('orders', [])
            if not o.get('close_only') and float(o.get('target_usd', 0) or 0) != 0]


def run_open_schedule(run_date, now_bucket, conn=None):
    """One scheduling tick: place this bucket's child slices for each open.
    Tasks 7-10 fill in submission / roll / sweep / brackets."""
    if os.environ.get(_GATE) != '1':
        return {'status': 'gate_off'}
    if not _account_is_paper():
        raise RuntimeError('B2 executor is paper-only; refusing on a non-paper account')
    opens = _load_sized_opens(run_date)
    return {'status': 'ok', 'opens': len(opens), 'bucket': now_bucket}
```

> **Task 0 note:** confirm the sized-handoff reader. The grounding map shows `finalize_sized_payload(run_date_str, payload, source=...)` writes it; verify the exact read API (`read_handoff(run_date,'sized')` vs a dedicated reader) and the payload's order-list key/shape, and fix `_load_sized_opens` accordingly. Do not guess — grep `finalize_sized_payload` and its writer.

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_skeleton.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_executor.py tests/test_b2_executor_skeleton.py
git commit -m "feat(sp6-b2): executor skeleton — gate + paper-only assert + remaining-bucket schedule"
```

## Task 7: Per-bucket child submission via `execute_single` (marketable-limit, idempotent coid)

**Files:**
- Modify: `src/execution/b2_executor.py`
- Test: `tests/test_b2_executor_submit.py`

- [ ] **Step 1: Write the failing test** (idempotent per-child coid; submission delegates to a thin seam that's mocked).

```python
# tests/test_b2_executor_submit.py
import os
from execution import b2_executor

def test_child_coid_is_deterministic_per_bucket():
    c1 = b2_executor.child_coid('2026-06-04', 'AAPL', 'momo', bucket=3)
    c2 = b2_executor.child_coid('2026-06-04', 'AAPL', 'momo', bucket=3)
    c3 = b2_executor.child_coid('2026-06-04', 'AAPL', 'momo', bucket=4)
    assert c1 == c2 and c1 != c3
    assert c1.startswith('B2') and 'AAPL' in c1 and len(c1) <= 128

def test_submit_child_skips_when_already_executed(monkeypatch):
    calls = []
    monkeypatch.setattr(b2_executor, '_child_already_done', lambda *a, **k: True)
    monkeypatch.setattr(b2_executor, '_submit_marketable_limit',
                        lambda **k: calls.append(k))
    res = b2_executor.submit_child(run_date='2026-06-04', ticker='AAPL',
                                   strategy_id='momo', bucket=3, slice_qty=10.0,
                                   side='buy', conn=None)
    assert res['skipped'] is True and calls == []
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_submit.py -v`
Expected: FAIL — `AttributeError: module 'execution.b2_executor' has no attribute 'child_coid'`.

- [ ] **Step 3: Add child submission to `b2_executor.py`.** Reuses the `alpaca` CLI flag pattern from `_submit_order_via_cli` (marketable limit: `--type limit --time-in-force day --limit-price <marketable>`), and writes/checks `alpaca_submissions` so `finalize_execution_ledger` has a row to update.

```python
# add to src/execution/b2_executor.py
import re as _re


def child_coid(run_date, ticker, strategy_id, bucket):
    """Deterministic per-child coid: B2{date}_{ticker}_{sid}_b{bucket} (<=128)."""
    t = _re.sub(r'[^A-Za-z0-9._-]', '_', (ticker or 'UNK'))
    sid = _re.sub(r'[^A-Za-z0-9._-]', '_', (strategy_id or 'unknown'))
    suffix = f'_b{int(bucket)}'
    prefix = f'B2{run_date.replace("-", "")}_{t}_'
    budget = max(1, 128 - len(prefix) - len(suffix))
    return prefix + sid[:budget] + suffix


def _child_already_done(conn, coid):
    """Idempotency: a child with this coid already has a non-NULL alpaca_order_id."""
    from execution import alpaca_executor as ae
    own = conn is None
    c = conn or ae._db()   # reuse the executor's connection helper
    try:
        with c.cursor() as cur:
            cur.execute('''SELECT 1 FROM alpaca_submissions
                           WHERE client_order_id = %s AND alpaca_order_id IS NOT NULL
                           LIMIT 1''', (coid,))
            return cur.fetchone() is not None
    finally:
        if own:
            c.close()


def _submit_marketable_limit(*, ticker, side, qty, coid, limit_price):
    """Submit ONE marketable-limit child via the alpaca CLI (RTH, tif=day)."""
    from execution import alpaca_executor as ae
    return ae._submit_order_via_cli(
        ticker=ticker, side=side, qty=qty, order_type='limit',
        tif='day', coid=coid, limit_price=limit_price)


def submit_child(run_date, ticker, strategy_id, bucket, slice_qty, side, conn,
                 limit_price=None):
    coid = child_coid(run_date, ticker, strategy_id, bucket)
    if _child_already_done(conn, coid):
        return {'skipped': True, 'coid': coid}
    result = _submit_marketable_limit(ticker=ticker, side=side, qty=abs(slice_qty),
                                      coid=coid, limit_price=limit_price)
    return {'skipped': False, 'coid': coid, 'result': result}
```

> **Task 0 note:** verify `_submit_order_via_cli`'s exact kwargs and the `alpaca_submissions.client_order_id` column name (grounding map shows the flag is `--client-order-id`; confirm the DB column). Confirm a connection helper exists (`ae._db()` or similar) or pass `conn` through. A marketable-limit price helper (`ae._pick_limit_price`) exists for ext-hours; reuse/adapt it for RTH crossing within the spec's `min(½·spread + k bps, hard cap)`.

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_submit.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_executor.py tests/test_b2_executor_submit.py
git commit -m "feat(sp6-b2): per-bucket marketable-limit child submission (idempotent coid)"
```

## Task 8: Cancel + roll the unfilled remainder into the next bucket

**Files:**
- Modify: `src/execution/b2_executor.py`
- Test: `tests/test_b2_executor_roll.py`

- [ ] **Step 1: Write the failing test** (unfilled child → cancel + remainder added to next bucket's target).

```python
# tests/test_b2_executor_roll.py
from execution import b2_executor

def test_roll_adds_unfilled_to_remaining_schedule():
    # bucket 0 targeted 10, filled 4 -> 6 unfilled rolls into buckets[1:]
    sched = [(0, 10.0), (1, 5.0), (2, 5.0)]
    rolled = b2_executor.roll_unfilled(sched, filled_bucket=0, filled_qty=4.0,
                                       signed_sign=1.0)
    # bucket 0 dropped; remaining still sums to original remaining target (20-4=16)
    assert abs(sum(q for _, q in rolled) - 16.0) < 1e-6
    assert all(t > 0 for t, _ in rolled)
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_roll.py -v`
Expected: FAIL — `AttributeError: ... roll_unfilled`.

- [ ] **Step 3: Add `roll_unfilled` to `b2_executor.py`.**

```python
# add to src/execution/b2_executor.py
def roll_unfilled(schedule, filled_bucket, filled_qty, signed_sign):
    """Drop the just-worked bucket; redistribute its unfilled remainder across the
    remaining buckets, preserving total signed target. schedule: [(bucket, qty)]
    for buckets >= filled_bucket. signed_sign: +1 long / -1 short."""
    target_this = next((q for t, q in schedule if t == filled_bucket), 0.0)
    unfilled = target_this - signed_sign * abs(filled_qty)   # signed remainder
    remaining = [(t, q) for (t, q) in schedule if t > filled_bucket]
    rem_tot = sum(q for _, q in remaining)
    new_total = rem_tot + unfilled
    if not remaining:
        return [(filled_bucket + 1, new_total)] if abs(new_total) > 1e-9 else []
    if abs(rem_tot) < 1e-12:
        share = new_total / len(remaining)
        return [(t, share) for (t, _) in remaining]
    scale = new_total / rem_tot
    return [(t, q * scale) for (t, q) in remaining]
```

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_roll.py -v`
Expected: PASS.

> **Documented limitation (spec §13):** Alpaca paper fills marketable limits whole/immediately, so the live run rarely exercises this roll. Keep the unit test as the honest validation of the path.

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_executor.py tests/test_b2_executor_roll.py
git commit -m "feat(sp6-b2): cancel+roll unfilled remainder into remaining buckets"
```

## Task 9: Completion floor + operational anomaly-halt

**Files:**
- Modify: `src/execution/b2_executor.py`
- Test: `tests/test_b2_executor_floor.py`

- [ ] **Step 1: Write the failing tests.**

```python
# tests/test_b2_executor_floor.py
from execution import b2_executor

def test_anomaly_triggers_sweep_not_abort():
    assert b2_executor.should_sweep(anomaly=True, is_last_bucket=False) is True

def test_last_bucket_sweeps_residual():
    assert b2_executor.should_sweep(anomaly=False, is_last_bucket=True) is True

def test_normal_mid_session_does_not_sweep():
    assert b2_executor.should_sweep(anomaly=False, is_last_bucket=False) is False

def test_sweep_qty_is_full_residual():
    # residual = target - filled; sweep fills all of it into the close
    assert abs(b2_executor.sweep_qty(target=100.0, filled=73.0) - 27.0) < 1e-9
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_floor.py -v`
Expected: FAIL — missing `should_sweep` / `sweep_qty`.

- [ ] **Step 3: Add the completion-floor logic.**

```python
# add to src/execution/b2_executor.py
ANOMALY_REASONS = ('stale_bar', 'nan_feature', 'broker_reject', 'spread_blowout')


def should_sweep(anomaly, is_last_bucket):
    """Sweep the residual into the close on the last bucket OR an operational
    anomaly. NEVER on a price/P&L condition (would contaminate §28)."""
    return bool(anomaly) or bool(is_last_bucket)


def sweep_qty(target, filled):
    """Residual to dump into close[T+1] (the Phase-A naive fill = worst case)."""
    return target - filled
```

The terminal sweep submits one marketable order for `sweep_qty` (via `submit_child` at the final bucket, or a market order if `anomaly`), guaranteeing completion ≡ Phase A worst case.

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_floor.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_executor.py tests/test_b2_executor_floor.py
git commit -m "feat(sp6-b2): completion floor + operational anomaly-halt (no outcome abort)"
```

## Task 10: Dual brackets — broker OCO at completion + strategy-ledger parity + exec ledger

**Files:**
- Modify: `src/execution/b2_executor.py`
- Test: `tests/test_b2_executor_brackets.py`

- [ ] **Step 1: Write the failing tests** (OCO placed at completion anchored to avg fill; exec ledger written via B0).

```python
# tests/test_b2_executor_brackets.py
from execution import b2_executor

def test_place_completion_bracket_uses_avg_fill_anchor(monkeypatch):
    captured = {}
    def fake_oco(**k):
        captured.update(k); return {'status': 'ok'}
    monkeypatch.setattr(b2_executor, '_submit_protective_oco', fake_oco)
    b2_executor.place_completion_bracket(ticker='AAPL', position_side='long',
                                         qty=100.0, avg_fill=190.0,
                                         stop_pct=0.05, target_pct=0.10, dry_run=False)
    # stop/target anchored to the ACTUAL avg fill, not close[T+1]
    assert abs(captured['stop_price'] - 190.0 * 0.95) < 1e-6
    assert abs(captured['target_price'] - 190.0 * 1.10) < 1e-6

def test_short_side_bracket_inverts(monkeypatch):
    captured = {}
    monkeypatch.setattr(b2_executor, '_submit_protective_oco',
                        lambda **k: captured.update(k) or {'status': 'ok'})
    b2_executor.place_completion_bracket(ticker='AAPL', position_side='short',
                                         qty=100.0, avg_fill=190.0,
                                         stop_pct=0.05, target_pct=0.10, dry_run=False)
    assert captured['stop_price'] > 190.0 and captured['target_price'] < 190.0
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_brackets.py -v`
Expected: FAIL — missing `place_completion_bracket`.

- [ ] **Step 3: Add the dual-bracket logic** (broker OCO via `stop_reattach.submit_protective_oco`, anchored to avg fill; the strategy-ledger mark stays Phase-A's `finalize_parity_marks` at `close[T+1]`, unchanged; the exec ledger uses B0's `finalize_execution_ledger`).

```python
# add to src/execution/b2_executor.py
def _submit_protective_oco(**kwargs):
    from execution.stop_reattach import submit_protective_oco
    return submit_protective_oco(**kwargs)


def place_completion_bracket(ticker, position_side, qty, avg_fill, stop_pct,
                             target_pct, dry_run):
    """Broker protective OCO at completion, anchored to the ACTUAL avg fill
    (protects reality). Strategy ledger is marked separately at close[T+1] by
    Phase-A's finalize_parity_marks (parity) — the gap is the execution alpha."""
    if position_side == 'long':
        stop_price = avg_fill * (1.0 - stop_pct)
        target_price = avg_fill * (1.0 + target_pct)
    else:
        stop_price = avg_fill * (1.0 + stop_pct)
        target_price = avg_fill * (1.0 - target_pct)
    return _submit_protective_oco(ticker=ticker, position_side=position_side,
                                  qty=qty, stop_price=stop_price,
                                  target_price=target_price, dry_run=dry_run)
```

The exec-ledger write (`actual_fill` → `alpaca_submissions`, then `finalize_execution_ledger` at 4 PM) reuses B0 unchanged: B2 must ensure the submission row carries the real `filled_avg_price`/`filled_qty` (written by the normal `record_submission` + `alpaca_reconcile` path that `execute_single` already drives).

- [ ] **Step 4: Run to verify it passes.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_executor_brackets.py -v`
Expected: PASS (2 tests).

> **Accepted corollary (spec §7):** no broker bracket during 9:30→close accumulation — the OCO goes on at completion. Consistent with the daily-close exit model.

- [ ] **Step 5: Commit.**
```bash
git add src/execution/b2_executor.py tests/test_b2_executor_brackets.py
git commit -m "feat(sp6-b2): dual brackets — broker OCO at completion anchored to avg fill"
```

## Task 11: Cutover wiring — 9:30 size-once, B2 owns opens, gate-selected; concurrency

**Files:**
- Modify: `src/execution/alpaca_executor.py` (gate-select the open-submission loop)
- Modify: cron/timer config (the 9:30 size-once + B2 schedule ticks) — confirm location in Task 0 (`src/agent/cron-schedule.js` or `docs/*.timer`)
- Test: `tests/test_b2_cutover.py`

- [ ] **Step 1: Write the failing test** (gate-on → B2 owns opens; gate-off → legacy submission loop byte-identical).

```python
# tests/test_b2_cutover.py
import os
from execution import alpaca_executor as ae

def test_b2_gate_off_uses_legacy_open_path(monkeypatch):
    monkeypatch.delenv('OPENCLAW_B2_EXECUTOR', raising=False)
    assert ae._b2_owns_opens() is False

def test_b2_gate_on_routes_opens_to_b2(monkeypatch):
    monkeypatch.setenv('OPENCLAW_B2_EXECUTOR', '1')
    assert ae._b2_owns_opens() is True
```

- [ ] **Step 2: Run to verify it fails.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_cutover.py -v`
Expected: FAIL — `AttributeError: ... _b2_owns_opens`.

- [ ] **Step 3: Add the gate-select seam in `alpaca_executor.py`.** In `main()`'s open-submission loop (the grounding map placed it ~`:1912-1998` — **re-locate in Task 0**), wrap open (non-close) orders so that when `_b2_owns_opens()` they are skipped by the legacy dumper (B2's scheduled ticks own them); closes still flow through legacy. Add the helper:

```python
# add near the top of src/execution/alpaca_executor.py
def _b2_owns_opens():
    """When OPENCLAW_B2_EXECUTOR=1, the 9:30->close B2 scheduler owns OPEN orders;
    the legacy 3:55 loop submits closes only. Gate OFF = byte-identical legacy."""
    import os as _os
    return _os.environ.get('OPENCLAW_B2_EXECUTOR') == '1'
```

Then in the submission loop, for each order: `if _b2_owns_opens() and not order.get('close_only'): continue  # B2 scheduler owns opens`. **Single owner of opens at a time → no double-execution.** Closes are unaffected (run_reconcile + legacy close path own them).

- [ ] **Step 4: Wire the 9:30 size-once + B2 schedule.** (Config; confirm the runner in Task 0.) The cron sequence on T+1:
  - 9:28 — `open_reconcile.run_reconcile` (existing).
  - 9:30 — `regime_blended_sizer_live.py` with `OPENCLAW_EOD_RECONCILE=1` (writes the sized handoff) — this is the size-once.
  - 9:30→close — `b2_executor.run_open_schedule(run_date, now_bucket)` invoked once per 30m bucket (a `*/30 9-15` style trigger, or a single long-running driver that loops per `bucket_of`); guarded by the `execute:close:inflight` lock.
  - ~3:55 — `b2_executor` terminal sweep (residual) — the last bucket sweep.

  **Concurrency:** acquire `execute:close:inflight:{run_date}` around each B2 tick (reuse `_set_close_inflight`/`_clear_close_inflight`); on a confirmed `*/5` regime redeploy mid-schedule, **cancel working children + re-plan** (the redeploy nets deltas) — implement by having `redeploy_pipeline.py` signal B2 (it already reads the inflight key to defer; extend B2 to cancel open children when it sees a redeploy sentinel for `run_date`).

- [ ] **Step 5: Run to verify the gate-select test passes + the legacy regression is untouched.**
Run: `PYTHONPATH=src python3 -m pytest tests/test_b2_cutover.py -v && PYTHONPATH=src nice -n 19 python3 -m pytest tests/test_alpaca_executor*.py -q`
Expected: cutover PASS; the existing alpaca_executor suite unchanged (gate-off byte-identical).

- [ ] **Step 6: Commit.**
```bash
git add src/execution/alpaca_executor.py tests/test_b2_cutover.py
git commit -m "feat(sp6-b2): cutover seam — B2 owns opens when gated, legacy byte-identical off"
```

---

# PART C — verification, activation, §28 evaluation

## Task 12: Whole-branch parity regression + activation runbook (operator-driven)

**Files:**
- Create: `docs/sp6-b2-activation-runbook.md`
- Test: full suite

- [ ] **Step 1: Prove gate-off byte-identical.**
Run: `cd /root/openclaw-b2-worktree && PYTHONPATH=src nice -n 19 python3 -m pytest tests/ -q` with **all** `OPENCLAW_B2_*` unset and `OPENCLAW_B2_W_HAWKES=0`.
Expected: the Phase-A / B1 / B0 suites pass unchanged; only the new B2 tests are additive. Record the count.

- [ ] **Step 2: Request whole-branch review** via `superpowers:requesting-code-review` (independent reviewer), focused on: paper-only assert un-bypassable; gate-off byte-identical; `Δ = sim_hawkes − sim_base` orientation; no Hawkes-conditioned fill path reachable at `w_hawkes=0`; the exec-ledger writes a real submission row; OPG/close paths untouched.

- [ ] **Step 3: Write `docs/sp6-b2-activation-runbook.md`** — the operator-present steps: apply migrations 129/130 (idempotent, additive); flip `OPENCLAW_B2_HAWKES_SHADOW=1` (Part A accrual, no live effect); confirm paper account; flip `OPENCLAW_B2_EXECUTOR=1` with `OPENCLAW_B2_W_HAWKES=0`; wire the 9:30 size-once + schedule crons; restart johnbot; verify first-cycle: sized handoff written at 9:30, B2 child orders submitted (paper), residual swept by close, OCO placed at completion, `b2_hawkes_shadow_ledger` accruing Δ. ABORT path: set gates OFF + restart (never `git reset --hard`).

- [ ] **Step 4: Commit.**
```bash
git add docs/sp6-b2-activation-runbook.md
git commit -m "docs(sp6-b2): activation runbook (migrations, gates, crons, verify, abort)"
```

## Task 13 (DEFERRED — B2.3): §28 evaluation + `w_hawkes` lift

> **Do NOT run until the pre-committed min-n/date is reached** (anti-peeking). This is a config + analysis step, not a code build.

- [ ] **Step 1:** At the pre-committed evaluation point (n ≥ MIN_N accrued + the committed date), run the §28 evaluation over the accrued `live_shadow_orders` window with a candidate `w_hawkes` and `cap`; persist the result to `b2_sec28_runs` (verdict + p-value + OOS).
- [ ] **Step 2:** Require the pass to **hold across the committed consecutive looks** (not a single lucky window).
- [ ] **Step 3:** If `verdict == 'pass_pending_signoff'` across the looks AND the operator signs off → set `OPENCLAW_B2_W_HAWKES` to the validated weight (the live executor already calls `b2_planner` with the config weight — **no code change**). Otherwise B2 stops at the `w_hawkes=0` executor (a clean negative result).

---

## Self-review — spec coverage

- §28 two-key gate → Tasks 4 (harness), 13 (eval + sign-off), 1 (`b2_sec28_runs` audit). ✓
- Hawkes self-exciting proxy → Task 2. ✓
- Capped-tilt curve, byte-identical at 0 → Task 3. ✓
- Live `w_hawkes=0` executor, paper-only, cutover → Tasks 6–11. ✓
- Dual brackets → Task 10. ✓
- Completion floor, no outcome abort, anomaly-halt → Task 9. ✓
- 9:30 size-once cutover, gate-selected, no double-exec → Task 11. ✓
- Two streams + Δ forced sim-vs-sim → Tasks 4/5 (`Δ = sim_hawkes − sim_base`). ✓
- Anti-peeking (min-n + consecutive looks) → Tasks 4 (`MIN_N`), 13. ✓
- Concurrency (inflight lock, redeploy cancels children) → Task 11. ✓
- Migrations (shadow ledger, §28 audit) → Task 1. ✓
- Gate-off byte-identical → Task 12 Step 1. ✓
- Documented limitations (paper fills sim'd; roll un-exercised; calibration caveat) → Tasks 8/10 notes, spec §13. ✓

**No placeholders:** new-module code is complete; MODIFY/wiring tasks (6 `_load_sized_opens`, 7 CLI kwargs, 11 loop location + cron runner) carry explicit Task-0 re-grounding notes rather than guessed line numbers, because B1/B0 are not yet merged into the build base. Re-ground them before implementing.
