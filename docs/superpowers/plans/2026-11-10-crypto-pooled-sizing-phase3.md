# Crypto Engine Phase 3 — Pooled Equity+Crypto Sizing with the BTC Sleeve: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Crypto names are sized inside the production regime-blended sizer alongside equities — one λ·NAV normaliser, the same per-ticker and cluster caps, a BTC benchmark sleeve whose weight is BTC's buy-and-hold Sharpe in the current *crypto* regime, every other crypto name hurdled by that Sharpe, BTC capped at 25 % NAV and crypto gross at 50 % NAV, crypto free to absorb the NAV that equities leave unused on risk-off equity days — shadow first, flipped by one flag.

**Architecture:** (1) a deterministic crypto regime tier table (`crypto_historical_regimes.parquet`) partitions crypto backtests and `S_m_crypto`; (2) `benchmark_baseline`/`benchmark_sizing` learn a second benchmark with its own calendar and a cache keyed by benchmark; (3) `S_beta_btc` is a clone of `S_beta_spy` for the crypto class; (4) the tradable universe gains crypto rows and every broker-position reader uses the Phase 1 symbol normaliser; (5) `_sharpe_cadence_path` reads every regime-keyed input per class, hurdles per class, caps per class, runs a funding-feasibility pass, and either prints a `crypto_sizing.shadow[…]` line (flag unset) or emits crypto orders (flag `=1`); (6) the crypto redeploy becomes a thin crypto-only re-run of the pooled sizer; (7) the dashboard shows the real `S_m` by crypto regime and the sizing mode.

**Tech Stack:** Python 3.13, pandas 3, pyarrow, duckdb (window reads), psycopg2, redis-py; Node 24 for the router/dashboard; pytest + `node --test`.

**Spec:** `docs/specs/2026-11-06-crypto-engine-whale-flow-design.md` §5 (5.1–5.4), §5.3c (hold-flat, delivered by Phase 2), §6.4/§6.6, §7 row P3, §9 decisions 4–5. Interface facts below were grep-verified on 2026-11-10.

## Global Constraints

