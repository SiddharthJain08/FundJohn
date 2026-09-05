# Spec — Options surface features (shared live/backtest), CBOE open interest, macro risk-free, NYSE session calendar

**Status:** APPROVED by operator 2026-09-04 (F1–F4 from the financepy fit review; F3 series choice and the Defect-A stopgap delegated to BotJohn; F5–F7 deferred until after this lands).
**Origin:** financepy fit review 2026-09-04 (artifact `19c2ce71`; learnings ERR-20260904-001/002, LRN-20260904-001).
**Grounding:** every symbol, line and number below was read or measured on `/root/openclaw` main (`791adfb1`) and `options_eod.parquet` session 2026-09-03 on 2026-09-04. Line numbers drift; symbol names are the stable reference.

---

## 0. Why

Four live strategies (S21_iv_hv_spread, S_HV8_gamma_theta_carry, S_HV19_iv_surface_tilt, S_HV20_iv_dispersion_reversion) and one candidate (S_pre_earnings_vol_runup) were qualified on backtest options features that the live path does not reproduce:

| Feature | Live (`engine.load_aux_data`) | Backtest (`build_options_aggregates` + `compute_rolling_options_fields`) |
|---|---|---|
| `iv_rank` | gated on a non-empty OI-by-strike map; OI is 100 % NULL for the whole options_eod history ⇒ **constant 50.0 for every ticker, silently** | 252-session percentile of `iv_front` (min 20 obs) |
| `iv30` | mean IV of **all strikes of the nearest expiry** (on a Thursday: the 1-DTE chain, 71 % null IV). SPY 0.402 / AAPL 0.502 / XOM 0.530 on 09-03 | `iv_front` = |Δ| .40–.60 mean of the front ≤45d expiry. SPY 0.125 / 0.264 / 0.291 |
| `ts_ratio` | near/far | `iv_back / iv_front` (far/near) |
| `iv_spread` | ATM call IV − ATM put IV, front month | alias of `term_slope` |
| `pc_ratio` | put/call volume, whole chain, latest date | front expiry only |
| `rv_20` | pct-change std × √252 | log-return std × √252 |
| histories | last 8 | last 20 |
| ATM strike (when OI exists) | nearest to `chain['close'].iloc[0]` = an option premium | n/a |

The operator's 2026-08-07 ruling makes the backtest side authoritative. The 08-07 parity audit already listed "iv_rank computed over 14d but labelled 30d"; nothing was changed. This spec removes the two-definition problem by construction: **one function, one history table, both consumers.**

Three further findings ride along because they touch the same code paths: CBOE open interest banked since 08-21 and read by nothing (F2), a 5 % risk-free hardcoded at six sites while macro.parquet carries the full bill curve (F3), and no NYSE session calendar anywhere (F4).

---

## Part A — F1 Options surface features

### A.1 Module

`src/strategies/options_surface.py` — pure functions, pandas + scipy only, deterministic (no randomness, no environment reads except the version constant). Importable from `execution.engine`, `scripts/*`, and tests via the existing `sys.path` convention (`ROOT` and `ROOT/src`).

```
OPTIONS_FEATURES_VERSION = 2

prepare_chain(df, as_of) -> DataFrame      # shared filters (A.3)
fit_smile(strikes, ivs, spot) -> SmileFit | None
constant_maturity(fits: dict[int, SmileFit], target_dte: int) -> CMPoint | None
features_for_day(chain, spot, as_of) -> dict            # per-(ticker, date) row, A.4
series_features(row, history: DataFrame) -> dict        # iv_rank, vrp_zscore, histories, A.5
```

### A.2 Inputs

