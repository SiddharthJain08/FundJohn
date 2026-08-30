# Beta-budget sizing — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rule C's removed conviction is redirected to the benchmark ticker (conviction conserved), the benchmark is capped at `benchmark_max_nav_frac·NAV`, everything ships behind `OPENCLAW_BENCH_BETA_BUDGET` (unset = shadow), and a daily `bench_realized` line makes book-vs-SPY visible.

**Architecture:** One new pure function (`apply_beta_budget`) beside the existing hurdle in `src/execution/benchmark_sizing.py`; the sizer's rule-C block chooses `_budgeted` over `_hurdled` when both flags are on and a qualified benchmark ticker exists; a NAV cap after the cluster cap; the shadow line grows three fields; the replay gains `--beta-budget`; a new `src/execution/bench_realized.py` computes book-vs-SPY from `logs/pnl_daily_ohlc.json` + SPY closes and `send_report` appends it to the #trade-reports digest.

**Tech Stack:** Python 3 (psycopg2, pyarrow), pytest; no new dependencies.

**Spec:** `docs/specs/2026-08-30-beta-budget-sizing-spec.md` (D-1..D-6). Base: `docs/specs/2026-08-29-benchmark-relative-sizing-spec.md` §2.5, amendment 1 `docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md`.

## Global Constraints

- Production tree is `/root/openclaw` on `main`; work there (the operator authorised main; no worktree).
- Never stage `src/strategies/manifest.json` (pipeline-dirty), `src/strategies/registry.py`, `src/strategies/strategy_signatures*.json`, `src/engine/daily-health-digest.js`, or new `S_g3m_*`/`S_lead_lag_*`/`S_holiday_*`/`S_leadlag_cluster_*` files.
- Never `source .env`; read a key with `grep -E '^KEY=' .env | cut -d= -f2-`.
- Tests reach the REAL DB when `POSTGRES_URI` is set: run only the named test files, never the full suite. Flag tests must `setenv(..., '0')`, never `delenv` (dotenv re-populates).
- Flags: `OPENCLAW_BENCH_RELATIVE_SIZING` (`bz.BENCH_SIZING_ENV`) and new `OPENCLAW_BENCH_BETA_BUDGET` (`bz.BETA_BUDGET_ENV`). Both unset/`'0'` = byte-identical sizing to today. The budget NEVER applies unless rule C applies (`_apply_hurdle`).
- Fail-open: every new sizer branch lives inside rule C's existing `try`; any exception → sized on raw `S_adj` with the existing WARN.
- No heavy compute 13:00–20:15 UTC on weekdays; no johnbot restart in this plan (flags stay unset; code is picked up by the next timer-spawned `trade` step — the sizer runs as a script, not inside johnbot).
- Commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01WZFQWB6mxLKnWSod6mPEJT`. Never `git reset --hard`.
- `pipeline_config` keys added here: `benchmark_max_nav_frac` (default `'1.0'`), `bench_realized_anchor` (default `'2026-06-23'`); both seeded `ON CONFLICT DO NOTHING` in migration `152`.

---

### Task 1: `apply_beta_budget`, flag reader, NAV-cap reader (pure, `benchmark_sizing.py`)

**Files:**
- Modify: `src/execution/benchmark_sizing.py` (after `apply_benchmark_hurdle`, ~:66; constants next to `BENCH_SIZING_ENV` ~:34)
- Test: `tests/execution/test_benchmark_sizing.py`

**Interfaces:**
- Produces: `BETA_BUDGET_ENV = 'OPENCLAW_BENCH_BETA_BUDGET'`; `beta_budget_enabled() -> bool`; `MAX_NAV_FRAC_KEY = 'benchmark_max_nav_frac'`; `benchmark_max_nav_frac(default=1.0, conn=None) -> float`; `apply_beta_budget(before: dict, hurdled: dict, s_m, bench_tickers: set) -> tuple[dict, float]`.

- [ ] **Step 1: Write the failing tests** — append to `tests/execution/test_benchmark_sizing.py`:

```python
# ── apply_beta_budget (spec 2026-08-30 §3.1) ────────────────────────────────
def test_beta_budget_conserves_conviction_and_redirects_to_benchmark():
    import pytest
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5, 'ZZTC': -2.5, 'ZZTD': -1.0}
    hurdled, dropped = bz.apply_benchmark_hurdle(before, 2.0, {'SPY'})
    out, pool = bz.apply_beta_budget(before, hurdled, 2.0, {'SPY'})
    # survivors hand exactly S_m, dropped hand their whole |S|: 2.0 + 1.5 + 2.0 + 1.0
    assert pool == pytest.approx(6.5)
    assert out['SPY'] == pytest.approx(2.0 + 6.5)          # own raw weight + pool
    assert out['ZZTA'] == pytest.approx(0.6) and out['ZZTC'] == pytest.approx(-0.5)
    assert 'ZZTB' not in out and 'ZZTD' not in out
    assert sum(abs(v) for v in out.values()) == pytest.approx(sum(abs(v) for v in before.values()))


def test_beta_budget_none_s_m_or_no_bench_is_identity():
    before = {'SPY': 2.0, 'ZZTA': 2.6}
    hurdled, _ = bz.apply_benchmark_hurdle(before, None, {'SPY'})
    out, pool = bz.apply_beta_budget(before, hurdled, None, {'SPY'})
    assert out == hurdled and out is not hurdled and pool == 0.0
    hurdled2, _ = bz.apply_benchmark_hurdle(before, 2.0, set())
    out2, pool2 = bz.apply_beta_budget(before, hurdled2, 2.0, set())
    assert out2 == hurdled2 and pool2 == 0.0


def test_beta_budget_splits_pool_across_benchmark_tickers_and_keeps_inputs():
    import pytest
    before = {'SPY': 2.0, 'IVV': 1.0, 'ZZTA': 3.0}
    hurdled, _ = bz.apply_benchmark_hurdle(before, 1.0, {'SPY', 'IVV'})
    snap_b, snap_h = dict(before), dict(hurdled)
    out, pool = bz.apply_beta_budget(before, hurdled, 1.0, {'SPY', 'IVV'})
    assert pool == pytest.approx(1.0)
    assert out['SPY'] == pytest.approx(2.5) and out['IVV'] == pytest.approx(1.5)
    assert before == snap_b and hurdled == snap_h


