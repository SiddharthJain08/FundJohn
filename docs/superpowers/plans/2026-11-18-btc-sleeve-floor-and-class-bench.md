# BTC Sleeve Floor + Per-Class Activation Bench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Crypto-class strategies activate against the BTC sleeve's own per-regime run vector, and the BTC sleeve is sized to at least `benchmark_min_nav_frac_crypto · NAV` (default 0.10) whenever it is acting, independent of BTC's regime Sharpe, with the crypto beta budget under its own flag.

**Architecture:** Two independent changes. (1) `_crypto_pooled_block` in `src/execution/regime_blended_sizer.py` gains a floor-weight injection before the one λ·NAV normaliser (pure algebra in `src/execution/pooled_sizing.py`), a crypto-specific beta-budget flag, a BTC-protected gross cap and two new trailing line fields. (2) `src/backtest/activation_assigner.py` resolves one bench vector per instrument class and threads the crypto vector to crypto-class strategies; the pending check, promotion gate and dashboard payload learn the per-class stamp. Everything is shadow for sizing (pooled flag unset) and live at the next daily cycle for activation.

**Tech Stack:** Python 3 (pytest, stubbed DB via monkeypatch), Node (node:test for `tests/*.js`). No migrations (pipeline_config keys are read-with-default).

**Spec:** `docs/superpowers/specs/2026-11-18-btc-sleeve-floor-and-class-bench-design.md`

## Global Constraints

- Equity byte-identity: with `OPENCLAW_CRYPTO_POOLED_SIZING` unset, `tests/execution/test_sizer_pooled_shadow.py::test_pooled_shadow_leaves_equity_targets_identical` must keep passing untouched.
- Floor default **0.10** NAV; key `benchmark_min_nav_frac_crypto`; `0` disables; value > `benchmark_max_nav_frac_crypto` is clamped to it with a WARN.
- The floor applies whenever `S_beta_btc` (any crypto-class benchmark sleeve id in `bench_ids`) has a loaded LONG `BTC-USD` signal in the crypto signal set for the run — detected from the signal rows, **not** from the sleeve's weight row; `S_m` sign/value never gates the floor.
- The floor is a **weight** injected before `res['gross_w']` is computed so the pooled normaliser conserves λ·NAV; it never survives hold-flat, never exceeds the 25 % clamp or the non-marginable funding pass.
- Crypto beta budget env `OPENCLAW_CRYPTO_BETA_BUDGET`: unset or `'1'` ⇒ ON; `'0'` ⇒ OFF. Pool harvested from crypto alts only.
- `crypto_sizing` line: append exactly ` floor_usd=%.0f pool=%.2f` after `top=…`. No other token moves.
- Activation: crypto-class strategies never see the equity vector or the equity tier-2 key. Crypto tier 2 = `strategy_activation_bench_sharpe_crypto`; tier 3 = `DEFAULT_MIN_SHARPE` with WARN. A registry without a crypto sleeve ⇒ tier 2/3 (never the `S_beta_spy` literal).
- New assigner stdout lines start exactly `bench[crypto]: ` / `thresholds[crypto]: `; must not match `HEADER_RE`, `DETAIL_RE`, `BENCH_VECTOR_RE`, `THRESHOLDS_RE`, `SUMMARY_RE` in `src/channels/api/activation_preview.js:76-111`. Equity lines byte-unchanged.
- Stamps are additive: `strategy_activation_last_applied` gains `bench_by_class`; existing keys unchanged.
- Box rules: no full suite while the fleet runs; no heavy tests 20:00–20:15Z / 20:55–21:02Z. File edit, `git commit`, `git push` as separate tool calls; commit messages via `-F <file>` (no backticks in shell).
- Tests reach the real DB at import (`.env`): stub every DB touchpoint as the existing harnesses do.

## Review Focus

1. Sleeve acting with `S_m ≤ 0` and no other crypto signal: `w_by` has no sleeve row, `meta` is empty — the floor must still size BTC-USD (Task 3 test `test_floor_applies_with_negative_s_m_and_no_weight_row`).
2. Equity-empty day (`eq_gross == 0`) with only the sleeve: the normaliser hands BTC λ·NAV; the 25 % clamp bounds it, the floor must not divide by zero (Task 2 `test_floor_weight_zero_gross_returns_unit_weight`, Task 3 `test_equity_empty_floor_is_capped_not_nan`).
3. `benchmark_min_nav_frac_crypto` mis-set above the 25 % cap or to garbage (Task 1 `test_benchmark_min_nav_frac_clamps_to_max_and_handles_garbage`).
4. Registry with crypto strategies but no crypto sleeve run / no sleeve: crypto cells must fall to the crypto key or 0.5 with a WARN, never SPY (Task 4 `test_crypto_vector_without_crypto_sleeve_never_uses_equity`).
5. Dashboard parser double-counting: the new lines must not be counted as strategies or overwrite the equity bench card (Task 6 node test `crypto bench lines are ignored by the equity parser`).

---

### Task 1: Config readers — floor fraction + crypto budget flag

**Files:**
- Modify: `src/execution/benchmark_sizing.py:36-46` (constants), `:60-75` (flag helpers), `:95-150` (readers)
- Test: `tests/execution/test_benchmark_sizing_multi_bench.py`