Per (ticker, date) the contract rows of `options_eod.parquet` (live: the 14-day window read + tier-1 intraday overlay, today-dated rows winning, exactly as today; backtest: the session's rows). `spot` is the underlying's close for that date from `prices.parquet` (backtest: `_attach_spot` semantics; live: the panel's last close for the ticker, which under same-day execution is the close-proxy row). `chain['close']` is never used as spot.

### A.3 Shared chain preparation (`prepare_chain`)

1. Drop rows whose four greeks are all zero (existing `_drop_zero_greeks` semantics).
2. `dte = (expiry − as_of).days`; keep `1 ≤ dte ≤ 120` (the intraday overlay's `MAX_DTE` band is 100; the extra 20 days only feed the 90-day point).
3. Normalise `option_type` to upper.
4. For smile fitting only: `implied_volatility > 0.01` and, when `delta` is present and non-zero, `0.05 ≤ |delta| ≤ 0.95`.

### A.4 Per-day features (`features_for_day`)

**Smile per expiry.** For each expiry with `7 ≤ dte ≤ 120`: one IV per strike, taking the OTM side (put if `K < spot`, call if `K > spot`, mean of both if equal); require ≥ 5 strikes and `k_min < 0 < k_max` where `k = ln(K / spot)`; fit `scipy.interpolate.PchipInterpolator(k, iv)`. Log spot-moneyness, `r = q = 0` (a 4 % rate over 30 days shifts the forward by 0.3 %, which moves ATM IV by < 0.002 on a typical smile; documented, not modelled — keeps the feature path free of the macro loader).

**Per-expiry points.** `atm_iv = smile(0)`; `iv_25d_put` and `iv_25d_call` at the 25-delta strikes solved by two fixed-point iterations of `x = −d1·σ√T + σ²T/2` with `d1 = Φ⁻¹(0.75)` (put) / `Φ⁻¹(0.25)` (call), `σ = smile(x)`, clamped to `[k_min, k_max]`.

**Constant maturity.** Target 30 and 90 calendar days. Interpolate linearly in total variance `σ²·T` between the two bracketing expiries; if only one side exists, use the nearest expiry when `|dte − target| ≤ 10`, else `None`. Applied to `atm_iv`, `iv_25d_put`, `iv_25d_call`.

**Row keys** (same names the strategies read today; new definition where marked ★):

| key | definition |
|---|---|
| `iv30` ★ | CM-30d ATM IV |
| `iv90` (new) | CM-90d ATM IV |
| `near_iv`, `far_iv` ★ | aliases of `iv30`, `iv90` |
| `ts_ratio` ★ | `iv30 / iv90` (near / far; > 1 = inverted). Backtest orientation was the inverse; the only readers were deprecated cohort strategies. |
| `iv_25d_put_30d`, `iv_25d_call_30d` (new) | CM-30d wing points |
| `skew_25d_30d` (new) | `iv_25d_put_30d − iv30` |
| `rr_25d_30d` (new) | `iv_25d_put_30d − iv_25d_call_30d` |
| `skew_20d` ★ | alias of `skew_25d_30d` (readers: deprecated S_HV14 family) |
| `iv_spread` ★ | ATM call IV − ATM put IV on the **front usable expiry** (`1 ≤ dte ≤ 45`, |Δ| .40–.60 raw band), both sides; the backtest's `term_slope` alias is dropped |
| `term_slope` | `iv90 − iv30` |
| `gamma_atm`, `theta_atm` | front usable expiry, |Δ| .40–.60 mean — unchanged math, one implementation |
| `call_volume`, `put_volume`, `volume`, `pc_ratio` ★ | sums over the whole prepared chain for the date (live semantics); `pc_ratio = put_volume / call_volume` or `None` |
| `spot`, `last_price` | the underlying close |
| `expiry_date` | front usable expiry (ISO) |
| `n_expiries_fit`, `n_strikes_30d` (new) | diagnostics |
| `options_features_version` | 2 |

OI-derived keys (`gex`, `iv_centroid_delta`, `surface_premium`, `contracts_liquid`, `open_interest_by_strike`, `max_pain`, `pcr_oi`) come from Part B and are `None` when no CBOE session is available. They are never a computed 0.

### A.5 Series features (`series_features`)

Given the ticker's history rows from the surface master (`date < as_of`, last 260 rows) and today's row:

| key | definition |
|---|---|
| `rv_20` ★ | 20-session std of **log** returns × √252 from `prices.parquet` (the backtest definition) |
| `vrp` | `iv30 − rv_20` |
| `iv_rank` ★ | percentile rank (0–100) of today's `iv30` within the trailing 252 sessions including today, **`None` when fewer than 20 observations** (never 50.0). S_HV19's own `or 50.0` fallback is its business. |
| `vrp_zscore` | 60-session z-score, min 10 obs |
| `iv_rank_history`, `vrp_history`, `hv20_history` ★ | last 20 values (backtest length), oldest first |
| `pc_ratio_history` | last 20 `pc_ratio` |

### A.6 The surface master

`data/master/options_surface.parquet` — one row per (ticker, date) with every A.4 key that is a scalar (no histories), plus `features_version` and `built_at`. Append-only under the CLAUDE.md invariant: rows are added; a rebuild of the trailing window replaces rows only via the existing `append_dedup(replace)` pattern on (ticker, date); atomic tmp + `os.replace`. Registered in `sync_data_ledger` (`options_surface: alpaca`) and `master_freshness` (`('date', 5)`).

`iv_history.parquet` keeps running unchanged (append-only; its ATM-30d definition is superseded but not deleted).

### A.7 Consumers

**Backtest.** `scripts/build_options_surface.py` (replaces `build_options_aggregates.py` in the refresh runner; the old script and the monthly `options_aggregates/` files stay on disk, unread): chunked filtered reads of `options_eod` (5-day chunks, the columns A.3 needs plus `bid`/`ask` for later), per (ticker, date) `features_for_day`, spot attached from `prices.parquet`, rows upserted into the surface master. `scripts/compute_rolling_options_fields.py` reads the surface master instead of the monthly aggregates, computes A.5 per ticker, and writes `options_aggregates_enriched.parquet` with the existing `FIELDS` plus the new keys. `aux_data_loader.FIELDS` gains the new keys; the `skew_20d ← skew` alias line goes (the key is emitted directly). `scripts/refresh_options_aggregates.py` (06:05 ET Mon–Fri via `src/engine/cron-schedule.js:764`) runs the new stage 1 over `T−7..T−1` then stage 2.

**Live.** `engine.load_aux_data` replaces the inline per-ticker block (`_oi_missing_tickers` loop through the `opts_dict[ticker] = {...}` assembly) with: `features_for_day(prepared_chain_for_ticker, spot, today)` → `series_features(row, history_from_master)` → Part B OI keys → `earnings_dte`, `last_price`. The surface master is read once per run, filtered to the universe and `date ≥ today − 400d`. If the master already holds today's row (a post-16:30 re-run), the master row wins over the in-memory computation (mirrors the overlay precedence invariant).

**Shadow (one cycle).** Flag `OPENCLAW_OPTIONS_SURFACE` (default `0`): at `0` the engine serves the OLD dict and logs one line per run — `[options_surface] shadow n=<tickers> iv30 old/new median=<r> p90=<r> iv_rank_nonnull=<pct> version=2`; at `1` it serves the new dict. The backtest side switches on rebuild (no flag: the enriched panel is derived data). Flip after the first clean shadow line (Tue 2026-09-08 15:00 ET; Mon 09-07 is Labor Day). The previous enriched panel is copied to `data/derived/options_aggregates_enriched.v1-2026-09-04.parquet` before the first rebuild, for the diff.

### A.8 Re-backtest and re-gate

After the panel rebuild: `unified_backtest` for S21_iv_hv_spread, S_HV8_gamma_theta_carry, S_HV19_iv_surface_tilt, S_HV20_iv_dispersion_reversion, S_options_flow_confirmed_momentum (its `pc_ratio` definition changed) and S_pre_earnings_vol_runup — serial transient units, `Nice=19`, `MemoryMax=3500M`, outside the Saturday research window (12:00–24:00 UTC) and before the Monday 04:00 UTC weights timer. The activation and eligibility assigners then re-gate at their normal cadence (activation precedes weights, per the canonical-sequence runbook). No manual promotion or demotion.

### A.9 Tests

- `tests/strategies/test_options_surface.py`: synthetic SVI smile → `fit_smile` recovers ATM and 25Δ points within 1e-3; `constant_maturity` bracketing, one-sided ≤ 10 d, and `None` cases; `iv_rank` `None` below 20 obs and exact percentile above; OTM-side strike selection; put-call equal-strike averaging.
- `tests/strategies/test_options_surface_parity.py`: a checked-in fixture parquet of three tickers' 2026-09-03 chains (SPY, AAPL, XOM; ≤ 3,000 rows) → the engine's per-ticker path and the builder's per-(ticker, date) path produce identical dicts for every shared key, and the enriched-panel row for that date equals `series_features` on the same history.
- `tests/execution/test_engine_options_surface_shadow.py`: flag 0 serves old keys and logs the shadow line; flag 1 serves version-2 keys; master-row precedence.
- Existing `test_engine_intraday_options_overlay.py` and `test_engine_options_window.py` stay green.

---

## Part B — F2 CBOE open interest

### B.1 Source

`data/master/cboe_chains/date=<session>.parquet` (per contract: `open_interest`, `iv`, greeks, `underlying_price`, ~670k rows/session, 553 underlyings) and `data/master/cboe_chain_aggregates.parquet` (per underlying-day: `call_oi`, `put_oi`, `pcr_oi`, `gex`, `atm_iv`, `iv30`). Captured 17:00 ET Mon–Fri by `openclaw-cboe-chains.timer` since 2026-08-21; 11 sessions on 2026-09-04.

### B.2 Function

`src/strategies/options_oi.py::oi_features_for_day(cboe_rows_for_ticker, as_of) -> dict` (shared, pure):

| key | definition |
|---|---|
| `open_interest_by_strike` | front expiry (`1 ≤ dte ≤ 45`): call + put OI per strike, zero-OI strikes dropped |
| `max_pain` | strike minimising total intrinsic payout of that expiry's OI |
| `contracts_liquid` | count of contracts with OI > 0 on the front expiry |
| `gex` | `Σ(call gamma × OI) − Σ(put gamma × OI)` over the front expiry, × 100 (the engine's and builder's existing formula, now on real OI) |
| `pcr_oi` (new) | `put_oi / call_oi` over the whole session chain |
| `iv_centroid_delta`, `surface_premium` | vega·OI-weighted, existing formulas, on the CBOE chain's `iv`/`delta`/`vega` |
| `oi_session` (new) | the CBOE session date used |

### B.3 Point in time

Both consumers use the **latest CBOE session strictly before `as_of`'s close that is ≤ as_of** — for the 15:00 ET compute on T that is T−1 (T's capture lands at 17:00 ET); for the backtest bar T it is the session T−1 as well, by the same rule (`cboe_session_for(as_of) = max session ≤ as_of − 1 day`). Sessions before 2026-08-21 → every key `None`.

### B.4 Consumers

Live: engine reads the one relevant session file filtered to the universe's tickers (~50 MB) and calls B.2 per ticker after A.4. Backtest: `build_options_surface.py` stage 1 writes the B.2 scalars into the surface master rows (`open_interest_by_strike` is not persisted; the enriched panel carries `gex`, `pcr_oi`, `contracts_liquid`, `iv_centroid_delta`, `surface_premium`, `max_pain`). `options_aux_freshness` keeps its "no fabricated zero" guard and gains "OI present from CBOE for ≥ 400 tickers on the latest session".

### B.5 Owed, not in this build

S5_max_pain un-deprecation needs ≥ 60 CBOE sessions of history for a meaningful backtest (≈ 2026-11-12). S_HV16_gex_regime is absent from the manifest (no file); nothing to revive.

---

## Part C — F3 Macro risk-free

### C.1 Decision

Series **DGS3MO** (3-month constant-maturity Treasury; FRED keyless stream, daily since 1981-09; 3.89 % on 2026-09-03; the same series `S_ast_fed_model` already names). DGS1MO starts 2001-07 and adds nothing the gates need.

### C.2 Module

`src/backtest/risk_free.py`:

```
RISK_FREE_ANNUAL_CONST = 0.05
RF_SOURCE = os.environ.get('OPENCLAW_RF_SOURCE', 'const')   # 'const' | 'macro'
rf_daily_for(dates) -> np.ndarray      # DGS3MO/100/252 aligned to dates (ffill; first value back-filled)
rf_annual_asof(d) -> float
excess_sharpe(rets, dates=None, source=None, min_obs=2) -> float | None
```

`excess_sharpe = mean(r_t − rf_t) / std(r_t, ddof=1) × √252` — the standard deviation of the **raw** returns, so that `source='const'` reproduces today's formula bit-for-bit at every site. When `dates` is `None`, `rf_t` is the value as of the run date (bench_realized's 20-day window).

macro.parquet is read once per process (`lru_cache`), columns `date, series, value` filtered to `series == 'DGS3MO'`. If the file or series is missing the module logs a WARNING and falls back to the constant — never silently.

### C.3 Sites

| site | change |
|---|---|
| `unified_backtest.aggregate_metrics` (`:641`) | `excess_sharpe(daily_returns, _dates)`; `config_json.rf_source` and `rf_mean_annual` recorded per run |
| `benchmark_baseline._excess_sharpe` (`:118`) | takes the marked dates; `RISK_FREE_ANNUAL/DAILY` constants re-exported from `risk_free` so `test_risk_free_constant_matches_the_engine` keeps pinning one value |
| `bench_realized._sharpe` (`:99`) | `excess_sharpe(rets, dates)` over the NAV-history dates |
| `strategies/auto_backtest` (`:439`) | `excess_sharpe(daily_ret.values, daily_ret.index)` |
| `backtest/options_pricing.RISK_FREE` | `r` defaults to `rf_annual_asof(as_of)` when the caller passes a date, else the constant (engine dormant; parity script unaffected) |
| `execution/trade_handoff_builder.RISK_FREE_DAILY` | unused constant; re-exported from `risk_free` |
| `execution/benchmark_sizing` S_m cache | the cache key gains `rf_source` so a flip never serves a const-rf `S_m` |

### C.4 Shadow and flip

With `RF_SOURCE='const'` every site also computes the macro variant and logs `[rf_shadow] site=<name> const=<s1> macro=<s2> n=<obs> rf_mean=<annual>`; the backtest writes both into `config_json`. The flip is `OPENCLAW_RF_SOURCE=macro` in `.env` + user-scope johnbot restart, after ≥ 5 shadow lines from the live cycle and one from a backtest. **The fleet is not re-backtested by this build**: newly-run backtests carry `rf_source` in `config_json`, so mixed populations are identifiable; the runbook schedules the fleet re-run under macro rf as an operator-triggered weekend transient unit (`scripts/fleet_overnight_resume.sh` pattern), after which the assigners re-gate.

Expected magnitude: over 2023-09..2026 the bill ranged 3.62–5.63 %, so Sharpe levels move by at most ±0.1 for typical strategy vol; over 2016–2019 windows (bill 1.34 %) long-history Sharpes rise by ≈ 0.2–0.3 at 12 % vol.

---

## Part D — F4 NYSE session calendar

### D.1 Master

`data/master/trading_calendar.parquet` — one row per session: `date, open, close, session_open, session_close, settlement_date, active, source='alpaca', fetched_at`. Built by `src/ingestion/ingest_trading_calendar.py` from `alpaca calendar --start <y>-01-01 --end <y>-12-31` per year 1970..(today + 3 y); sessions that disappear upstream (an exchange-declared closure) are kept with `active=false`, never deleted. Refreshed by `openclaw-trading-calendar.timer` (monthly, 1st 06:00 UTC, `Persistent=true`), documented in `docs/systemd/`; `master_freshness` entry `('fetched_at', 45)`.

### D.2 Library

`src/lib/trading_calendar.py` (pure Python, master-first, no subprocess in the hot path):

```
is_session(d) -> bool
next_session(d) -> date          # strictly after d
prev_session(d) -> date          # strictly before d
sessions(start, end) -> list[date]
sessions_before(d, n) -> list[date]
is_open(now_et) -> bool          # RTH incl. early closes (close column)
expiry_session(third_friday) -> date   # the session on or before it
```

Fallback order when the master is missing: one `alpaca calendar` call for the requested range, then weekday arithmetic with a WARNING (`trading_calendar: master missing, weekday fallback`). Never silent.

### D.3 Sites

| site | replacement |
|---|---|
| `backtest/_trading_calendar.trading_days` | `sessions(start, end)` |
| `execution/engine._next_trading_day`, `_is_trading_session` | master-first, alpaca CLI second, weekday last |
| `execution/engine._panel_fresh_required` weekday test | `is_session(run_date)` |
| `execution/trade_handoff_builder:325` prev day | `prev_session` |
| `execution/option_hedge._next_trading_day` | `next_session` |
| `execution/alpaca_executor:277,298` session fallback | `is_session` + `is_open` |
| `backtest/options_pricing.nearest_monthly_expiry` | `expiry_session(third_friday)` |
| `ingestion/ingest_cboe_chains._prev_business_day`, `session_date_for` | `prev_session` / `is_session` |
| `ingestion/ingest_finra_short_interest._roll_back_weekend` | roll back to a session |
| `ingestion/ingest_nasdaq_earnings_calendar` day lists | `sessions_before` / `sessions` |
| `system_checks/checks/pipeline:49`, `acting_ingest_coverage:47`, `storage:44`, `fmp_provider_health:40`, `maintenance/doctor:664`, `scripts/run_intraday_market_state:137` | `is_session` (and `is_open` where RTH is meant) |
| `strategies/implementations/S_holiday_seasonality_energy_etf_tv1.py` | holiday set = weekdays that are not sessions; `_BDAY_US` → session arithmetic. **Strategy change ⇒ re-backtest** (`S_holiday_seasonality_energy_etf_tv1`, eligible LOW_VOL, Sharpe 0.61 on 08-29) and re-gate. `S_preholiday_effect` is checked for the same coupling in the plan. |

### D.4 Tests

`tests/lib/test_trading_calendar.py` on a fixture master (2019, 2025, 2026): Good Friday 2026-04-03 and Labor Day 2026-09-07 are not sessions; 2025-01-09 (national day of mourning) is not a session; `expiry_session(2019-04-19)` = 2019-04-18; `is_open` false at 13:30 ET on 2026-11-27; weekday fallback logs and returns. Site tests pin the holiday behaviour where one exists (`test_engine_equity_calendar`, executor session tests).

---

## E. Sequencing

1. **D** (calendar master + library + sites) and **C** (risk-free module + sites, shadow) — independent, small, first.
2. **A** (surface module, master, builder, live path behind the flag, parity tests, panel rebuild + v1 copy).
3. **B** (OI function, live + builder wiring, freshness guard).
4. Re-backtests (A.8 + D.3 holiday) as serial transient units in a quiet window; assigners at cadence.
5. Live shadow line Tue 2026-09-08 15:00 ET → flip `OPENCLAW_OPTIONS_SURFACE=1`; rf shadow ≥ 5 lines → operator flip + fleet re-run (runbook).

Each task is TDD (tests first), committed on `main` with the task id in the message; the tree currently carries uncommitted changes from another session in `pipeline_orchestrator.py`, `resolve_script.js`, `manifest.json`, `strategy_signatures.json` — those files are not touched or staged by this work.

## F. Out of scope

Solving IV from quotes for the 39 % of contracts Alpaca leaves null (F1b), F5 synthetic-engine upgrades, F6 model-free variance, F7 risk-neutral density: after this lands. Nothing in this spec installs or vendors financepy.

---

## Amendments 2026-09-05 (final review)

Rulings from the final whole-branch review, recorded here rather than by
rewriting the sections above.

1. **`config_json['rf']` is nested.** C.4 says "the backtest writes both into
   `config_json`"; the shape is a nested object, not flattened keys:
   `config_json['rf'] = {source, const, macro, rf_mean_annual, n}` (written by
   `unified_backtest` from `aggregate_metrics`'s `rf_shadow`). Queries
   distinguishing populations use `config_json->'rf'->>'source'`.

2. **The live `spot` is the last known close, and `rv_20` is mapped as-of.**
   A.4/A.5 left the live spot's date implicit. It is the last `prices.parquet`
   close at or before the chain date — T−1 at the 15:00 ET compute (the day's
   close does not exist yet) and T after the 16:15 collect. Two consequences,
   both now in the code:
   - `options_aux_v2.build` records `spot_date` per ticker, and the shadow line
     reports `spot_stale` (the share of tickers priced off a close older than
     the surface date). A high `spot_stale` at the intraday compute is EXPECTED,
     not a defect.
   - `series_features` maps `rv_20` **as-of** — the last realized-vol value at
     or before the frame date, tolerance 7 calendar days
     (`options_surface.RV_ASOF_TOLERANCE`) — not by exact date. Under the
     production intraday overlay the chain rows are dated today while
     `prices.parquet` ends at T−1, so the exact-date map returned NaN and
     silently dropped `rv_20`/`vrp`/`vrp_zscore` from the live v2 dict. The
     panel side (`build_panel`) is unaffected: every surface date there has a
     same-day close, and with a same-day close the as-of result is identical to
     the exact-date one (pinned by `test_options_surface_parity.py`).

3. **The surface master's version column is `options_features_version`.** A.6
   calls it `features_version`; the builder writes, and every consumer reads,
   `options_features_version` (the name in `options_surface.SCALAR_KEYS`).

4. **Shadow-mode cost budget.** `options_aux_v2.build` takes a per-run wall-clock
   budget, `OPENCLAW_OPTIONS_SURFACE_BUDGET_S` (default 240 s). With the flag OFF
   the dict is diagnostic only, so the loop stops at the budget and returns the
   partial dict with a WARNING; with the flag ON the dict is load-bearing, so it
   runs to completion and warns once. The shadow line carries `dur=<s>s`.