def test_beta_budget_flag_and_nav_frac_reader(monkeypatch):
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '0'); assert bz.beta_budget_enabled() is False
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '1'); assert bz.beta_budget_enabled() is True
    store = {}
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 1.0          # unset -> default
    store[bz.MAX_NAV_FRAC_KEY] = '0.5'
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 0.5
    store[bz.MAX_NAV_FRAC_KEY] = 'garbage'
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 1.0          # garbage -> default
    store[bz.MAX_NAV_FRAC_KEY] = '-2'
    assert bz.benchmark_max_nav_frac(conn=_Conn(store)) == 1.0          # non-positive -> default
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing.py -q -k "beta_budget"`
Expected: FAIL with `AttributeError: module 'execution.benchmark_sizing' has no attribute 'apply_beta_budget'` (and `BETA_BUDGET_ENV`).

- [ ] **Step 3: Implement** in `src/execution/benchmark_sizing.py`.

Next to `BENCH_SIZING_ENV` add:

```python
BETA_BUDGET_ENV = 'OPENCLAW_BENCH_BETA_BUDGET'   # spec 2026-08-30: '1' = redirect rule C's removed conviction to the benchmark
MAX_NAV_FRAC_KEY = 'benchmark_max_nav_frac'       # pipeline_config: benchmark ticker |target| <= frac * NAV under the budget (D-4)
```

Next to `bench_relative_sizing_enabled` add:

```python
def beta_budget_enabled() -> bool:
    return os.environ.get(BETA_BUDGET_ENV) == '1'