**Interfaces:**
- Produces:
  - `MIN_NAV_FRAC_KEY_CRYPTO = 'benchmark_min_nav_frac_crypto'`, `MIN_NAV_FRAC_CRYPTO_DEFAULT = 0.10`
  - `CRYPTO_BETA_BUDGET_ENV = 'OPENCLAW_CRYPTO_BETA_BUDGET'`
  - `crypto_beta_budget_enabled() -> bool` (unset/`'1'` ⇒ True; `'0'` ⇒ False)
  - `benchmark_min_nav_frac(conn=None, *, instrument_class: str = 'crypto') -> float` — reads the crypto key; missing/garbage ⇒ 0.10; negative ⇒ 0.0 with WARN; `0.0` allowed (disables); result `min(v, benchmark_max_nav_frac(conn=conn, instrument_class='crypto'))` with a WARN when clamped. Any `instrument_class != 'crypto'` returns `0.0` (no equity floor).
  - `_nonneg_config_float(key, default, conn=None) -> float` (like `_positive_config_float` but accepts `0`).

- [ ] **Step 1: Write the failing tests** (append to `tests/execution/test_benchmark_sizing_multi_bench.py`, reuse that file's cursor/conn stubs)

```python
def test_crypto_beta_budget_flag_default_on(monkeypatch):
    monkeypatch.delenv(bz.CRYPTO_BETA_BUDGET_ENV, raising=False)
    assert bz.crypto_beta_budget_enabled() is True
    monkeypatch.setenv(bz.CRYPTO_BETA_BUDGET_ENV, '0'); assert bz.crypto_beta_budget_enabled() is False
    monkeypatch.setenv(bz.CRYPTO_BETA_BUDGET_ENV, '1'); assert bz.crypto_beta_budget_enabled() is True
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '0'); assert bz.crypto_beta_budget_enabled() is True  # SPY flag irrelevant

def test_benchmark_min_nav_frac_default_and_zero():
    assert bz.benchmark_min_nav_frac(conn=_conn(None)) == 0.10          # missing row -> default
    assert bz.benchmark_min_nav_frac(conn=_conn('0')) == 0.0            # 0 disables
    assert bz.benchmark_min_nav_frac(conn=_conn('0.15')) == 0.15
    assert bz.benchmark_min_nav_frac(conn=_conn('0.15'), instrument_class='equity') == 0.0

def test_benchmark_min_nav_frac_clamps_to_max_and_handles_garbage(caplog):
    # max key absent -> 0.25 default cap; 0.40 requested -> 0.25 + WARN
    assert bz.benchmark_min_nav_frac(conn=_conn_multi({'benchmark_min_nav_frac_crypto': '0.40'})) == 0.25
    assert 'clamp' in caplog.text.lower()
    assert bz.benchmark_min_nav_frac(conn=_conn('abc')) == 0.10
    assert bz.benchmark_min_nav_frac(conn=_conn('-1')) == 0.0
```
(`_conn(value)` = a stub whose cursor returns `(value,)` or `None`; `_conn_multi(d)` answers per key — write both in the test module if not already present.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing_multi_bench.py -k "crypto_beta_budget or min_nav_frac" -v`
Expected: FAIL — `AttributeError: … has no attribute 'crypto_beta_budget_enabled'`

- [ ] **Step 3: Implement** the constants, `crypto_beta_budget_enabled`, `_nonneg_config_float`, `benchmark_min_nav_frac` in `src/execution/benchmark_sizing.py`. Clamp WARN text must contain the word `clamp`.

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/execution/test_benchmark_sizing_multi_bench.py tests/execution/test_benchmark_sizing.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/benchmark_sizing.py tests/execution/test_benchmark_sizing_multi_bench.py
git commit -F /tmp/msg_t1.txt   # "sizing: benchmark_min_nav_frac (crypto floor, default 0.10) + OPENCLAW_CRYPTO_BETA_BUDGET flag"
```

---

### Task 2: Pure helpers — floor weight, protected gross cap, line fields

**Files:**
- Modify: `src/execution/pooled_sizing.py:88-99` (near `pooled_scales`), `:119-127` (`crypto_gross_cap`), `:193-218` (`shadow_line`)
- Test: `tests/execution/test_pooled_sizing_helpers.py`

**Interfaces:**
- Consumes: `pooled_scales(*, eq_gross, cr_gross, lam_global, liq_eq, liq_cr, nav) -> (scale_eq, scale_cr)` (unchanged).
- Produces:
  - `floor_weight(w_btc: float, frac: float, *, eq_gross: float, cr_gross_other: float, lam_global: float, liq_cr: float) -> tuple[float, str]` — returns `(w_out, how)`. `how ∈ {'own', 'floor', 'unit', 'off'}`. Algebra: with `G = eq_gross + cr_gross_other` and `k = lam_global·min(1,max(0,liq_cr))`, the post-normaliser BTC dollars are `w·k·NAV/(G + w)`; the floor weight is `w_f = frac·G/(k − frac)` when `k > frac` and `G > 0`. `w_out = max(w_btc, w_f)` (`how='floor'` when the floor wins, else `'own'`). `frac <= 0` ⇒ `(w_btc, 'off')`. `G == 0` and `w_btc <= 0` ⇒ `(1.0, 'unit')` (BTC alone in the normaliser takes λ·NAV, later clamped). `k <= frac` ⇒ `(max(w_btc, G), 'floor')` (degenerate: at least half of λ·NAV; the caller's clamp bounds it) and the caller logs a WARN.
  - `crypto_gross_cap(crypto_targets: dict, nav: float, frac: float, *, protect: set | None = None) -> tuple[dict, float]` — shave non-protected tickers pro-rata first; only if their total is exhausted shave protected ones pro-rata. `protect=None` ⇒ today's behaviour byte-identical.
  - `shadow_line(..., floor_usd: float = 0.0, pool: float = 0.0) -> str` — appends ` floor_usd=%.0f pool=%.2f` after `top=…`.

- [ ] **Step 1: Write the failing tests** (append to `tests/execution/test_pooled_sizing_helpers.py`)

```python
def test_floor_weight_solves_for_target_fraction():
    NAV, LAM, LIQ, F = 100_000.0, 2.0, 1.0, 0.10
    w, how = ps.floor_weight(0.0, F, eq_gross=90.0, cr_gross_other=0.0, lam_global=LAM, liq_cr=LIQ)
    assert how == 'floor'
    _, scale_cr = ps.pooled_scales(eq_gross=90.0, cr_gross=w, lam_global=LAM, liq_eq=1.0, liq_cr=LIQ, nav=NAV)
    assert abs(w * scale_cr - F * NAV) < 1e-6
    w2, how2 = ps.floor_weight(50.0, F, eq_gross=90.0, cr_gross_other=0.0, lam_global=LAM, liq_cr=LIQ)
    assert (w2, how2) == (50.0, 'own')                      # own conviction above the floor wins

def test_floor_weight_zero_gross_returns_unit_weight():
    assert ps.floor_weight(0.0, 0.10, eq_gross=0.0, cr_gross_other=0.0, lam_global=2.0, liq_cr=1.0) == (1.0, 'unit')
    assert ps.floor_weight(0.0, 0.0, eq_gross=90.0, cr_gross_other=0.0, lam_global=2.0, liq_cr=1.0) == (0.0, 'off')
    w, how = ps.floor_weight(0.0, 0.10, eq_gross=90.0, cr_gross_other=0.0, lam_global=0.05, liq_cr=1.0)
    assert how == 'floor' and w == 90.0                      # k <= frac degenerate branch

def test_crypto_gross_cap_protects_btc_first():
    t = {'BTC-USD': 10_000.0, 'ETH-USD': 30_000.0, 'SOL-USD': 20_000.0}
    out, shaved = ps.crypto_gross_cap(t, 100_000.0, 0.50, protect={'BTC-USD'})
    assert out['BTC-USD'] == 10_000.0 and abs(shaved - 10_000.0) < 1e-6
    assert abs(out['ETH-USD'] + out['SOL-USD'] - 40_000.0) < 1e-6 and abs(out['ETH-USD'] / out['SOL-USD'] - 1.5) < 1e-9
    out2, shaved2 = ps.crypto_gross_cap({'BTC-USD': 60_000.0, 'ETH-USD': 5_000.0}, 100_000.0, 0.50, protect={'BTC-USD'})
    assert out2['ETH-USD'] == 0.0 and abs(out2['BTC-USD'] - 50_000.0) < 1e-6 and abs(shaved2 - 15_000.0) < 1e-6
    assert ps.crypto_gross_cap(t, 100_000.0, 0.50) == ps.crypto_gross_cap(t, 100_000.0, 0.50, protect=None)

def test_shadow_line_floor_and_pool_fields():
    line = ps.shadow_line('HIGH_VOL', 1.79, {'BTC-USD': 9_100.0}, {'BTC-USD': 9_100.0}, [], {'BTC-USD'},
                          164_000.0, gross_cap_shaved=0.0, funding_shortfall=0.0, hold_flat=False,
                          mode='shadow', nav=91_000.0, floor_usd=8_928.0, pool=0.0)
    assert line.endswith("top=[('BTC-USD', 9100)] floor_usd=8928 pool=0.00")
```
Also extend `test_shadow_line_format`: its two lines must end with ` floor_usd=0 pool=0.00`.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/execution/test_pooled_sizing_helpers.py -v`
Expected: FAIL — `floor_weight` undefined; `TypeError: unexpected keyword 'protect'`/`'floor_usd'`

- [ ] **Step 3: Implement** `floor_weight`, the `protect` branch of `crypto_gross_cap`, and the two `shadow_line` kwargs in `src/execution/pooled_sizing.py`. Update the `_crypto_pooled_block` docstring order text is Task 3's job, not here.

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/execution/test_pooled_sizing_helpers.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/pooled_sizing.py tests/execution/test_pooled_sizing_helpers.py
git commit -F /tmp/msg_t2.txt   # "pooled_sizing: floor_weight, BTC-protected gross cap, floor_usd/pool line fields"
```

---

### Task 3: Wire the floor + crypto budget into `_crypto_pooled_block`

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:1686-1697` (docstring order), `:1725-1740` (signal load guard + sleeve detection), `:1804-1824` (hurdle/budget/floor/normaliser), `:1851` (gross cap), `:1874-1883` (line)
- Test: `tests/execution/test_sizer_pooled_shadow.py`

**Interfaces:**
- Consumes: Task 1 `benchmark_min_nav_frac`, `crypto_beta_budget_enabled`; Task 2 `floor_weight`, `crypto_gross_cap(protect=)`, `shadow_line(floor_usd=, pool=)`.
- Produces: `res['floor_usd']: float`, `res['pool']: float`, `res['sleeve_acting']: bool` on the block's return dict (read by tests; the line carries them).

Behaviour to implement (in this order inside the block):
1. Signal load guard: today `if w_by:`; change to `if w_by or (set(bench_ids or ()) & crypto_ids_all):` so the sleeve's signal is loaded even with no weight row. Keep the `in w_by` filter for the **sized** set, but compute first, from the unfiltered loaded rows: `sleeve_acting = any(s.get('strategy_id') in bench_ids and s.get('strategy_id') in crypto_ids_all and s.get('ticker') == BTC_TICKER and _dir_to_int(s.get('direction')) > 0 for s in loaded)`. Rows with `option_spec` excluded as today.
2. Budget: replace `_bsz.beta_budget_enabled()` with `_bsz.crypto_beta_budget_enabled()`; keep the returned pool: `hurdled, pool = _bsz.apply_beta_budget(...)`.
3. Floor: after the hurdle block and before `w = {t: v for t, v in w.items() if v}` / `res['gross_w']`: if `sleeve_acting`: `frac = _bsz.benchmark_min_nav_frac(instrument_class='crypto')`; `w_btc = float(w.get(BTC_TICKER, 0.0))`; `other = sum(abs(v) for t, v in w.items() if t != BTC_TICKER)`; `w_new, how = _ps.floor_weight(w_btc, frac, eq_gross=eq_gross, cr_gross_other=other, lam_global=lam_global, liq_cr=liq_cr)`; if `how in ('floor', 'unit')`: `w[BTC_TICKER] = w_new`; add `BTC_TICKER` to `bench_cr`-style exemptions for the per-ticker cap (`exempt`) so the floor is not capped by `0.10·(|S|+1)·λ·NAV`; log `crypto_sizing.floor: how=%s frac=%.3f w_btc %.4f -> %.4f`. This must also run when `meta` is empty (restructure: compute `w = {}` in that case and let the `if w:` sizing branch run). `floor_usd` = `max(0, before_usd[BTC] − own_usd)` where `own_usd = w_btc · scale_cr` (0 when `how == 'own'`/`'off'`).
4. Gross cap: `_ps.crypto_gross_cap(targets, nav, _bsz.crypto_gross_max_nav_frac(), protect={BTC_TICKER} if sleeve_acting else None)`.
5. Line: pass `floor_usd=floor_usd, pool=pool`.
6. Docstring order line: `… S_m_crypto hurdle → crypto beta budget (OPENCLAW_CRYPTO_BETA_BUDGET, default ON) → BTC sleeve floor (benchmark_min_nav_frac_crypto) → pooled normaliser → …`.

- [ ] **Step 1: Write the failing tests** (append to `tests/execution/test_sizer_pooled_shadow.py`; extend `run(...)` with `min_frac=0.10`, `cr_budget='1'` params: `monkeypatch.setattr(bz, 'benchmark_min_nav_frac', lambda conn=None, *, instrument_class='crypto': min_frac)`, `monkeypatch.setenv(bz.CRYPTO_BETA_BUDGET_ENV, cr_budget)`)

```python
def test_floor_applies_with_negative_s_m_and_no_weight_row(monkeypatch):
    # sleeve weight row absent (S_m<0 -> weight 0), only the sleeve signal present
    h = run(monkeypatch, s_m_cr=-0.4, eq_rows=None,
            cr_rows=[_row('S_cr_a', 2.0)],                       # no S_beta_btc row
            cr_carried=[_carried('S_beta_btc', 'BTC-USD')])
    line = h.written[-1]
    assert 'floor_usd=' in line and " top=[('BTC-USD', " in line
    btc = _top_usd(line, 'BTC-USD'); assert abs(btc - 0.10 * NAV) < 1.0

def test_floor_conserves_lambda_nav_under_pooling(monkeypatch):
    h = run(monkeypatch, pooled=True, s_m_cr=0.8)
    eq_gross = sum(abs(o['target_usd']) for o in h.orders if o['ticker'] not in CRYPTO_TKRS)
    cr_gross = sum(abs(o['target_usd']) for o in h.orders if o['ticker'] in CRYPTO_TKRS)
    assert abs(eq_gross + cr_gross - LAM * NAV) < 1.0
    assert _top_usd(h.written[-1], 'BTC-USD') >= 0.10 * NAV - 1.0

def test_floor_inactive_without_sleeve_signal(monkeypatch):
    h = run(monkeypatch, cr_carried=[_carried('S_cr_a', 'ETH-USD')])
    assert ' floor_usd=0 ' in h.written[-1] + ' '

def test_floor_bounded_by_btc_clamp_and_hold_flat(monkeypatch):
    h = run(monkeypatch, min_frac=0.25, btc_frac=0.25)
    assert _top_usd(h.written[-1], 'BTC-USD') <= 0.25 * NAV + 1.0
    h2 = run(monkeypatch, hold={'sleeve': {'since': 1}, 'tickers': {}})
    assert 'hold=1' in h2.written[-1] and "('BTC-USD'" not in h2.written[-1].split('top=')[1]

def test_floor_scaled_by_funding_pass(monkeypatch):
    h = run(monkeypatch, nmbp=2_000.0)
    assert _top_usd(h.written[-1], 'BTC-USD') <= 2_000.0 + 1.0 and 'funding_bind=' in h.written[-1]

def test_gross_cap_shaves_alts_before_btc(monkeypatch):
    h = run(monkeypatch, gross_frac=0.12, min_frac=0.10)
    assert abs(_top_usd(h.written[-1], 'BTC-USD') - 0.10 * NAV) < 1.0

def test_crypto_beta_budget_flag_and_pool_on_line(monkeypatch):
    h = run(monkeypatch, s_m_cr=0.8, cr_budget='1')             # S_cr_b weight 0.5 < S_m -> pool 0.5 (+ min(2.0,0.8) from S_cr_a)
    assert 'pool=1.30' in h.written[-1]
    h0 = run(monkeypatch, s_m_cr=0.8, cr_budget='0')
    assert 'pool=0.00' in h0.written[-1]
    monkeypatch.setenv(bz.BETA_BUDGET_ENV, '0')                  # SPY flag off must not disable the crypto budget
    assert 'pool=1.30' in run(monkeypatch, s_m_cr=0.8, cr_budget='1').written[-1]

def test_equity_empty_floor_is_capped_not_nan(monkeypatch):
    h = run(monkeypatch, eq_carried=[], s_m_cr=-0.4, cr_rows=[_row('S_cr_a', 2.0)],
            cr_carried=[_carried('S_beta_btc', 'BTC-USD')])
    btc = _top_usd(h.written[-1], 'BTC-USD'); assert 0 < btc <= 0.25 * NAV + 1.0
```
Add `_top_usd(line, ticker)` helper (parse the `top=[…]` list with `ast.literal_eval`). `run()` must accept `cr_rows` (default `CR_ROWS`) — add the parameter. The pool value: `apply_beta_budget` pool = Σ over non-bench crypto tickers of `min(|S_adj|, S_m)`; the test harness stubs `_corr_adjusted_maps`? If S_adj differs from the raw weights in the harness, replace `1.30` by the value computed from the harness's size map — assert against a computed expectation, not a literal, if needed.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/execution/test_sizer_pooled_shadow.py -k "floor or beta_budget or gross_cap_shaves" -v`
Expected: FAIL — `floor_usd` absent from the line / BTC $ far below 0.10·NAV

- [ ] **Step 3: Implement** items 1–6 above in `src/execution/regime_blended_sizer.py`.

- [ ] **Step 4: Run the whole pooled-shadow file plus the hurdle-wiring file**

Run: `python3 -m pytest tests/execution/test_sizer_pooled_shadow.py tests/execution/test_sizer_benchmark_hurdle_wiring.py tests/execution/test_crypto_redeploy_pooled.py -q`
Expected: all PASS, including `test_pooled_shadow_leaves_equity_targets_identical` and `test_shadow_line_eq_scale_ratio`

- [ ] **Step 5: Commit**

```bash
git add src/execution/regime_blended_sizer.py tests/execution/test_sizer_pooled_shadow.py
git commit -F /tmp/msg_t3.txt   # "sizer: BTC sleeve floor (weight-injected, S_m-independent) + crypto beta budget on its own flag"
```

---

### Task 4: Per-class bench vector in the activation assigner

**Files:**
- Modify: `src/backtest/activation_assigner.py:127` (keys), `:169-247` (`stamp_last_applied`), `:250-323` (`stamp_bench_sharpe_config`), `:427-466` (`_load_pipeline_config_bench_vector`), `:529-635` (`load_bench_sharpe`), `:666-693` (`_fmt_bench_vector`, `_fmt_thresholds`), `:1130` (`_notify_botjohn_log`), `:1294-1324` + `:1365-1369` + `:1411-1430` + `:1469-1477` (`main`)
- Test: `tests/backtest/test_activation_assigner_multi_sleeve.py`, `tests/backtest/test_activation_assigner_bench.py`

**Interfaces:**
- Produces:
  - `CONFIG_KEY_BENCH_SHARPE_CRYPTO = 'strategy_activation_bench_sharpe_crypto'`
  - `_load_pipeline_config_bench_vector(conn, key: str = CONFIG_KEY_BENCH_SHARPE) -> dict`
  - `load_bench_sharpe(conn, sleeve_id=None, *, instrument_class: str = 'equity') -> tuple[dict, dict]`; meta gains `'instrument_class'`. For `instrument_class == 'crypto'`: if `resolve_bench_sleeve_id` returns `sleeve_source == 'literal_fallback'`, do **not** query that sleeve's run (`run_id=None`, `sleeve_id=None`) and `_log('WARN: no crypto benchmark sleeve in the registry; crypto bench from pipeline_config/default')`; tier 2 reads `CONFIG_KEY_BENCH_SHARPE_CRYPTO`.
  - `stamp_bench_sharpe_config(conn, bench, regime_source=None, *, key: str = CONFIG_KEY_BENCH_SHARPE) -> bool`
  - `stamp_last_applied(..., bench_by_class: Optional[dict] = None)` → payload key `bench_by_class` present only when given: `{'crypto': {'sleeve_id', 'bench_sharpe', 'bench_run_id', 'bench_regime_source'}}`.
  - `_fmt_bench_vector(bench, meta)` → prefix `bench[crypto]: ` when `meta.get('instrument_class') == 'crypto'`, else `bench: ` (unchanged). `_fmt_thresholds(bench, excess, *, instrument_class='equity')` → `thresholds[crypto]: …` for crypto.
  - `_notify_botjohn_log(..., extra_lines: Optional[list] = None)` appends the crypto lines after the equity thresholds line.
  - `main()`: `_crypto_bench_ids = bench_ids − _equity_bench_ids`; when non-empty, `crypto_sleeve_id = 'S_beta_btc' if in set else sorted(...)[0]` and `crypto_vector, crypto_meta = load_bench_sharpe(conn, sleeve_id=crypto_sleeve_id, instrument_class='crypto')`; when empty but any manifest class is `'crypto'`, `load_bench_sharpe(conn, None, instrument_class='crypto')` (tier 2/3 path). Pass `bench=crypto_vector if classes.get(sid, 'equity') == 'crypto' else bench_vector` to `apply_one`. `display_threshold`, header, summary, `bench:`/`bench diff:`/`thresholds:` lines unchanged. Stamps: `bench_by_class={'crypto': {...}}` when a crypto vector was built; `stamp_bench_sharpe_config(conn, crypto_vector, regime_source=crypto_meta['regime_source'], key=CONFIG_KEY_BENCH_SHARPE_CRYPTO)`.

- [ ] **Step 1: Write the failing tests** (in `tests/backtest/test_activation_assigner_multi_sleeve.py`, reusing `_Cur`/`_Conn`/`_run_main`; add crypto-run rows to the conn stub: `S_beta_btc` run `rb` with regimes LOW 0.86 / TRANS 0.23 / HIGH −0.49 / CRISIS 0.60; `S_beta_spy` run `rs` with 0.9378 / 0.4299 / 0.5326 / 1.575)

```python
class TestPerClassBenchVector(unittest.TestCase):
    def test_crypto_strategy_judged_against_crypto_sleeve_vector(self):
        calls = _run_main(bench_ids={'S_beta_spy', 'S_beta_btc'}, params=PARAMS,
                          classes={'S_alpha': 'equity', 'S_btc_x': 'crypto'}, sids=('S_alpha', 'S_btc_x'))
        self.assertEqual(calls['S_btc_x']['bench']['TRANSITIONING'], 0.23)
        self.assertEqual(calls['S_alpha']['bench']['TRANSITIONING'], 0.4299)
    def test_crypto_vector_without_crypto_sleeve_never_uses_equity(self):
        calls, out = _run_main_capture(bench_ids={'S_beta_spy'}, params=PARAMS,
                                       classes={'S_btc_x': 'crypto'}, sids=('S_btc_x',), config={})
        self.assertEqual(calls['S_btc_x']['bench'], {r: 0.5 for r in CANONICAL_REGIMES})
        self.assertIn('WARN: no crypto benchmark sleeve', out)
    def test_crypto_tier2_reads_crypto_key_only(self):
        calls, _ = _run_main_capture(bench_ids={'S_beta_spy'}, params=PARAMS, classes={'S_btc_x': 'crypto'},
                                     sids=('S_btc_x',),
                                     config={'strategy_activation_bench_sharpe': '{"LOW_VOL": 9.0}',
                                             'strategy_activation_bench_sharpe_crypto': '{"LOW_VOL": 0.7}'})
        self.assertEqual(calls['S_btc_x']['bench']['LOW_VOL'], 0.7)
    def test_log_lines_and_stamps(self):
        _, out, stamps = _run_main_capture(bench_ids={'S_beta_spy', 'S_beta_btc'}, params=PARAMS,
                                           classes={'S_btc_x': 'crypto'}, sids=('S_btc_x',), all_apply=True)
        self.assertIn('[activation_assigner] bench[crypto]: sleeve=S_beta_btc source=registry run=rb LOW_VOL=0.86', out)
        self.assertIn('[activation_assigner] thresholds[crypto]: LOW_VOL=0.86 ', out)
        self.assertEqual(out.count('[activation_assigner] bench: sleeve='), 1)     # equity line still once
        la = json.loads(stamps['strategy_activation_last_applied'])
        self.assertEqual(la['bench_by_class']['crypto']['bench_run_id'], 'rb')
        self.assertEqual(la['bench_sharpe']['LOW_VOL'], 0.9378)                    # equity keys untouched
        self.assertEqual(json.loads(stamps['strategy_activation_bench_sharpe_crypto'])['CRISIS'], 0.6)
```
Also assert in the existing equity tests that the `bench:`/`thresholds:` lines are byte-identical to before (they already pin them; keep them passing).

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/backtest/test_activation_assigner_multi_sleeve.py -v`
Expected: FAIL — `load_bench_sharpe() got an unexpected keyword 'instrument_class'` / crypto strategy sees 0.4299

- [ ] **Step 3: Implement** the interface list above in `src/backtest/activation_assigner.py`.

- [ ] **Step 4: Run the assigner suites**

Run: `python3 -m pytest tests/backtest/test_activation_assigner.py tests/backtest/test_activation_assigner_bench.py tests/backtest/test_activation_assigner_excess.py tests/backtest/test_activation_assigner_main.py tests/backtest/test_activation_assigner_multi_sleeve.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/backtest/activation_assigner.py tests/backtest/test_activation_assigner_multi_sleeve.py
git commit -F /tmp/msg_t4.txt   # "activation_assigner: per-class bench vector (crypto sleeve run for crypto-class strategies; never SPY)"
```

---

### Task 5: Pending check learns the crypto sleeve run

**Files:**
- Modify: `src/execution/activation_apply.py:102-140` (`_bench_sleeve_run_id`), `:258-270` (`pending_state`)
- Test: `tests/execution/test_activation_apply.py`

**Interfaces:**
- Consumes: Task 4 marker key `bench_by_class.crypto.bench_run_id`; `resolve_bench_sleeve_id(conn, sleeve_id=None, instrument_class)` returning `(id, source)`.
- Produces: `_bench_sleeve_run_id(conn, *, instrument_class: str = 'equity') -> Optional[str]` — for `'crypto'`, returns `None` when `source == 'literal_fallback'` (no crypto sleeve). `pending_state` adds `out['crypto_bench_run_id']` and a reason `crypto benchmark sleeve primary run {id!r} != last-applied bench_by_class.crypto.bench_run_id {marker!r}` when they differ **and** a crypto run exists; a marker without `bench_by_class` while a crypto sleeve run exists ⇒ pending.

- [ ] **Step 1: Write the failing tests** (append to `tests/execution/test_activation_apply.py`, same stub style as `test_pending_when_bench_sleeve_run_id_differs_from_marker`)

```python
def test_pending_when_crypto_sleeve_run_differs_from_marker():
    st = _pending(marker={'bench_run_id': 'rs', 'bench_by_class': {'crypto': {'bench_run_id': 'rb_old'}}},
                  sleeve_runs={'equity': 'rs', 'crypto': 'rb_new'})
    assert st['pending'] and any('crypto benchmark sleeve' in r for r in st['reasons'])
def test_not_pending_when_both_sleeve_runs_match_marker():
    st = _pending(marker={'bench_run_id': 'rs', 'bench_by_class': {'crypto': {'bench_run_id': 'rb'}}},
                  sleeve_runs={'equity': 'rs', 'crypto': 'rb'})
    assert not any('benchmark sleeve' in r for r in st['reasons'])
def test_no_crypto_sleeve_in_registry_is_not_pending_on_that_check():
    st = _pending(marker={'bench_run_id': 'rs'}, sleeve_runs={'equity': 'rs', 'crypto': None})
    assert not any('crypto benchmark sleeve' in r for r in st['reasons'])
```
(`_pending(...)` = a small local helper that monkeypatches `resolve_bench_sleeve_id` per class and the runs query; write it in the test module.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/execution/test_activation_apply.py -k crypto -v`
Expected: FAIL — no crypto reason produced

- [ ] **Step 3: Implement** in `src/execution/activation_apply.py`.

- [ ] **Step 4: Run the file**

Run: `python3 -m pytest tests/execution/test_activation_apply.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/execution/activation_apply.py tests/execution/test_activation_apply.py
git commit -F /tmp/msg_t5.txt   # "activation_apply: crypto sleeve run marks activation pending (bench_by_class)"
```

---

### Task 6: Node consumers — promotion bench leg per class + bench card payload + parser safety

**Files:**
- Modify: `src/lib/promotion_service.js:46-110, 247-302` ; `src/channels/api/activation_preview.js:343-392` (`benchCardPayload`)
- Test: `tests/lib/promotion_bench_leg.test.js`, `tests/test_activation_api.test.js`

**Interfaces:**
- Consumes: Task 4 keys `strategy_activation_bench_sharpe_crypto`, marker `bench_by_class`; stdout lines `bench[crypto]: …`, `thresholds[crypto]: …`.
- Produces:
  - `const BENCH_KEY_CRYPTO = 'strategy_activation_bench_sharpe_crypto'`; `_loadBenchVector(dbQuery, instrumentClass)` reads `BENCH_KEY_CRYPTO` when `instrumentClass === 'crypto'`, else `BENCH_KEY`; the WARN names the key. `_benchContext(dbQuery, sid, instrumentClass)`; both callers (`computeQualifyingRegimes`, `evaluatePromotionGate`) pass their `instrumentClass`.
  - `benchCardPayload` returns an additional `bench_by_class: marker.bench_by_class || null` (object or null).
  - Parser (`parseAssignerOutput` or equivalent in `activation_preview.js`): no code change expected; the test proves the new lines are ignored.

- [ ] **Step 1: Write the failing tests**

`tests/lib/promotion_bench_leg.test.js` (append, same harness):
```js
test('crypto-class gate reads the crypto bench key, never the equity key', async () => {
  const cfg = { strategy_activation_bench_sharpe: JSON.stringify({LOW_VOL: 9, TRANSITIONING: 9, HIGH_VOL: 9, CRISIS: 9}),
                strategy_activation_bench_sharpe_crypto: JSON.stringify({LOW_VOL: 0.86, TRANSITIONING: 0.23, HIGH_VOL: -0.49, CRISIS: 0.60}) };
  const r = await computeQualifyingRegimes({ dbQuery: stubDb({ cfg, sleeves: {TRANSITIONING: {sharpe: 0.9, max_dd_pct: 10, trade_count: 300}} }),
                                             sid: 'S_btc_x', instrumentClass: 'crypto' });
  assert.deepEqual([...r.keys ? r.keys() : Object.keys(r)], ['TRANSITIONING']);
});
test('crypto-class gate without the crypto key falls back to 0.5 with a WARN naming the crypto key', async () => { /* expect warn text to include 'strategy_activation_bench_sharpe_crypto' and the 0.9 sleeve to qualify */ });
```
`tests/test_activation_api.test.js` (append):
```js
test('crypto bench lines are ignored by the equity parser', () => {
  const out = [
    '[activation_assigner] threshold=0.9378 min_trades=100 dry_run=True strategies=1 excess=0.0',
    '[activation_assigner] bench: sleeve=S_beta_spy source=registry run=rs LOW_VOL=0.9378 TRANSITIONING=0.4299 HIGH_VOL=0.5326 CRISIS=1.575',
    '[activation_assigner] bench[crypto]: sleeve=S_beta_btc source=registry run=rb LOW_VOL=0.86 TRANSITIONING=0.23 HIGH_VOL=-0.49 CRISIS=0.6',
    '[activation_assigner] thresholds: LOW_VOL=0.9378 TRANSITIONING=0.4299 HIGH_VOL=0.5326 CRISIS=1.575 (excess=0.0)',
    '[activation_assigner] thresholds[crypto]: LOW_VOL=0.86 TRANSITIONING=0.23 HIGH_VOL=-0.49 CRISIS=0.6 (excess=0.0)',
    '[activation_assigner]   S_btc_x: LOW_VOL: False->False, TRANSITIONING: False->True, HIGH_VOL: False->False, CRISIS: False->False',
    '[activation_assigner] activation_assigner summary: 1 strategies evaluated, 0 skipped (no corrected backtest), 1 cell(s) activated, 0 cell(s) deactivated, 0 newly-dormant strategies, threshold=0.9378, min_trades=100, dry_run=True, errors=0',
  ].join('\n');
  const p = parseAssignerOutput(out);
  assert.equal(p.bench.sleeve, 'S_beta_spy'); assert.equal(p.strategies.length, 1); assert.equal(p.thresholds.LOW_VOL, 0.9378);
});
test('benchCardPayload carries bench_by_class', () => {
  const marker = { value: JSON.stringify({ bench_sharpe: {LOW_VOL: 1}, bench_by_class: { crypto: { bench_run_id: 'rb' } } }) };
  assert.equal(benchCardPayload(marker, null).bench_by_class.crypto.bench_run_id, 'rb');
  assert.equal(benchCardPayload({ value: JSON.stringify({ bench_sharpe: {LOW_VOL: 1} }) }, null).bench_by_class, null);
});
```
(Use the parser's real exported name and result shape from `activation_preview.js`; adjust property names to what the module exports — the assertions' intent is fixed: equity bench sleeve stays `S_beta_spy`, one strategy counted, thresholds unchanged.)

- [ ] **Step 2: Run to verify they fail**

Run: `node --test tests/lib/promotion_bench_leg.test.js tests/test_activation_api.test.js`
Expected: FAIL — crypto gate reads 9.0 (nothing qualifies); `bench_by_class` undefined

- [ ] **Step 3: Implement** in `promotion_service.js` and `activation_preview.js`.

- [ ] **Step 4: Run the node tests**

Run: `node --test tests/lib/promotion_bench_leg.test.js tests/test_activation_api.test.js tests/test_promotion_service_gate.js tests/test_transition_route_refactor.js`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/lib/promotion_service.js src/channels/api/activation_preview.js tests/lib/promotion_bench_leg.test.js tests/test_activation_api.test.js
git commit -F /tmp/msg_t6.txt   # "promotion/dashboard: crypto-class bench key + bench_by_class payload; parser ignores bench[crypto] lines"
```

---

### Task 7: Docs — spec §7d as-built, `.env.example`, changelog

**Files:**
- Modify: `docs/specs/2026-11-06-crypto-engine-whale-flow-design.md` (new `## 7d. As built — BTC sleeve floor + per-class activation bench (2026-11-18)` after §7c), `.env.example` (add `# OPENCLAW_CRYPTO_BETA_BUDGET=1` with a one-line comment next to the other crypto flags), `docs/archive/changelog.md` (newest-first entry)

- [ ] **Step 1: Write §7d** — bullets: floor key/default/order-of-operations, `OPENCLAW_CRYPTO_BETA_BUDGET` semantics, the two line fields, the per-class bench (keys, lines, stamps, pending check, promotion leg), expected first effects (halving CRISIS ON; `floor_usd≈0.10·NAV` on the shadow line), rollout notes (daily cycle live for activation; sizer shadow; johnbot restart for the promotion service).
- [ ] **Step 2: Add the `.env.example` line and the changelog entry** (cite the task commits' subjects).
- [ ] **Step 3: Verify** `python3 -c "import ast,sys; ast.parse(open('src/execution/regime_blended_sizer.py').read())"` and `git diff --stat` shows only the three doc files.
- [ ] **Step 4: Commit**

```bash
git add docs/specs/2026-11-06-crypto-engine-whale-flow-design.md .env.example docs/archive/changelog.md
git commit -F /tmp/msg_t7.txt   # "docs: §7d as-built — BTC sleeve floor + per-class activation bench"
```

---

## Self-review notes

- Spec coverage: §5.1 → Tasks 4, 5, 6; §5.2 → Tasks 1, 2, 3; §5.3 rollout → controller after merge (ledger/CLAUDE.md/memory are the controller's, not a task); §6 builder items → Task 2 (algebra), Task 3 (detection), Task 2 (`protect` on `crypto_gross_cap`), Task 6 (payload only).
- Names used consistently: `floor_weight`, `crypto_beta_budget_enabled`, `benchmark_min_nav_frac`, `CONFIG_KEY_BENCH_SHARPE_CRYPTO`, `bench_by_class`, `BENCH_KEY_CRYPTO`, `_bench_sleeve_run_id(conn, *, instrument_class)`.
- Review Focus items 1–5 each have a named test in Tasks 1–4 and 6.