- **Flag** `OPENCLAW_CRYPTO_POOLED_SIZING` (unset ⇒ **shadow**: the pooled crypto targets are computed and printed as `crypto_sizing.shadow[…]`, equity orders are emitted exactly as today, crypto orders are NOT emitted and the legacy `crypto_redeploy_sizer` keeps acting; `=1` ⇒ apply). Flip only after two clean shadow cycles reviewed by the operator.
- **Operator rules (spec §9)**: benchmark BTC-USD only; crypto per-ticker cap = the equity rule `0.10·(|S|+1)·λ·NAV`; the same cluster cap at the same levels; `pipeline_config.benchmark_max_nav_frac_crypto = 0.25`; `pipeline_config.crypto_gross_max_nav_frac = 0.50`; crypto fills the book by conviction and may absorb unused NAV on equity TRANSITIONING/HIGH_VOL/CRISIS days; the only funding constraint is non-marginable buying power (feasibility pass, crypto scaled pro-rata, logged `crypto_sizing.funding_bind=…`).
- **Per-class regime-of-record (spec §5.3a)**: equity tickers keep reading the equity `regime_state`/`params` exactly as today; crypto tickers read the crypto regime (`regime.crypto_regime.load_crypto_regime_state()['state']`, canonical labels) for weights (`strategy_weights.load_current(crypto_regime)`), signal eligibility, `min_acting_strategies`, `liquidity_param`, S_m, cadence reset, and the zero-conviction flatten. **Equity semantics must not change when the flag is unset** — the equity test suite (`tests/execution/test_sizer_*.py`, `test_regime_blended_sizer*.py`, `test_bench_corr_removal.py`, `test_asset_corr_cap_*.py`) must stay green unmodified except where a test's own fixture needs the new optional argument.
- **S_m semantics**: `S_m_crypto` = buy-and-hold BTC-USD daily excess returns (rf = `backtest.risk_free`, source `OPENCLAW_RF_SOURCE`) partitioned by `crypto_historical_regimes.parquet`, annualised with **365** trading days, horizons `(1,2,3,5,10,21)`, `min_obs 40`. For the crypto class only: hurdle uses `max(S_m_crypto, 0)` and the sleeve weight is `max(S_m_crypto, 0)`. SPY semantics untouched.
- **Cache**: `pipeline_config.benchmark_regime_sharpe` becomes schema 3: `{'schema': 3, 'rf_source': …, 'benchmarks': {'SPY': {as_of, start, horizons, by_regime}, 'BTC-USD': {...}}}`; a schema-2 payload is read as `benchmarks: {payload.benchmark: payload}` and rewritten as 3 on the next write. One benchmark's refresh never evicts another's.
- **Crypto regime tier table** (`src/strategies/crypto_historical_regimes.py`): BTC-USD daily closes from the prices master (hive read via `parquet_store.prices_read_table`, never a whole-file read), 30-day annualised realised vol (√365), 5-day rolling median, tiers `LOW_VOL < t1 ≤ TRANSITIONING < t2 ≤ HIGH_VOL < t3 ≤ CRISIS`; thresholds calibrated ONCE by `scripts/calibrate_crypto_regime_tiers.py` so the tier frequencies over the overlap window (2026-05-26 → run date) match `crypto_regime_states` frequencies (LOW_VOL 41 %, TRANSITIONING 32 %, CRISIS 16 %, HIGH_VOL 11 % as of 2026-11-10 — recompute at run time), then FROZEN as module constants `CRYPTO_RV_TIERS` with the calibration date in a comment. Output parquet `data/master/crypto_historical_regimes.parquet` columns `date, close, rv30, rv30_smoothed, regime`; written with `parquet_store.append_dedup(key_cols=['date'], mode='replace')` (NEVER-DELETE; add to the CLAUDE.md invariant list). Rebuilt daily after the prices collect (hook next to `historical_regimes` in `src/pipeline/collector.js:2552`).
- **Symbol forms**: internal `BTC-USD`; tradable-universe rows for crypto are stored with the INTERNAL symbol (`BTC-USD`) and `asset_class='crypto'` so `ticker in tradable_symbols` works unchanged; broker positions are normalised with `ingestion.crypto_flow.universe.normalize_broker_symbol` (handles `BTCUSD`, `BTC/USD`).
- **Account**: `execution.alpaca_trader._fetch_account_state` gains `non_marginable_buying_power` (float, None-safe); `account_state['equity']` stays the NAV.
- **Backtest**: `unified_backtest.load_regimes(instrument_class='equity')` returns the crypto tier series for `instrument_class == 'crypto'`; no rule-C mirror (ruling: none exists for equities either; recorded).
- **Tests**: targeted files only (`pytest <files> -q -W error`); never the full suite; no network/DB/Redis in tests (fakes/monkeypatch). Node: `node --check` + `node --test tests/test_routes_crypto.test.js tests/test_dashboard_crypto_page.test.js`.
- **Box**: 2-core/8 GB; the tier-table build reads one ticker's closes (tiny); the S_m compute for BTC is one ticker × 10 years (tiny).
- **Deployment**: johnbot user-scope restart only outside 15:00–16:30 ET and 22:00–23:30Z; migrations and the fleet re-gate are controller merge-phase steps.

## Review Focus