def benchmark_max_nav_frac(default: float = 1.0, conn=None) -> float:
    """pipeline_config.benchmark_max_nav_frac as a positive float; anything
    missing/garbage/non-positive -> default (logged). Own connection when
    conn is None."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (MAX_NAV_FRAC_KEY,))
            row = cur.fetchone()
        if not row or row[0] is None:
            return float(default)
        v = float(str(row[0]).strip())
        if not math.isfinite(v) or v <= 0:
            logger.warning('[bench_sizing] %s=%r not positive; using %s', MAX_NAV_FRAC_KEY, row[0], default)
            return float(default)
        return v
    except Exception as e:
        logger.warning('[bench_sizing] %s unreadable (%s: %s); using %s', MAX_NAV_FRAC_KEY, type(e).__name__, e, default)
        return float(default)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
```

After `apply_benchmark_hurdle` add:

```python
def apply_beta_budget(before: dict, hurdled: dict, s_m, bench_tickers: set) -> tuple[dict, float]:
    """Pure (spec 2026-08-30 §3.1). Redirect the conviction rule C removed to
    the benchmark tickers so Σ|w| is conserved: every alpha ticker hands
    min(|S_i|, S_m) to the pool (a survivor exactly S_m, a dropped ticker its
    whole |S_i|; shorts too, D-2); the pool is split equally across
    bench_tickers (D-3) on top of their own raw weight.
    before  = the ticker_w handed to apply_benchmark_hurdle
    hurdled = its first return value
    Returns (budgeted_weights, pool). s_m None or no bench_tickers ->
    (dict(hurdled), 0.0). Never mutates its inputs."""
    if s_m is None or not bench_tickers:
        return dict(hurdled), 0.0
    s_m = float(s_m)
    pool = sum(min(abs(float(s)), s_m) for t, s in before.items() if t not in bench_tickers)
    out = dict(hurdled)
    share = pool / len(bench_tickers)
    for b in bench_tickers:
        out[b] = out.get(b, 0.0) + share
    return out, pool
```

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing.py -q`
Expected: all PASS (existing 10 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/execution/benchmark_sizing.py tests/execution/test_benchmark_sizing.py
git commit -m "feat(bench): apply_beta_budget — rule C's removed conviction redirected to the benchmark ticker (spec 2026-08-30 §3.1), budget flag + NAV-frac reader"
```

---

### Task 2: `shadow_line` grows the budget fields

**Files:**
- Modify: `src/execution/benchmark_sizing.py::shadow_line` (~:188)
- Test: `tests/execution/test_benchmark_sizing.py`

**Interfaces:**
- Consumes: `apply_beta_budget` (Task 1).
- Produces: `shadow_line(..., h=None, budgeted: dict | None = None, beta_pool: float = 0.0, budget_mode: str = 'shadow')` — existing positional/keyword args unchanged; when `budgeted is None` the line is byte-identical to today.

- [ ] **Step 1: Write the failing test** — append to `tests/execution/test_benchmark_sizing.py`:

```python
def test_shadow_line_reports_beta_budget_fields():
    before = {'SPY': 2.0, 'ZZTA': 2.6, 'ZZTB': 1.5}
    after, dropped = bz.apply_benchmark_hurdle(before, 2.0, {'SPY'})
    budgeted, pool = bz.apply_beta_budget(before, after, 2.0, {'SPY'})
    line = bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, lam_nav=100_000.0, h=1,
                          budgeted=budgeted, beta_pool=pool, budget_mode='shadow')
    # pool = 2.0 (ZZTA) + 1.5 (ZZTB) = 3.5; budgeted SPY = 5.5 of Σ 6.1
    assert ' beta_budget=shadow pool=3.5 beta_share_budget=0.902 beta_usd_budget=90164 ' in line + ' '
    assert bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, 100_000.0, h=1,
                          budgeted=budgeted, beta_pool=pool, budget_mode='apply').count('beta_budget=apply') == 1
    # budgeted omitted -> byte-identical to the pre-budget format
    assert 'beta_budget=' not in bz.shadow_line('LOW_VOL', 2.0, before, after, dropped, {'SPY'}, 100_000.0, h=1)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing.py -q -k beta_budget_fields`
Expected: FAIL with `TypeError: shadow_line() got an unexpected keyword argument 'budgeted'`.

- [ ] **Step 3: Implement** — replace the `shadow_line` signature and return:

```python
def shadow_line(regime_state: str, s_m, before: dict, after: dict, dropped: list,
                bench_tickers: set, lam_nav: float, *, mode: str = 'shadow',
                h: int | None = None, budgeted: dict | None = None,
                beta_pool: float = 0.0, budget_mode: str = 'shadow') -> str:
    """One line per cycle. Dollar moves are computed by normalizing BOTH books
    to lam_nav (the sizer's Σ|target| = λ·NAV rule) so the diff is in the units
    the book will actually move. `budgeted` (spec 2026-08-30 §3.3) appends the
    beta-budget fields; omitted -> byte-identical to the pre-budget format."""
    g0, beta0 = _shares(before, bench_tickers)
    g1, beta1 = _shares(after, bench_tickers)
    usd0 = {t: (v / g0) * lam_nav for t, v in before.items()} if g0 > 0 else {}
    usd1 = {t: (v / g1) * lam_nav for t, v in after.items()} if g1 > 0 else {}
    moves = sorted(((t, round(usd1.get(t, 0.0) - usd0.get(t, 0.0), 2)) for t in set(usd0) | set(usd1)),
                   key=lambda kv: -abs(kv[1]))
    moved = sum(abs(m) for _, m in moves) / (2.0 * lam_nav) if lam_nav > 0 else 0.0
    s_m_txt = 'None' if s_m is None else f'{float(s_m):.2f}'
    h_txt = '' if h is None else f' h={int(h)}'
    budget_txt = ''
    if budgeted is not None:
        _, beta_b = _shares(budgeted, bench_tickers)
        budget_txt = (f' beta_budget={budget_mode} pool={float(beta_pool):.1f} '
                      f'beta_share_budget={beta_b:.3f} beta_usd_budget={beta_b * lam_nav:.0f}')
    return (f'bench_sizing.{mode}[{regime_state}]: S_m={s_m_txt}{h_txt} bench={sorted(bench_tickers)} '
            f'dropped={len(dropped)}/{len(before)} beta_share_before={beta0:.3f} beta_share_after={beta1:.3f} '
            f'gross_moved_frac={moved:.3f}{budget_txt} dropped_tickers={sorted(dropped)[:15]} top_moves={moves[:10]}')
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing.py -q`
Expected: all PASS (the two pre-existing `shadow_line` tests must still pass unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/execution/benchmark_sizing.py tests/execution/test_benchmark_sizing.py
git commit -m "feat(bench): shadow_line carries beta_budget pool/share/usd (spec 2026-08-30 §3.3)"
```

---

### Task 3: Migration 152 + `.env.example`

**Files:**
- Create: `src/database/migrations/152_beta_budget.sql`
- Modify: `.env.example` (next to the existing `OPENCLAW_BENCH_RELATIVE_SIZING` entry)

**Interfaces:**
- Produces: `pipeline_config` rows `benchmark_max_nav_frac='1.0'`, `bench_realized_anchor='2026-06-23'` (both idempotent). Applied by johnbot's `postgres.js` migration replay on its next restart — the readers default to the same values, so nothing depends on the restart.

- [ ] **Step 1: Write the migration**

```sql
-- 152: beta-budget sizing (2026-08-30; docs/specs/2026-08-30-beta-budget-sizing-spec.md D-4, D-6).
-- Idempotent: never overwrites an operator-edited value.
INSERT INTO pipeline_config (key, value, description, updated_at)
VALUES ('benchmark_max_nav_frac', '1.0',
        'Spec 2026-08-30 D-4: under OPENCLAW_BENCH_BETA_BUDGET=1 a benchmark ticker''s |target_usd| is clamped to this fraction of NAV (shaved, never redistributed). 1.0 = the reference portfolio is unlevered.',
        NOW())
ON CONFLICT (key) DO NOTHING;

INSERT INTO pipeline_config (key, value, description, updated_at)
VALUES ('bench_realized_anchor', '2026-06-23',
        'Spec 2026-08-30 D-6: anchor date (YYYY-MM-DD) for the daily bench_realized book-vs-SPY line appended to the #trade-reports digest. 2026-06-23 = start of the P&L-bleed window.',
        NOW())
ON CONFLICT (key) DO NOTHING;
```

- [ ] **Step 2: Verify the SQL parses against the real DB WITHOUT applying it** (no psql on the box):

Run:
```bash
cd /root/openclaw && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" python3 - <<'PY'
import os, psycopg2
sql = open('src/database/migrations/152_beta_budget.sql').read()
c = psycopg2.connect(os.environ['POSTGRES_URI']); cur = c.cursor()
cur.execute(sql); c.rollback(); print('parses + executes; rolled back')
PY
```
Expected: `parses + executes; rolled back`.

- [ ] **Step 3: `.env.example`** — directly under the `OPENCLAW_BENCH_RELATIVE_SIZING` entry add:

```
# Spec 2026-08-30 (beta budget): '1' = the conviction rule C removes from alpha is
# redirected to the benchmark ticker (SPY) and the benchmark is capped at
# pipeline_config.benchmark_max_nav_frac x NAV. Only acts when
# OPENCLAW_BENCH_RELATIVE_SIZING=1 and a qualified benchmark ticker exists.
# Unset/0 = shadow: the bench_sizing line prints beta_budget=shadow every cycle.
# OPENCLAW_BENCH_BETA_BUDGET=1
```

- [ ] **Step 4: Commit**

```bash
git add src/database/migrations/152_beta_budget.sql .env.example
git commit -m "chore(bench): migration 152 seeds benchmark_max_nav_frac + bench_realized_anchor; .env.example documents OPENCLAW_BENCH_BETA_BUDGET"
```

---

### Task 4: Sizer wiring — budget selection, INFO attribution, NAV cap

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` rule-C block (~:1729–1757) and the line after the cluster cap (`target_usd = _apply_asset_corr_cap(...)`, ~:1917)
- Test: `tests/execution/test_sizer_benchmark_hurdle_wiring.py`, `tests/execution/test_sizer_benchmark_cap_exemptions.py`, `tests/execution/test_sizer_flatten_zero_conviction.py`

**Interfaces:**
- Consumes: `bz.apply_beta_budget`, `bz.beta_budget_enabled`, `bz.benchmark_max_nav_frac`, `bz.shadow_line(..., budgeted=, beta_pool=, budget_mode=)`, `bz.BETA_BUDGET_ENV`.
- Produces: sizer behaviour per spec §3.2/§3.4/§3.5.

- [ ] **Step 1: Write the failing tests.**

Append to `tests/execution/test_sizer_benchmark_hurdle_wiring.py` (the module's `run()` already sets `BENCH_SIZING_ENV`; add the budget flag through a new keyword — edit `run()`'s signature to `def run(monkeypatch, flag, s_m=2.0, lines=None, budget=False, max_nav_frac=1.0):` and, right after the `BENCH_SIZING_ENV` setenv lines, add `monkeypatch.setenv(bz.BETA_BUDGET_ENV, '1' if budget else '0')` and `monkeypatch.setattr(bz, 'benchmark_max_nav_frac', lambda default=1.0, conn=None: max_nav_frac)`):

```python
def test_budget_applies_only_with_both_flags(monkeypatch):
    lines = []
    t = targets(run(monkeypatch, flag=True, budget=True, lines=lines))
    gross = LAM * NAV
    # pool = 2.0 (ZZTA hands S_m) + 1.5 (ZZTB dropped, whole |S|) = 3.5; SPY = 2.0 + 3.5 = 5.5 of Σ 6.1
    assert 'ZZTB' not in t
    assert abs(t['ZZTA'] - gross * 0.6 / 6.1) < 1e-6
    assert abs(t['SPY'] - gross * 5.5 / 6.1) < 1e-6
    assert any('bench_sizing.apply[LOW_VOL]' in l and 'beta_budget=apply pool=3.5' in l for l in lines)


def test_budget_flag_alone_is_rule_c_shadow_and_prints_budget_shadow(monkeypatch):
    lines = []
    t = targets(run(monkeypatch, flag=False, budget=True, lines=lines))
    gross = LAM * NAV
    assert abs(t['SPY'] - gross * 2.0 / 6.1) < 1e-6           # raw S_adj book, byte-identical to today
    assert any('bench_sizing.shadow[LOW_VOL]' in l and 'beta_budget=shadow pool=3.5' in l for l in lines)


def test_rule_c_on_budget_off_is_unchanged(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=False))
    gross = LAM * NAV
    assert abs(t['SPY'] - gross * 2.0 / 2.6) < 1e-6           # Task-wiring behaviour from 2026-08-29


def test_budget_with_no_s_m_falls_back_to_raw(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=True, s_m=None))
    assert set(t) == {'SPY', 'ZZTA', 'ZZTB'}


def test_budget_nav_cap_clamps_benchmark_without_redistribution(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=True, max_nav_frac=0.5))
    gross = LAM * NAV
    assert abs(t['SPY'] - 0.5 * NAV) < 1e-6                    # 5.5/6.1 * 200k = 180k -> clamped to 50k
    assert abs(t['ZZTA'] - gross * 0.6 / 6.1) < 1e-6           # alpha untouched (no renorm-up)


def test_nav_cap_inert_when_budget_off(monkeypatch):
    t = targets(run(monkeypatch, flag=True, budget=False, max_nav_frac=0.5))
    gross = LAM * NAV
    assert abs(t['SPY'] - gross * 2.0 / 2.6) < 1e-6           # 153.8k > 50k, NOT clamped
```

Append to `tests/execution/test_sizer_flatten_zero_conviction.py` a test using its `_run` harness. Read `_run`'s signature first (`sed -n 96,160p`) and build it as: weights rows for `S_beta_spy` (daily_weight 0.8) and ten alpha strategies (daily_weight 0.5, all ≤ S_m), carried rows `SPY` + ten fixture tickers, `broker={}`, `min_acting_strategies=1`, EOD lane, flatten flag `'1'`; set `OPENCLAW_BENCH_RELATIVE_SIZING='1'` and `OPENCLAW_BENCH_BETA_BUDGET='1'`; patch `execution.benchmark_sleeve.load_benchmark_sleeve_ids` → `{'S_beta_spy'}`, `execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing` → `0.8`, `execution.benchmark_sizing.benchmark_max_nav_frac` → `1.0`, and `_sizer._post_flatten_alert` → record calls:

```python
def test_e_all_alpha_below_hurdle_with_benchmark_becomes_100pct_spy_not_flatten(monkeypatch):
    """Spec 2026-08-30 §3.5: every alpha ≤ S_m + a qualified benchmark ticker
    -> the book is SPY at min(λ·NAV, benchmark_max_nav_frac·NAV); no flatten."""
    ...  # build per the description above
    orders = _run(...)
    opens = {o['ticker']: o['target_usd'] for o in orders if not _is_close(o)}
    assert set(opens) == {'SPY'}
    assert abs(opens['SPY'] - NAV * 1.0) < 1e-6      # λ·NAV would exceed NAV -> capped at 1.0·NAV
    assert flatten_calls == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_hurdle_wiring.py tests/execution/test_sizer_flatten_zero_conviction.py -q`
Expected: the new tests FAIL (budget not applied: `t['SPY']` equals the rule-C value; `beta_budget=` absent from lines; flatten test finds ten alpha names or the flatten firing). Pre-existing tests PASS.

- [ ] **Step 3: Implement — rule-C block.** In `_sharpe_cadence_path`, replace the block from `_bench_applied_dropped = 0` through the `except Exception as e:` WARN with:

```python
    _bench_applied_dropped = 0
    _beta_budget_applied = False
    try:
        _h = _bsz.load_benchmark_horizon()
        _s_m = _bsz.regime_benchmark_sharpe_for_sizing(regime_state, date.today(), horizon=_h)
        _before = dict(ticker_w)
        _hurdled, _bench_dropped = _bsz.apply_benchmark_hurdle(_before, _s_m, _bench_tkrs)
        # Beta budget (spec 2026-08-30 §3.1/§3.2): the conviction rule C removed
        # is redirected to the benchmark ticker(s) so Σ|w| is conserved and the
        # book degrades toward buy-and-hold SPY instead of re-normalized alpha.
        # Applies ONLY when rule C applies AND OPENCLAW_BENCH_BETA_BUDGET=1.
        _budgeted, _beta_pool = _bsz.apply_beta_budget(_before, _hurdled, _s_m, _bench_tkrs)
        _bench_on = _bsz.bench_relative_sizing_enabled()
        _budget_on = _bsz.beta_budget_enabled()
        # B1 (final fix wave, controller ruling — D5's premise is beta as the
        # base): flag ON with NO net-direction-qualified benchmark ticker this
        # cycle (sleeve silent, or net-cancelled) must NOT apply the hurdle —
        # a healthy day where every alpha sits at/below S_m would otherwise
        # empty ticker_w straight into the ARMED zero-conviction flatten with
        # no beta base to fall back on. Only apply when both the flag is ON
        # AND there is at least one qualified benchmark ticker.
        _apply_hurdle = _bench_on and bool(_bench_tkrs)
        _apply_budget = _apply_hurdle and _budget_on
        _bline = _bsz.shadow_line(regime_state, _s_m, _before, _hurdled, _bench_dropped,
                                  _bench_tkrs, lam * nav, mode='apply' if _apply_hurdle else 'shadow', h=_h,
                                  budgeted=_budgeted, beta_pool=_beta_pool,
                                  budget_mode='apply' if _apply_budget else 'shadow')
        logger.info(_bline)
        if os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') != '1':
            _post_corr_cumsharpe_log(_bline)
        if _bench_on and not _bench_tkrs:
            logger.warning('bench_sizing: flag ON but no net-direction-qualified benchmark ticker '
                           'this cycle; rule C NOT applied (sized on raw S_adj) — '
                           'dropped-if-applied=%d/%d', len(_bench_dropped), len(_before))
        if _apply_hurdle:
            for _t in _bench_dropped:
                ticker_meta.pop(_t, None)
            ticker_w = defaultdict(float, _budgeted if _apply_budget else _hurdled)
            _bench_applied_dropped = len(_bench_dropped)
            _beta_budget_applied = _apply_budget
            if _apply_budget and _bench_applied_dropped and _bench_applied_dropped == len(_before) - len(_bench_tkrs):
                logger.info('bench_sizing: rule C moved the whole book to beta (pool=%.1f, dropped=%d)',
                            _beta_pool, _bench_applied_dropped)
    except Exception as e:
        logger.warning('bench_sizing: failed (%s: %s); sizing on raw S_adj', type(e).__name__, e)
```

Note `_bench_exempt` (defined ABOVE this block) stays as is — the per-ticker cap and cluster-cap exemptions are already gated on rule C's flag.

- [ ] **Step 4: Implement — NAV cap.** Immediately after

```python
    target_usd = _apply_asset_corr_cap(target_usd, gate_net_sharpe, nav,
                                       lam=lam_global, exclude=_bench_exempt)
```

insert:

```python
    # Benchmark NAV cap (spec 2026-08-30 §3.4, D-4): under the beta budget the
    # reference portfolio is UNLEVERED, so a benchmark ticker never exceeds
    # benchmark_max_nav_frac × NAV (default 1.0). Shaved, never redistributed
    # (same philosophy as the two caps above). Inert unless the budget applied
    # this cycle. Fail-open: an unreadable frac falls back to 1.0 in the reader.
    if _beta_budget_applied:
        _max_bench = _bsz.benchmark_max_nav_frac() * nav
        for _t in _bench_tkrs:
            _usd = target_usd.get(_t)
            if _usd is not None and abs(_usd) > _max_bench:
                logger.info('bench_sizing: benchmark %s clamped %.0f -> %.0f (%.2f×NAV)',
                            _t, _usd, math.copysign(_max_bench, _usd), _max_bench / nav)
                target_usd[_t] = math.copysign(_max_bench, _usd)
```

`_beta_budget_applied` is initialised to `False` before the `try` in Step 3, so it always exists. Confirm `math` is imported in the sizer (`grep -n '^import math' src/execution/regime_blended_sizer.py`); add it if not.

- [ ] **Step 5: Run to verify they pass**

Run: `python3 -m pytest tests/execution/test_sizer_benchmark_hurdle_wiring.py tests/execution/test_sizer_benchmark_cap_exemptions.py tests/execution/test_sizer_flatten_zero_conviction.py tests/execution/test_sizer_benchmark_acting_gate.py tests/execution/test_benchmark_sizing.py -q`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/execution/test_sizer_benchmark_hurdle_wiring.py tests/execution/test_sizer_flatten_zero_conviction.py
git commit -m "feat(sizer): beta budget behind OPENCLAW_BENCH_BETA_BUDGET — budgeted book when rule C applies, benchmark NAV cap, whole-book-to-beta attribution (spec 2026-08-30 §3.2/3.4/3.5)"
```

---

### Task 5: Replay `--beta-budget` / `--max-nav-frac`

**Files:**
- Modify: `scripts/bench_relative_sizing_replay.py` (`_size`, `main`, `diff_books` output)

**Interfaces:**
- Consumes: `bz.BETA_BUDGET_ENV`, `bz.benchmark_max_nav_frac`.
- Produces: CLI flags `--beta-budget` (ON leg also sets `OPENCLAW_BENCH_BETA_BUDGET=1`), `--max-nav-frac FLOAT` (monkeypatches `bz.benchmark_max_nav_frac` for the run, read-only).

- [ ] **Step 1: Implement**

In `_size(nav, regime, flag_on)` change the signature to `_size(nav, regime, flag_on, *, budget=False, max_nav_frac=None)` and after the `BENCH_SIZING_ENV` handling add:

```python
    if flag_on and budget: os.environ[bz.BETA_BUDGET_ENV] = '1'
    else:                  os.environ.pop(bz.BETA_BUDGET_ENV, None)
    if max_nav_frac is not None:
        bz.benchmark_max_nav_frac = lambda default=1.0, conn=None, _v=float(max_nav_frac): _v
```

In `main()` add `ap.add_argument('--beta-budget', action='store_true')` and `ap.add_argument('--max-nav-frac', type=float, default=None)`; call `on = _size(a.nav, regime, True, budget=a.beta_budget, max_nav_frac=a.max_nav_frac)`; after the `gross OFF=… ON=…` print add:

```python
    beta_usd = sum(abs(v) for t, v in on.items() if t in bench)
    print(f"beta_usd_on={beta_usd:.0f} ({beta_usd / a.nav * 100:.1f}% NAV) alpha_gross_on={d['gross_on'] - beta_usd:.0f} "
          f"mode={'rule C + beta budget' if a.beta_budget else 'rule C'}")
```

Update the module docstring's Usage line to mention both flags.

- [ ] **Step 2: Verify (read-only, outside 13:00–20:15 UTC on weekdays; today is Sunday)**

Run: `cd /root/openclaw && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" timeout 900 nice -n 19 python3 scripts/bench_relative_sizing_replay.py --nav 92342.81 --beta-budget --top 5 2>&1 | grep -E "^(regime=|gross |beta_usd_on|dropped|added)"`
Expected: `beta_usd_on=…` printed; because `S_beta_spy` has no signal until Mon 15:00 ET the sizer will log `flag ON but no net-direction-qualified benchmark ticker` and OFF==ON (`beta_usd_on` small) — that is the correct current state; the script must not error. Paste the output into the report.

- [ ] **Step 3: Commit**

```bash
git add scripts/bench_relative_sizing_replay.py
git commit -m "scripts: bench replay --beta-budget / --max-nav-frac (spec 2026-08-30 §3.7)"
```

---

### Task 6: `bench_realized` line (D-6)

**Files:**
- Create: `src/execution/bench_realized.py`
- Modify: `src/execution/send_report.py` (after `summary, file_text = _fmt_closed_positions_digest(run_date, closed_rows)`)
- Test: `tests/execution/test_bench_realized.py`

**Interfaces:**
- Consumes: `backtest.benchmark_baseline.load_benchmark_closes(start, end, 'SPY')`, `execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing`, `logs/pnl_daily_ohlc.json` (`{"days": {"YYYY-MM-DD": {"open","high","low","close"}}}`, `close` = NAV), `pipeline_config.bench_realized_anchor`.
- Produces: `ANCHOR_KEY='bench_realized_anchor'`, `DEFAULT_ANCHOR='2026-06-23'`, `load_nav_history(path=None) -> dict[str, float]`, `compute(nav_by_date, spy_by_date, run_date, anchor) -> dict | None`, `format_line(stats, regime, s_m) -> str`, `bench_realized_line(run_date, *, nav_path=None, conn=None) -> str | None`.

Ruling (controller, deviates from spec §6's channel): the line is appended to the #trade-reports digest that `send_report` already posts every cycle, instead of a separate #botjohn-log post — no new webhook lookup, no expiry key, same visibility. Recorded in the ledger.

- [ ] **Step 1: Write the failing tests** — `tests/execution/test_bench_realized.py`:

```python
"""Spec 2026-08-30 §6 (D-6): daily book-vs-SPY realized line. Pure compute on
aligned daily closes; report-only, never gates."""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import pytest  # noqa: E402
from execution import bench_realized as br  # noqa: E402


def _series(start_nav, daily, n, first='2026-06-01'):
    import datetime as dt
    d0 = dt.date.fromisoformat(first); out = {}; v = start_nav
    for i in range(n):
        d = d0 + dt.timedelta(days=i)
        if d.weekday() < 5:
            out[d.isoformat()] = v
            v *= (1 + daily)
    return out


def _series_noisy(start, daily, n, first='2026-06-01'):
    """Like _series but the daily return alternates daily*1.5 / daily*0.5 so
    the trailing-20 std is non-zero (a constant return has no Sharpe)."""
    import datetime as dt
    d0 = dt.date.fromisoformat(first); out = {}; v = start; k = 0
    for i in range(n):
        d = d0 + dt.timedelta(days=i)
        if d.weekday() < 5:
            out[d.isoformat()] = v
            v *= (1 + daily * (1.5 if k % 2 == 0 else 0.5)); k += 1
    return out


def test_compute_returns_since_anchor_windows_and_sharpes():
    nav = _series(100_000.0, -0.002, 120)     # book bleeds 20 bp/day (constant)
    spy = _series(500.0, +0.001, 120)         # SPY +10 bp/day (constant)
    run_date = max(nav)
    st = br.compute(nav, spy, run_date, anchor='2026-06-23')
    assert st['anchor'] == '2026-06-23' and st['n_common'] >= 60
    assert st['book_since'] < 0 < st['spy_since']
    assert st['gap_pp'] == pytest.approx((st['book_since'] - st['spy_since']) * 100)
    assert st['book_20d'] == pytest.approx((1 - 0.002) ** 20 - 1, rel=1e-6)
    assert st['spy_20d'] == pytest.approx((1 + 0.001) ** 20 - 1, rel=1e-6)
    assert st['book_60d'] < st['book_20d'] and st['spy_60d'] > st['spy_20d']
    assert st['book_sharpe_20d'] is None and st['spy_sharpe_20d'] is None   # constant returns: zero variance -> None


def test_compute_sharpes_sign_with_noisy_returns():
    nav = _series_noisy(100_000.0, -0.002, 120)
    spy = _series_noisy(500.0, +0.001, 120)
    st = br.compute(nav, spy, max(nav), anchor='2026-06-23')
    assert st['book_sharpe_20d'] < 0 < st['spy_sharpe_20d']


def test_compute_uses_only_common_dates_and_handles_gaps():
    nav = _series(100_000.0, 0.0, 40); spy = _series(500.0, 0.0, 40)
    spy.pop(sorted(spy)[-3])                                   # SPY missing a day
    st = br.compute(nav, spy, max(nav), anchor='2026-06-01')
    assert st['n_common'] == len(nav) - 1
    assert st['book_20d'] == 0.0 and st['spy_20d'] == 0.0
    assert st['book_sharpe_20d'] is None and st['spy_sharpe_20d'] is None   # zero variance -> None, never NaN


def test_compute_too_short_returns_none():
    nav = _series(1.0, 0.0, 3); spy = _series(1.0, 0.0, 3)
    assert br.compute(nav, spy, max(nav), anchor='2026-06-01') is None


def test_format_line_shape():
    st = {'anchor': '2026-06-23', 'n_common': 48, 'book_since': -0.29, 'spy_since': 0.049, 'gap_pp': -33.9,
          'book_20d': -0.05, 'spy_20d': 0.02, 'book_60d': -0.2, 'spy_60d': 0.04,
          'book_sharpe_20d': -3.1, 'spy_sharpe_20d': 1.2}
    line = br.format_line(st, 'LOW_VOL', 0.805)
    assert line.startswith('bench_realized: since=2026-06-23 book=-29.0% spy=+4.9% gap=-33.9pp')
    assert '| 20d book=-5.0% spy=+2.0% | 60d book=-20.0% spy=+4.0% |' in line
    assert 'regime=LOW_VOL book_sharpe_20d=-3.10 spy_sharpe_20d=+1.20 S_m=0.805' in line
    assert br.format_line(dict(st, book_sharpe_20d=None, spy_sharpe_20d=None), 'CRISIS', None).endswith(
        'regime=CRISIS book_sharpe_20d=n/a spy_sharpe_20d=n/a S_m=n/a')


def test_load_nav_history_reads_close(tmp_path):
    p = tmp_path / 'pnl.json'
    p.write_text(json.dumps({'days': {'2026-08-28': {'open': 1, 'high': 2, 'low': 0, 'close': 92342.81},
                                       '2026-08-27': {'close': 93000.0}}}))
    assert br.load_nav_history(p) == {'2026-08-28': 92342.81, '2026-08-27': 93000.0}


def test_bench_realized_line_is_fail_open(monkeypatch, tmp_path):
    monkeypatch.setattr(br, 'load_nav_history', lambda path=None: (_ for _ in ()).throw(OSError('no file')))
    assert br.bench_realized_line('2026-08-28', conn=object()) is None


def test_bench_realized_line_end_to_end(monkeypatch):
    nav = _series(100_000.0, -0.001, 90); spy = _series(500.0, 0.001, 90)
    monkeypatch.setattr(br, 'load_nav_history', lambda path=None: nav)
    monkeypatch.setattr(br, '_load_spy_closes', lambda start, end: spy)
    monkeypatch.setattr(br, '_load_anchor', lambda conn: '2026-06-23')
    monkeypatch.setattr(br, '_load_regime_and_s_m', lambda conn, run_date: ('LOW_VOL', 0.805))
    line = br.bench_realized_line(max(nav), conn=object())
    assert line.startswith('bench_realized: since=2026-06-23 book=-') and 'S_m=0.805' in line
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/execution/test_bench_realized.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'execution.bench_realized'`.

- [ ] **Step 3: Implement `src/execution/bench_realized.py`**

```python
"""bench_realized.py — daily book-vs-buy-and-hold-SPY realized line (spec
2026-08-30 §6, D-6). Report-only: gates nothing, never raises out of
bench_realized_line (returns None on any failure, logged).

NAV history = logs/pnl_daily_ohlc.json (`days[date].close`, the live sampler's
end-of-day NAV); SPY = prices.parquet via benchmark_baseline.load_benchmark_closes
(pyarrow pushdown). Returns are computed on the COMMON dates of both series.
Sharpe over the trailing 20 common dates: (mean − rf/252)/std·√252, rf 5 %
(unified_backtest's convention); zero variance or < 5 obs -> None.
"""
from __future__ import annotations
import json
import logging
import math
import os
import statistics
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
NAV_HISTORY_PATH = ROOT / 'logs' / 'pnl_daily_ohlc.json'
ANCHOR_KEY = 'bench_realized_anchor'
DEFAULT_ANCHOR = '2026-06-23'
RISK_FREE_DAILY = 0.05 / 252
MIN_COMMON = 5


def load_nav_history(path=None) -> dict[str, float]:
    p = Path(path) if path else NAV_HISTORY_PATH
    days = json.loads(p.read_text()).get('days') or {}
    return {d: float(v['close']) for d, v in days.items() if isinstance(v, dict) and v.get('close') is not None}


def _load_spy_closes(start: str, end: str) -> dict[str, float]:
    from backtest.benchmark_baseline import load_benchmark_closes
    return load_benchmark_closes(start, end, 'SPY')


def _load_anchor(conn) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (ANCHOR_KEY,))
            row = cur.fetchone()
        v = str(row[0]).strip() if row and row[0] else ''
        import datetime as _dt
        _dt.date.fromisoformat(v)
        return v
    except Exception:
        return DEFAULT_ANCHOR


def _load_regime_and_s_m(conn, run_date):
    regime, s_m = None, None
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM intraday_regime_states ORDER BY ts_utc DESC LIMIT 1')
            row = cur.fetchone()
            regime = row[0] if row else None
        if regime:
            from execution.benchmark_sizing import regime_benchmark_sharpe_for_sizing
            s_m = regime_benchmark_sharpe_for_sizing(regime, run_date, conn=conn)
    except Exception as e:
        logger.warning('[bench_realized] regime/S_m unavailable: %s', e)
    return regime, s_m


def _sharpe(rets: list[float]):
    if len(rets) < MIN_COMMON:
        return None
    sd = statistics.stdev(rets)
    if not sd or not math.isfinite(sd):
        return None
    return (statistics.fmean(rets) - RISK_FREE_DAILY) / sd * math.sqrt(252)


def _window_return(vals: list[float], n: int):
    if len(vals) < n + 1:
        return None
    return vals[-1] / vals[-n - 1] - 1.0


def compute(nav_by_date: dict, spy_by_date: dict, run_date, anchor: str):
    run_date = str(run_date)[:10]
    dates = sorted(d for d in nav_by_date if d in spy_by_date and d <= run_date)
    if len(dates) < MIN_COMMON:
        return None
    nav = [float(nav_by_date[d]) for d in dates]
    spy = [float(spy_by_date[d]) for d in dates]
    # since-anchor: first common date >= anchor
    i0 = next((i for i, d in enumerate(dates) if d >= anchor), None)
    if i0 is None or i0 == len(dates) - 1:
        return None
    book_since = nav[-1] / nav[i0] - 1.0
    spy_since = spy[-1] / spy[i0] - 1.0
    nav_r = [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav))]
    spy_r = [spy[i] / spy[i - 1] - 1.0 for i in range(1, len(spy))]
    return {
        'anchor': anchor, 'n_common': len(dates), 'run_date': dates[-1],
        'book_since': book_since, 'spy_since': spy_since, 'gap_pp': (book_since - spy_since) * 100.0,
        'book_20d': _window_return(nav, 20), 'spy_20d': _window_return(spy, 20),
        'book_60d': _window_return(nav, 60), 'spy_60d': _window_return(spy, 60),
        'book_sharpe_20d': _sharpe(nav_r[-20:]), 'spy_sharpe_20d': _sharpe(spy_r[-20:]),
    }


def _pct(v):
    return 'n/a' if v is None else f'{v * 100:+.1f}%'


def _sh(v):
    return 'n/a' if v is None else f'{v:+.2f}'


def format_line(st: dict, regime, s_m) -> str:
    return (f"bench_realized: since={st['anchor']} book={_pct(st['book_since'])} spy={_pct(st['spy_since'])} "
            f"gap={st['gap_pp']:+.1f}pp | 20d book={_pct(st['book_20d'])} spy={_pct(st['spy_20d'])} | "
            f"60d book={_pct(st['book_60d'])} spy={_pct(st['spy_60d'])} | "
            f"regime={regime or 'n/a'} book_sharpe_20d={_sh(st['book_sharpe_20d'])} "
            f"spy_sharpe_20d={_sh(st['spy_sharpe_20d'])} S_m={'n/a' if s_m is None else f'{float(s_m):.3f}'}")


def bench_realized_line(run_date, *, nav_path=None, conn=None):
    """The full line, or None on any failure (logged). Own connection when conn is None."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        nav = load_nav_history(nav_path)
        if not nav:
            return None
        anchor = _load_anchor(conn)
        spy = _load_spy_closes(min(min(nav), anchor), str(run_date)[:10])
        st = compute(nav, spy, run_date, anchor)
        if st is None:
            return None
        regime, s_m = _load_regime_and_s_m(conn, run_date)
        return format_line(st, regime, s_m)
    except Exception as e:
        logger.warning('[bench_realized] skipped (%s: %s)', type(e).__name__, e)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
```

`_pct` must handle `None` (`book_60d` is None when fewer than 61 common dates) — it does. `format_line` must not crash on `gap_pp` None — `gap_pp` is always a float when `compute` returns a dict.

- [ ] **Step 4: Wire into `send_report.main`** — right after `summary, file_text = _fmt_closed_positions_digest(run_date, closed_rows)`:

```python
    # Spec 2026-08-30 D-6: book-vs-buy-and-hold-SPY realized line. Report-only,
    # fail-open; appended to the #trade-reports digest (controller ruling: same
    # daily post, no separate webhook).
    try:
        from execution.bench_realized import bench_realized_line
        _br = bench_realized_line(run_date)
        if _br:
            summary = f'{summary}\n{_br}'
            print(f'[send_report] {_br}')
    except Exception as e:
        print(f'[send_report] bench_realized skipped: {e}')
```

- [ ] **Step 5: Run to verify they pass, then a real dry-run**

Run: `python3 -m pytest tests/execution/test_bench_realized.py -q` → all PASS.
Run: `cd /root/openclaw && POSTGRES_URI="$(grep -E '^POSTGRES_URI=' .env | cut -d= -f2- | tr -d '"')" PYTHONPATH=src timeout 300 python3 src/execution/send_report.py --date 2026-08-28 --dry-run 2>&1 | grep -E "bench_realized|DRY-RUN"` → prints a `bench_realized: since=2026-06-23 book=…% spy=…%` line (expected roughly book −29 % / spy +4.9 %). Paste it into the report.

- [ ] **Step 6: Commit**

```bash
git add src/execution/bench_realized.py src/execution/send_report.py tests/execution/test_bench_realized.py
git commit -m "feat(report): bench_realized — daily book vs buy-and-hold SPY line appended to the trade-reports digest (spec 2026-08-30 D-6)"
```

---

### Task 7: Docs — changelog, spec status

**Files:**
- Modify: `docs/archive/changelog.md` (new entry at the top of `## Recent Changes`), `docs/specs/2026-08-30-beta-budget-sizing-spec.md` (Status line)

- [ ] **Step 1: Spec status** — change the `**Status:**` paragraph's first sentence to `**Status:** LANDED 2026-08-30 (<first>..<last> commit shas from \`git log --oneline\`; flag \`OPENCLAW_BENCH_BETA_BUDGET\` unset = shadow; flip per §5 after two clean shadow cycles).` and, in §6, add one sentence: `Implementation note: the line is appended to the #trade-reports digest by send_report (same daily post), not posted separately to #botjohn-log.`

- [ ] **Step 2: Changelog entry** (newest-first, under `## Recent Changes`), one paragraph in the file's style: what landed (Tasks 1–6 with file names), the flags and their semantics, the measured expected effect from the spec (SPY $2.1k → $71.7k / 78 % NAV, alpha gross $137k → $68k on the 08-30 replay), the NAV cap, the `bench_realized` line and the dry-run output from Task 6, and the rollout state: both flags unset/`0`, first shadow cycle Mon 2026-08-31 15:00 ET, one combined flip after two clean cycles (D-5).

- [ ] **Step 3: Commit**

```bash
git add docs/archive/changelog.md docs/specs/2026-08-30-beta-budget-sizing-spec.md
git commit -m "docs: beta-budget sizing landed — changelog + spec status (shadow; flip per §5)"
```

---

## Self-review

- **Spec coverage:** §3.1 → Task 1; §3.3 → Task 2; §3.4 (cap + config) → Tasks 1, 3, 4; §3.2/§3.5/§3.6 → Task 4; §3.7 → Task 5; §6 → Task 6 (channel ruling recorded); §5 rollout → docs (Task 7) + operator; §7 tests → each task's tests.
- **Placeholders:** Task 4's flatten test body is described, not written verbatim, because it depends on that file's `_run` harness signature — the implementer reads `_run` and writes it; the assertions are given. Task 7's changelog text is described (content, not prose).
- **Type consistency:** `apply_beta_budget(before, hurdled, s_m, bench_tickers) -> (dict, float)` used identically in Tasks 1, 2, 4; `shadow_line` keywords `budgeted=`, `beta_pool=`, `budget_mode=` identical in Tasks 2, 4; `benchmark_max_nav_frac(default=1.0, conn=None)` identical in Tasks 1, 4, 5; `_beta_budget_applied` defined before the `try` and read after the cluster cap in Task 4.