1. **Flag unset must be a no-op for equities.** Expected: byte-identical equity targets and orders versus today for the same inputs. Pinned in Task 8 (`test_pooled_shadow_leaves_equity_targets_identical`) by running the path twice (feature off vs the pre-change behaviour captured via the existing harness) and comparing `target_usd` for equity tickers.
2. **A crypto position at the broker while the flag is unset.** Expected: the pooled path must NOT emit an orphan close for it (today's D-1 bug); the legacy crypto lane owns it. Pinned in Task 5 (`test_orphan_pass_ignores_crypto_positions_when_not_pooled`).
3. **Negative S_m_crypto in CRISIS.** Expected: alt hurdle uses 0 (alts keep their raw conviction), sleeve weight 0, no negative weight anywhere, SPY untouched. Pinned in Task 2 (`test_crypto_hurdle_floors_negative_s_m`) and Task 3 (`test_sleeve_weight_floors_at_zero`).
4. **Funding bind.** Expected: with equity gross near 2×NAV and crypto conviction at the cap, crypto targets scale down pro-rata to `non_marginable_buying_power`, the shortfall is logged, equity targets unchanged. Pinned in Task 8 (`test_funding_pass_scales_crypto_only`).
5. **Hold-flat from Phase 2.** Expected: with `crypto_breaker:hold_flat` set, every crypto target is 0 (sleeve included) even when the flag is `=1`; equities unaffected. Pinned in Task 8 (`test_hold_flat_zeroes_crypto_targets_only`).

---

### Task 1: Crypto regime tier table (fixes D-2)

**Files:**
- Create: `src/strategies/crypto_historical_regimes.py`, `scripts/calibrate_crypto_regime_tiers.py`
- Modify: `src/backtest/unified_backtest.py:375` (`load_regimes`) and its call at `:1768`; `src/pipeline/collector.js:2552` (daily rebuild hook); `CLAUDE.md` invariant sentence; `src/system_checks/checks/master_freshness.py` `_CADENCES` (`'crypto_historical_regimes.parquet': ('date', 5)`); `src/strategies/sync_data_ledger.py`
- Test: `tests/strategies/test_crypto_historical_regimes.py`, `tests/backtest/test_unified_backtest_regime_source.py`

**Interfaces:**
```python
CACHE = ROOT/'data'/'master'/'crypto_historical_regimes.parquet'
BENCH = 'BTC-USD'; RV_WINDOW = 30; SMOOTH_DAYS = 5; ANNUALISE = 365
CRYPTO_RV_TIERS: tuple[tuple[float, str], ...]  # ((t1,'LOW_VOL'),(t2,'TRANSITIONING'),(t3,'HIGH_VOL')) — frozen after calibration; above t3 → CRISIS
def load_btc_closes(start=None, end=None) -> pd.Series            # prices master hive read, ticker=='BTC-USD'
def classify_history(start_date=None, end_date=None, *, closes: pd.Series | None = None, tiers=CRYPTO_RV_TIERS) -> pd.DataFrame  # date, close, rv30, rv30_smoothed, regime ('UNKNOWN' for the first 29 rows)
def rebuild_cache(start_date=None, end_date=None) -> Path          # append_dedup(key=['date'], mode='replace')
def regime_series(dates) -> pd.Series; def regime_for_date(d) -> str
def calibrate_tiers(rv_smoothed: pd.Series, target_freq: dict[str, float]) -> tuple[float, float, float]  # quantile cut-points so tier frequencies match target over the series' index window
# unified_backtest:
def load_regimes(instrument_class: str = 'equity') -> pd.Series   # crypto → crypto_historical_regimes.regime_series over its CACHE
```
- [ ] **Step 1: Failing tests** — synthetic closes (geometric random walk with two vol segments) → `classify_history` has `UNKNOWN` for the first 29 rows, canonical labels after, higher-vol segment maps to higher tiers; `calibrate_tiers` returns cut-points whose empirical frequencies match a target within ±2 pp; `rebuild_cache(tmp)` is idempotent (second call same row count); `load_regimes('crypto')` returns a series with weekend dates present while `load_regimes('equity')` is unchanged (monkeypatch both readers).
- [ ] **Step 2:** fail. **Step 3:** implement; the calibration script prints the three cut-points and the matched frequencies and rewrites the `CRYPTO_RV_TIERS` constant in the module with a `# calibrated 2026-11-10 against crypto_regime_states 2026-05-26..` comment (it edits its own source via a marked block — say so in the docstring). **Run the calibration for real** (reads the DB for the HMM frequencies and the prices master): commit the frozen constants. Wire the collector hook (`node --check`). **Step 4:** pass. **Step 5:** commit `crypto-sizing: crypto regime tier table (calibrated to the live HMM), backtest partition by instrument class`

---

### Task 2: S_m for a second benchmark with its own calendar; cache keyed by benchmark

**Files:**
- Modify: `src/backtest/benchmark_baseline.py:44-45, :82-137, :232` ; `src/execution/benchmark_sizing.py:36-46, :140-160, :193-215, :255-300`; `src/execution/strategy_weights.py:709-814`; `src/execution/bench_realized.py:48-93` (SPY unchanged, but read via the new cache shape)
- Test: `tests/backtest/test_benchmark_baseline_crypto.py`, `tests/execution/test_benchmark_sizing_multi_bench.py`

**Interfaces:**
```python
# benchmark_baseline
def regime_benchmark_sharpe_by_horizon(start_date, end_date, benchmark='SPY', min_obs=40, horizons=BENCH_HORIZONS, record_shadow=True, *, regime_source: str = 'equity', trading_days: int | None = None) -> dict
    # regime_source 'equity' → historical_regimes.parquet (today); 'crypto' → crypto_historical_regimes.regime_series; trading_days default 252 for equity / 365 for crypto
# benchmark_sizing
CACHE_SCHEMA = 3
def regime_benchmark_sharpe_for_sizing(regime_state, run_date, *, benchmark='SPY', conn=None, compute=None, horizon=None, regime_source: str | None = None)  # regime_source defaults by benchmark: 'crypto' for 'BTC-USD'
def _read_cache(conn) -> dict  # upgrades schema 2 → {'schema':3,'benchmarks':{bench: payload}}
def _write_cache(conn, benchmark, payload)  # merges into benchmarks[benchmark]
def benchmark_max_nav_frac(default=1.0, conn=None, *, instrument_class='equity') -> float   # 'crypto' → key 'benchmark_max_nav_frac_crypto' default 0.25
def crypto_gross_max_nav_frac(default=0.50, conn=None) -> float   # key 'crypto_gross_max_nav_frac'
def apply_benchmark_hurdle(ticker_w, s_m, bench_tickers, *, floor_zero: bool = False) -> tuple[dict, list]   # floor_zero → uses max(s_m, 0)
# strategy_weights
def _load_benchmark_regime_sharpe(conn, regimes, *, benchmark='SPY') -> dict
def _benchmark_sleeve_effective_sharpe(strategy_id, regime, eff, bench_ids, s_m_by_regime, *, floor_zero=False)
```
  The sleeve weight call site (`strategy_weights.py:787-814`) resolves each sleeve's benchmark and class from the registry `parameters` (`benchmark_sleeve: true`, `instrument_class`, new `benchmark_ticker`) and uses the crypto regime labels for a crypto sleeve (weights table rows for the crypto class are keyed by the same four labels — the sizer picks the right row by passing the crypto regime).
- [ ] **Step 1: Failing tests** — `test_sharpe_by_horizon_crypto_uses_365_and_crypto_partition` (synthetic closes + synthetic crypto regime series via monkeypatch; assert √365 annualisation by comparing against a direct computation); `test_cache_schema3_roundtrip_and_schema2_upgrade`; `test_btc_refresh_does_not_evict_spy`; `test_crypto_hurdle_floors_negative_s_m` (s_m −0.3, floor_zero → alts unchanged); `test_benchmark_max_nav_frac_per_class_defaults` (0.25 crypto key absent → 0.25; equity key present → its value); `test_sleeve_weight_floors_at_zero`.
- [ ] **Step 2–5:** fail → implement → pass (plus the existing `tests/backtest/test_benchmark_baseline.py tests/execution/test_benchmark_sizing.py tests/execution/test_strategy_weights_benchmark_sleeve.py` green) → commit `crypto-sizing: S_m for BTC-USD on the crypto partition (365-day), benchmark-keyed cache (schema 3), per-class max NAV fraction`

---

### Task 3: `S_beta_btc` sleeve strategy + registration + always-on

**Files:**
- Create: `src/strategies/implementations/S_beta_btc.py`
- Modify: `src/strategies/manifest.json` (entry mirroring `S_beta_spy`'s with `instrument_class: 'crypto'`, `eligible_regimes` all four, `backtest_tickers: ['BTC-USD']`, `benchmark_sleeve: true`, state `candidate`), `src/backtest/activation_assigner.py:467, :1330` (always_on for EVERY sleeve id; `resolve_bench_sleeve_id` keeps returning the equity sleeve for the bench vector — add `instrument_class` filter param default 'equity'), `src/execution/activation_apply.py:102`
- Create: `scripts/register_s_beta_btc.py` (the registry INSERT from the 08-29 runbook shape with `parameters = {'benchmark_sleeve': True, 'instrument_class': 'crypto', 'benchmark_ticker': 'BTC-USD'}`, `universe = ARRAY['BTC-USD']`, status `pending_approval`; idempotent `ON CONFLICT (id) DO UPDATE`)
- Test: `tests/strategies/test_beta_btc.py`, `tests/backtest/test_activation_assigner_multi_sleeve.py`

**Interfaces:** `class BetaBtc(BaseStrategy)`: `id='S_beta_btc'`, `instrument_class='crypto'` (class attribute, like `S_btc_momentum.py:42`), `tier=2`, `signal_frequency='daily'`, `min_lookback=2`, `active_in_regimes=[all four]`, `MAX_SIGNALS=1`, `benchmark_sleeve=True`, `exit_hook=True`, `BENCHMARK='BTC-USD'`, `HOLD_DAYS=21` (ruling: same as SPY; the sizer re-targets daily anyway), `STOP_FRAC=0.60`, `TARGET_FRACS=(5.0,6.0,7.0)`; `generate_signals` emits one LONG `BTC-USD` signal with `signal_params={'hold_days', 'benchmark_sleeve': True, 'regime': regime['state']}`; `should_exit` returns `'regime_exit'` when the CRYPTO regime state differs from the one at entry (the engine passes the crypto regime to crypto strategies — confirm at `src/execution/engine.py:2269-2290` and note the exact call in the report).
- [ ] **Step 1: Failing tests** — clone `tests/strategies/test_beta_spy.py` + `test_beta_spy_exit_hook.py` for BTC; `test_assigner_always_on_for_every_sleeve` (two sleeve ids → both `benchmark_sleeve_always_on`); `test_resolve_bench_sleeve_id_prefers_equity_sleeve_for_bench_vector`.
- [ ] **Step 2–5:** fail → implement → pass → run `scripts/register_s_beta_btc.py --dry-run` (prints the SQL; the real insert is a merge-phase step) → commit `crypto-sizing: S_beta_btc benchmark sleeve (crypto class), multi-sleeve always-on, registration script`

---

### Task 4: Tradable universe gains crypto rows; consumers guarded

**Files:**
- Modify: `src/maintenance/refresh_tradable_universe.py:55-192`; `src/execution/regime_blended_sizer.py:739-741, :796-798, :895-897, :917-919, :2142-2180`; `src/execution/trade_handoff_builder.py:473`
- Test: `tests/maintenance/test_refresh_tradable_universe_crypto.py`, `tests/execution/test_trade_handoff_universe_filter.py` (extend), `tests/execution/test_sizer_universe_class_filter.py`

**Interfaces:** `fetch_alpaca_assets(timeout=60, asset_class='us_equity')`; `fetch_all_assets()` → equities + crypto (`alpaca asset list --asset-class crypto --status active`, symbols normalised `BTC/USD → BTC-USD`, `asset_class='crypto'`); `upsert_universe(conn, assets, dry_run=False)` deactivation UPDATE scoped `AND asset_class = ANY(%s)` to the classes present in this run; `load_tradable_symbols(conn, *, classes=('us_equity',)) -> set[str]` new shared helper in `src/execution/tradable_universe.py` used by both sizer loaders and the handoff builder; the sizer passes `classes=('us_equity','crypto') if pooled_sizing_enabled() else ('us_equity',)`; `_load_asset_eligibility`/`_apply_asset_eligibility_gate` skip symbols ending `-USD` (crypto is not marginable/borrowable by design) — add the skip next to the existing `/` skip at `:2178`.
- [ ] **Step 1: Failing tests** — `test_crypto_rows_inserted_with_internal_symbol_and_class`; `test_deactivation_scoped_by_class` (a crypto-only run must not deactivate equities and vice versa); `test_sizer_loader_filters_us_equity_when_not_pooled` / `_includes_crypto_when_pooled`; `test_handoff_loader_default_equity_only`; `test_eligibility_gate_skips_usd_pairs`.
- [ ] **Step 2–5:** fail → implement → pass → commit `crypto-sizing: tradable universe carries crypto rows (internal symbols), class-scoped deactivation, class-filtered consumers`

---

### Task 5: Broker-symbol normaliser everywhere (D-1) and the orphan pass

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:982-1028` (`_load_broker_positions_usd`), `:1073-1137` (`_classify_position_deltas`); `src/execution/crypto_redeploy_sizer.py:22-30`; `src/execution/open_reconcile.py:340`; `src/execution/account_breaker.py:224-260`; `src/execution/regime_liquidator.py:212-240`
- Test: `tests/execution/test_broker_symbol_normalisation.py`

**Interfaces:** `_load_broker_positions_usd()` returns keys in INTERNAL form for crypto (`BTC-USD`) and tags them: the dict value stays a float for compatibility, plus a parallel module-level helper `_broker_asset_classes: dict[str, str]` returned by a new `_load_broker_positions_usd_with_class() -> tuple[dict, dict]` (the old function wraps it); `_classify_position_deltas(..., pooled: bool = False)` skips the orphan-close pass for `asset_class == 'crypto'` keys when `pooled` is False (the legacy crypto lane owns them) — pinned by Review Focus 2.
- [ ] **Step 1: Failing tests** — both symbol forms normalise in each module (parametrised); `test_orphan_pass_ignores_crypto_positions_when_not_pooled`; `test_orphan_pass_includes_crypto_when_pooled`.
- [ ] **Step 2–5:** fail → implement → pass (existing `test_crypto_redeploy.py`, `test_account_breaker_*.py`, `test_regime_liquidator.py` green) → commit `crypto-sizing: one broker-symbol normaliser across sizer/redeploy/reconcile/breakers; orphan pass class-aware`

---

### Task 6: Account state + executor guards

**Files:**
- Modify: `src/execution/alpaca_trader.py:43` (`_fetch_account_state` adds `non_marginable_buying_power`, `buying_power`, `cash`), `src/execution/alpaca_executor.py:1300-1364` (`_route_crypto_order` buying-power guard for BUYs: refuse with `status 'refused', reason 'crypto_buying_power'` when `notional > non_marginable_buying_power` fetched once per process via `_fetch_account_state`; closes exempt), `src/execution/regime_blended_sizer_live.py:251-268` (class from ANY contributor, not only the first: if any contributor is crypto the order is crypto)
- Test: `tests/execution/test_executor_crypto_bp_guard.py`, `tests/execution/test_live_wrapper_instrument_class.py`
- [ ] **Step 1: Failing tests** — account parse with/without the field; guard refuses a $5k buy when nmbp is $1k, allows when $10k, allows closes regardless; live wrapper picks crypto class when the second contributor is crypto.
- [ ] **Step 2–5:** fail → implement → pass → commit `crypto-sizing: non-marginable buying power in account state, crypto buy guard in the executor, class from any contributor`

---

### Task 7: Per-class regime-of-record inside `_sharpe_cadence_path` (shadow-only wiring)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py` (`_sharpe_cadence_path` :1436–2106, `_size_positions_impl` :128–200, `_maybe_flatten_zero_conviction` :1375), `src/execution/regime_blended_sizer_live.py:546-570` (load the crypto regime + crypto params row and pass them through)
- Create: `src/execution/pooled_sizing.py` (pure helpers so the big function grows by calls, not by lines)
- Test: `tests/execution/test_pooled_sizing_helpers.py`, `tests/execution/test_sizer_pooled_shadow.py`

**Interfaces:**
```python
# pooled_sizing.py (pure)
def pooled_sizing_enabled(env=os.environ) -> bool
def ticker_classes(ticker_meta: dict, instrument_class_for) -> dict[str, str]     # 'crypto' if ANY contributor strategy is crypto else 'equity'
def split_by_class(d: dict, classes: dict[str, str]) -> tuple[dict, dict]         # (equity_part, crypto_part)
def load_crypto_regime_and_params(conn=None) -> tuple[str | None, dict | None]     # crypto state via load_crypto_regime_state; params = regime_sizer_params row for that state (same table, keyed by label)
def merge_weights(equity_rows: list[dict], crypto_rows: list[dict], crypto_strategy_ids: set[str]) -> list[dict]   # strategy rows: crypto ids take the crypto-regime row, others the equity row
def crypto_gross_cap(crypto_targets: dict, nav: float, frac: float) -> tuple[dict, float]   # pro-rata shave to frac·nav; returns (targets, shaved_usd)
def funding_pass(crypto_targets: dict, non_marginable_bp: float | None) -> tuple[dict, float]   # pro-rata scale when Σ|crypto| > nmbp; (targets, shortfall)
def shadow_line(crypto_regime, s_m_crypto, before: dict, after: dict, dropped: list, bench_tkrs: set, lam_nav: float, *, gross_cap_shaved: float, funding_shortfall: float, hold_flat: bool, mode: str) -> str   # 'crypto_sizing.{mode}[{regime}]: S_m=… n=… gross=… cap_shaved=… funding_bind=… hold=… …'
```
  Wiring in `_sharpe_cadence_path` (new kwargs `crypto_regime_state: str | None = None, crypto_params: dict | None = None`; when None the function behaves exactly as today — the whole crypto block is skipped): (a) weights — `_sw.load_current(regime_state)` for equity rows and `load_current(crypto_regime_state)` for crypto strategy ids, merged with `merge_weights`; (b) signal loaders — call `_load_approved_carried_signals`/`_load_active_window_signals` a second time with `regime_state=crypto_regime_state` restricted to crypto strategy ids and union (the loaders already take a regime kw; add a `strategy_ids=` filter kw to both); (c) `min_acting`/`liquidity_param` for crypto tickers from `crypto_params`; (d) S_m — `_s_m_eq` as today; `_s_m_cr = regime_benchmark_sharpe_for_sizing(crypto_regime_state, date.today(), benchmark='BTC-USD', horizon=_h)`; hurdle = `apply_benchmark_hurdle(eq_part, _s_m_eq, bench_eq)` + `apply_benchmark_hurdle(cr_part, _s_m_cr, bench_cr, floor_zero=True)`; beta budget per class (budget is OFF in production; keep the split anyway); (e) caps — unchanged formulas on the merged dict (bench exemptions from both sleeves); corr removal per class (`_apply_benchmark_corr_removal(eq_part, bench_eq, nav)` + crypto with `bench_cr`); D-4′ clamp per class with `benchmark_max_nav_frac(instrument_class=…)`; then `crypto_gross_cap(…, crypto_gross_max_nav_frac())`; (f) `funding_pass` with `account_state.get('non_marginable_buying_power')`; (g) hold-flat — `crypto_hold.hold_state()` (Phase 2) → zero crypto targets; (h) zero-conviction flatten evaluated per class (`_maybe_flatten_zero_conviction` gains `instrument_class` and a broker-book filter); (i) **emission**: if `pooled_sizing_enabled()` the crypto targets join `target_usd`; else they are dropped from `target_usd` after the shadow line is logged/posted (`_post_corr_cumsharpe_log` like `bench_sizing`).
- [ ] **Step 1: Failing tests** — pure helpers each; then the harness from `tests/execution/test_sizer_benchmark_hurdle_wiring.py` extended with crypto signals: `test_pooled_shadow_leaves_equity_targets_identical` (run with `crypto_regime_state=None` vs with crypto inputs but flag unset → equity `target_usd` equal, no crypto keys emitted, one `crypto_sizing.shadow` line logged), `test_pooled_apply_emits_crypto_targets`, `test_crypto_weights_read_crypto_regime_row`, `test_per_class_hurdle_uses_each_s_m`, `test_crypto_gross_cap_and_bench_cap`, `test_hold_flat_zeroes_crypto_targets_only`, `test_funding_pass_scales_crypto_only`, `test_zero_conviction_flatten_per_class`.
- [ ] **Step 2–5:** fail → implement → pass (ALL existing sizer tests green, unmodified) → commit `crypto-sizing: per-class regime-of-record, per-class hurdle/caps/clamp, crypto gross cap, funding pass, hold-flat, shadow line (flag-gated emission)`

---

### Task 8: Crypto redeploy becomes a thin re-run of the pooled sizer; cadence reset per class

**Files:**
- Modify: `src/execution/crypto_redeploy_sizer.py`, `scripts/redeploy_crypto.py`, `src/execution/engine.py:1743-1760` (`_stamp_cadence_reset_on_flip` class-scoped probe: compare the crypto regime against the newest `execution_signals.regime_state` of CRYPTO strategies only, and the equity regime against equity strategies' rows)
- Test: `tests/execution/test_crypto_redeploy_pooled.py`, `tests/execution/test_engine_cadence_reset_per_class.py`
- When `pooled_sizing_enabled()`: `size_crypto_positions` calls `regime_blended_sizer._size_positions_impl` restricted to crypto signals with the crypto regime and returns only crypto orders (equity orders discarded with a counter); else the legacy budget model (unchanged). Hold-flat honoured either way (Phase 2).
- [ ] **Step 1: Failing tests** — pooled branch returns crypto-only orders from a fake `_size_positions_impl`; legacy branch unchanged; cadence reset stamps crypto only on a crypto flip and equity only on an equity flip.
- [ ] **Step 2–5:** fail → implement → pass → commit `crypto-sizing: redeploy re-runs the pooled sizer for the crypto subset; cadence reset per class`

---

### Task 9: Dashboard — real S_m by crypto regime, sizing mode, funding bind

**Files:**
- Modify: `src/channels/api/routes_crypto.js` (`/api/crypto/risk` sizing block: `{status: 'shadow'|'apply', flag, benchmark:'BTC-USD', bench_cap_nav, gross_cap_nav, s_m_by_regime: {LOW_VOL:…, TRANSITIONING:…, HIGH_VOL:…, CRISIS:…} (from the schema-3 cache, horizon = benchmark_horizon_days), last_shadow_line: <from pipeline_config key 'crypto_sizing_last_line' written by the sizer>}`), `src/channels/api/server.js` (`S_m crypto by regime` tile shows the numbers; risk panel shows mode + last line), `src/execution/regime_blended_sizer.py` (write `pipeline_config.crypto_sizing_last_line` each cycle)
- Test: `tests/test_routes_crypto.test.js` (extend), `tests/test_dashboard_crypto_page.test.js` (extend)
- [ ] **Step 1–5:** fail → implement → `node --check`, node tests → commit `dashboard: crypto S_m by regime, pooled-sizing mode and last shadow line`

---

### Task 10 (controller, merge phase)
- [ ] Migrations none (pipeline_config keys only); `scripts/register_s_beta_btc.py --apply`; promote `S_beta_btc` through the normal path (backtest on the crypto partition → dashboard approve → `activation_apply`).
- [ ] Tradable-universe refresh with crypto (`python3 src/maintenance/refresh_tradable_universe.py`), verify 36 crypto rows, equities untouched.
- [ ] Build the tier table (`python3 src/strategies/crypto_historical_regimes.py --rebuild`), confirm `master_freshness` green; compute `S_m_crypto` once (`python3 -c` calling `regime_benchmark_sharpe_for_sizing(<state>, today, benchmark='BTC-USD')`) and read the four values back from the cache; sanity: CRISIS likely ≤ 0.
- [ ] Fleet re-gate of the crypto strategies on the crypto partition: a transient systemd unit, `nice -n 19`, one strategy at a time, outside market hours and the evening windows — the 7 crypto ids from `instrument_class_for`.
- [ ] johnbot restart outside the protected windows; first daily cycle prints `crypto_sizing.shadow[…]`; two clean shadow cycles (Mon/Tue) → operator flips `OPENCLAW_CRYPTO_POOLED_SIZING=1` → weights rebuild → floors recheck → activation (the canonical post-flip sequence).
- [ ] Changelog, spec §7a addendum, memory.

## Self-review notes (2026-11-10)
- Spec §5.1 (T3), §5.2 (T1, T2), §5.3 (T7), §5.3a (T7, T8), §5.3b (T6, T7), §5.3c (T7 via Phase 2's reader), §5.4.1 (T4), §5.4.2 (T5), §5.4.3 (T1 + T10 re-gate), §5.4.4 (T7, T8), §5.4.5 (T7 shadow), §5.4.6 — **ruling: no rule-C backtest mirror** (none exists for equities; the regime partition IS mirrored), §6.4/§6.6 (T9), §7 P3 (T10).
- Rulings recorded here: `S_beta_btc` holds 21 d like SPY; crypto tradable rows stored under the internal symbol; the orphan pass skips crypto unless pooled; `resolve_bench_sleeve_id` keeps the equity sleeve as the bench vector; funding pass is a pro-rata scale on crypto only; weights table rows for the crypto class are read by crypto regime label (no schema change).
- Types: `pooled_sizing.*` names, `apply_benchmark_hurdle(floor_zero=)`, `benchmark_max_nav_frac(instrument_class=)`, `load_regimes(instrument_class=)`, `_fetch_account_state()['non_marginable_buying_power']`, `crypto_hold.hold_state()` used consistently across tasks.
- Review Focus 1–5 pinned in T7 (1, 4, 5), T5 (2), T2/T3 (3).
