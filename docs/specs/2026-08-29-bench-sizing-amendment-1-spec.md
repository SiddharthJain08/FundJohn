# Spec — Benchmark-relative sizing, amendment 1: forward-looking `S_m`, regime-exit beta sleeve, always-on sleeve activation

**Status:** LANDED 2026-08-29 (`bcfaee2..191d4f2e`; final review clean; flag paused to SHADOW per D-E1, re-flip owed after two shadow cycles). Approved 21:55 UTC. Amends
`docs/specs/2026-08-29-benchmark-relative-sizing-spec.md` (D1–D9; code landed
`67afceb..78e5bbf`, flag flipped 20:36 UTC). Design approved in chat 2026-08-29
21:55 UTC ("A, horizon 1 day by default"; "(c), keep the beta sleeve active in all
regimes regardless of the activation slider"; "go ahead").

**Grounding:** every symbol below was verified against the working tree at
`c15fc89` on 2026-08-29. Line numbers drift; symbol names are the stable reference.

---

## 0. Findings that motivate this amendment (all reproduced, not asserted)

Verification scripts: `beta_sharpe_verify.py` / `beta_sharpe_verify2.py` in the
session scratchpad (throwaway; the numbers below are what they printed against
`data/derived/prices_spy_only.parquet` + `data/master/historical_regimes.parquet`,
window 2016-04-12..2026-08-28, N = 2,610 days).

**F1 — `S_m` today is a contemporaneous, rf = 0 statistic.**
`backtest.benchmark_baseline.regime_benchmark_sharpe` tags day *t* with
`historical_regimes.parquet` (5-day rolling-median VIX **through day t**,
`strategies.historical_regimes.SMOOTH_DAYS = 5`) and scores SPY's close-to-close
return **on day t**, mean/std·√252 with rf = 0 (its docstring says so). Because
VIX moves with the same day's SPY return (corr(SPY ret, ΔVIX) = −0.79 on this
window), "days that ended LOW_VOL" are selected on their own outcome. The label
is fine for tagging entries; it is not a return a close-of-day decision can earn.

**F2 — every alpha sleeve is excess over 5 %.** `unified_backtest.aggregate_metrics`
computes `sharpe = (daily_returns.mean() − RISK_FREE_DAILY) / std · √252` with
`RISK_FREE_DAILY = 0.05 / TRADING_DAYS_PER_YEAR` (`unified_backtest.py:68`; the
same constant is duplicated in `execution/trade_handoff_builder.py:39`), on the
equal-weight daily-marks equity curve of the trades entered in that regime
(`_portfolio_daily_returns`, true marks since `OPENCLAW_TRUE_MTM_MARKS` defaults
to `'1'`, `unified_backtest.py:766`). `S_adj` is built from those sleeves, so
`S_adj − S_m` today mixes an rf = 5 % numerator with an rf = 0 hurdle.

**F3 — the persisted `S_beta_spy` sleeves (run `d61deca4`) are arithmetically
correct under that convention.** Recomputed from `strategy_backtest_trades` with
true marks + rf 5 %: 0.231 / 0.179 / 0.714 / 1.166 vs persisted 0.202 / 0.155 /
0.694 / 1.149 (residual = `spread_v1` cost, ≈5.6 bp per SPY round trip measured
on the run). They answer "buy SPY at a regime-tagged close, hold 21 days through
whatever follows, excess over 5 %" — not "what beta earns while the regime holds".

**F4 — reconciliation ladder, LOW_VOL** (each step verified):

| statistic | Sharpe |
|---|---|
| contemporaneous, rf 0 (`benchmark_baseline` today; cached `S_m`) | 2.01 |
| contemporaneous, rf 5 % | 1.45 |
| label known at prior close, next-day hold, rf 5 % | 0.80 |
| entry-tagged, hold 5 / 10 days | 0.57 / 0.34 |
| entry-tagged, hold 21 (LOW_VOL spells: median 14 d; mark-days after the flip score −0.46) | 0.26 |
| + `spread_v1` cost = the persisted sleeve | 0.23 (0.20) |

**F5 — forward table (entry-tagged, daily-marks union, rf 5 %, no costs; ± = std
error):**

| regime | H=1 | H=2 | H=3 | H=5 | H=10 | H=21 | H=21 exit-on-flip | approved alpha sleeves' median hold |
|---|---|---|---|---|---|---|---|---|
| LOW_VOL | 0.80 ± .52 | 0.60 | 0.51 | 0.57 | 0.34 | 0.26 | 0.80 | 7.6 d |
| TRANSITIONING | 0.41 ± .47 | 0.25 | 0.29 | 0.42 | 0.52 | 0.20 | 0.41 | 6.0 d |
| HIGH_VOL | 0.49 ± .78 | 0.59 | 0.87 | 1.11 | 0.63 | 0.73 | 0.49 | 4.5 d |
| CRISIS | 1.54 ± 1.30 | 1.40 | 1.16 | 1.30 | 1.35 | 1.18 | 1.54 | 3.2 d |

Unconditional SPY on the window: 0.62 (rf 5 %) / 0.90 (rf 0). The "H=21
exit-on-flip" column equals H=1 by construction (a lot closed on the first bar
whose regime differs contributes exactly the still-in-regime mark-days).

**Retraction:** the 2026-08-29 20:36 UTC changelog/memory line "forward-H Sharpe
after LOW_VOL days is −0.14 (H=1)" was wrong (not reproducible); the correct
next-day figure is +1.34 (rf 0) / +0.80 (rf 5 %). Task 7 below corrects the record.

---

## 1. `S_m` — forward, horizon-matched, rf-consistent (`src/backtest/benchmark_baseline.py`)

**D-A1.** `S_m[regime][H]` := annualized excess Sharpe of the equal-weight
daily-marks equity curve of benchmark lots entered at every close tagged
`regime`, each held exactly H trading days (no stop, no target, no cost), i.e.
`(mean(r_d) − RISK_FREE_DAILY) / std(r_d, ddof=1) · √252` over the set of
trading days d on which at least one such lot is open, where `r_d` is the
benchmark's close-to-close return on d. This is `aggregate_metrics`' estimator
applied to a synthetic benchmark strategy, so `S_adj` and `S_m` are the same
unit (F2). For H = 1 the day set is exactly {t+1 : regime(t) = regime}.

**D-A2.** Horizon grid `BENCH_HORIZONS = (1, 2, 3, 5, 10, 21)`. New public
function `regime_benchmark_sharpe_by_horizon(start_date, end_date, benchmark='SPY',
min_obs=40, horizons=BENCH_HORIZONS) -> dict[str, dict[int, float | None]]`.
The existing `regime_benchmark_sharpe(start_date, end_date, benchmark, min_obs)`
keeps its signature and flat `{regime: float | None}` shape but now returns the
**H = 1** column (so `unified_backtest`'s informational
`strategy_backtest_regimes.benchmark_sharpe` write at `unified_backtest.py:1419`
needs no change and becomes the H = 1 value). `min_obs` counts mark-days per
regime (unchanged semantics: < 40 ⇒ `None`).

**D-A3.** Risk-free: `benchmark_baseline.RISK_FREE_DAILY = 0.05 / TRADING_DAYS_PER_YEAR`
declared locally (the module is deliberately import-free of `unified_backtest`),
with a unit test asserting equality with `unified_backtest.RISK_FREE_DAILY` and
`trade_handoff_builder.RISK_FREE_DAILY`. The loaders `load_regime_tags` /
`load_benchmark_closes` are unchanged (tests keep monkeypatching them and the
`REGIMES_PARQUET` / `PRICES_PARQUET` constants exactly as
`tests/backtest/test_benchmark_baseline.py` does today).

**D-A4.** Fail-open contract unchanged: whole-window load failure ⇒ `{}`;
thin regime ⇒ `None` for that regime at every H.

### 1.1 Cache and sizer (`src/execution/benchmark_sizing.py`, `regime_blended_sizer.py`)

**D-A5.** `regime_benchmark_sharpe_for_sizing` computes via
`regime_benchmark_sharpe_by_horizon` and persists the full grid:
`{'schema': 2, 'as_of', 'benchmark', 'start', 'horizons': [1,2,3,5,10,21],
'by_regime': {regime: {'1': s, '2': s, …}}}` under the existing
`pipeline_config['benchmark_regime_sharpe']` key. `_read_cache` treats a payload
without `schema == 2` as a miss (this is what invalidates today's contemporaneous
cache on the first cycle after deploy; no migration, no manual delete).

**D-A6.** Horizon selection: `benchmark_horizon_days` read from
`pipeline_config` (int; must be ON the grid — off-grid, missing or garbage ⇒ **default 1**, off-grid logged),
via a small `_load_benchmark_horizon(default=1)` in `benchmark_sizing` mirroring
`regime_blended_sizer._load_lambda`'s pattern. `regime_benchmark_sharpe_for_sizing`
returns `by_regime[regime][str(H)]`. The `shadow_line` gains `h=<H>` next to
`s_m=` so the log is self-describing. The sizer call site
(`regime_blended_sizer._sharpe_cadence_path`, `_bsz.regime_benchmark_sharpe_for_sizing(regime_state, date.today())`)
is unchanged. Migration: seed `pipeline_config('benchmark_horizon_days', '1',
description)` in a new migration file (idempotent `ON CONFLICT DO NOTHING`);
the dashboard exposes nothing new in this amendment (operator edits the row).

**D-A7.** `apply_benchmark_hurdle` semantics unchanged (benchmark tickers exempt;
alpha `w = sign(S_adj)·(|S_adj| − S_m)`, drop at `≤ 0`).

---

## 2. Beta sleeve exits on regime flip (`src/strategies/implementations/S_beta_spy.py`)

**D-B1.** `BetaSpy.exit_hook = True`; `should_exit(position, prices, regime, aux_data)`
returns `'regime_exit'` when `regime.get('state')` is a canonical regime and
differs from `position['signal_params'].get('regime')` (the entry regime the
sleeve already records in `signal_params`); `None` otherwise — including a
missing/unknown state on either side (hold; the hold cap still protects). No
other reads: no `prices` dependency, pure, look-ahead-safe by construction.

**D-B2.** `HOLD_DAYS` stays 21: the promotion guard
`promotion_service._holdCapMismatch` compares the run's `max_hold_days` with
`MAX(strategy_regime_params.max_hold_days)` (NULL ⇒ `LIVE_HOLD_CAP_DEFAULT = 21`)
for `exit_hook` runs, and the live time stop is `engine._hold_cap(signal_params,
regime_param_resolver.configured_max_hold_days(sid, default=21))`. A daily
re-entered lot under the hook lives min(21, days until the next flip).

**D-B3.** Re-entry: `generate_signals` is unchanged (one LONG SPY per bar,
`signal_params['regime']` = the bar's state), so the bar after a flip opens a
new lot tagged with the new regime. Backtest: every open lot exits at close *t* (hook) and a new lot opens
at close *t* (`same_close` fill) — each lot still has exactly one entry and one
exit, so a flip shortens lots rather than adding round trips (cost per lot
unchanged, cost per unit time higher; 1,487 hook exits over 127 spells on the
2016–2026 tags). Live, in the 15:00 step
`run_strategies` → `write_signals` runs BEFORE `update_pnl` (`engine.main`,
:2452 vs :2563): with `OPENCLAW_SAMEDAY_EXEC=1` (`.env:179`) `write_signals`'
`_lifecycle_rows` gate is on, the held ticker's spent row (`target_date` <
today) triggers a continuation mint — a NEW open row carrying today's
`signal_params` (new regime tag) — and `update_pnl` then closes only the OLD row
(`strategy_exit:regime_exit`; the new row has `days_held = 0` and matching
regime ⇒ hold). The 15:55 carried set (`_load_approved_carried_signals`,
`DISTINCT ON (strategy_id, ticker)` newest `target_date`) still targets SPY, so
the sizer nets against the held position: no churn live. The backtest is
therefore mildly pessimistic on costs; accepted.

**D-B4.** Regime source asymmetry accepted: live `regime['state']` is the
intraday HMM regime-of-record (engine passes its payload to `update_pnl`);
backtest `state` is the VIX tier (`_per_bar_simulate`'s `regime_payload`). This
is the same kin-mismatch every alpha sleeve carries through entry tagging
(`regime_blended_sizer` sizes on VIX-tier sleeves under the HMM regime).

**D-B5.** Expected sleeves after the re-backtest ≈ F5's H=1 column less one
spread per flip: LOW_VOL ≈ 0.78, TRANSITIONING ≈ 0.40, HIGH_VOL ≈ 0.48,
CRISIS ≈ 1.5. The implementation plan records the actual numbers.

**D-B6.** The hook is live for the sleeve the moment the strategy change is
deployed (`update_pnl` checks `strat.exit_hook` on the loaded instance;
`OPENCLAW_EXIT_HOOK_LIVE=1` since 08-28) — independent of the re-backtest.
`S_beta_spy` becomes the first hook strategy to trade; the Phase-2 spec §4.3
watch checklist applies (`signal_pnl` close `strategy_exit:regime_exit` at
15:00 ET → 15:55 sizer nets against the new row; `[exit_hook] closes:` digest line).

---

## 3. Fleet-backtestable sleeve (`src/backtest/unified_backtest.py`)

**D-C1.** `load_prices_panels(calendar='union', tickers=None)`: when `tickers`
is a non-empty collection, the `pq.read_table(PRICES_PARQUET, columns=_COLS,
read_dictionary=[...])` call adds `filters=[('ticker', 'in', sorted(set(tickers)))]`.
Everything after the read (quarantine filter, categorical normalisation, pivot)
is unchanged, so the SPY-only output is byte-identical to reading the SPY-only
parquet the 08-29 scratchpad runner used. `tickers=None` ⇒ today's path,
byte-identical (guarded by `tests/backtest/test_arrow_dictionary_read_equivalence.py`
plus a new equivalence case).

**D-C2.** Source of `tickers`: manifest `metadata.backtest_tickers` (list of
symbols) read in `run_backtest` next to the existing `_bounded_resolver` manifest
read; absent ⇒ `None`. `--strategy-file` runs of an unregistered strategy get
no filter (as today). `S_beta_spy`'s manifest entry gains
`"backtest_tickers": ["SPY"]` (manifest edit performed through the repo's
manifest lock, not committed by the campaign — the pipeline owns manifest
commits, as on 08-29).

**D-C3.** Retires the scratchpad runner: the weekly
`scripts/rebacktest_runner.py` (`python3 -m backtest.unified_backtest --strategy-id …`)
now runs the sleeve inside the fleet's 3.5 GB unit cap.

---

## 4. Always-on sleeve activation

**D-D1.** `activation_assigner.compute_eligible(conn, strategy_id, threshold,
min_trades, instrument_class, *, always_on: bool = False)`: when `always_on`,
`diag` is computed exactly as today but `eligible_by_regime` is
`{r: True for r in CANONICAL_REGIMES}` and the audit `reason` string ends with
`rule=benchmark_sleeve_always_on` instead of `rule=qualifies(>0·classDD·trades)+slider`.
`apply_one` forwards the kwarg; `main()` computes
`execution.benchmark_sleeve.load_benchmark_sleeve_ids()` once and passes
`always_on = sid in bench_ids`. A sleeve with no primary run is still
`skipped_no_run` (nothing to size on). Dashboard slider changes therefore never
touch the sleeve's rows; the diag still shows its per-regime numbers.

**D-D2.** `strategy_weights.find_negative_across_all_eligible` excludes
`load_benchmark_sleeve_ids()` from `active_ids` (a benchmark sleeve is never
auto-demoted; its dormancy in a bad regime is expressed by weight, not state).

**D-D3.** Weights unchanged: `strategy_weights._regime_weight` gives
`daily_weight = effective_sharpe` per regime, so a regime where the sleeve's
sleeve is ≤ 0 sizes to dust and the existing dust drop removes it. No floor,
no special case.

**D-D4.** The candidate→live promotion gate (`promotion_service.js`) is not
touched: the sleeve is already live, and the always-on rule is an *activation*
exemption, not a promotion exemption. Future benchmark sleeves promote through
the normal gate.

---

## 5. Rollout and the apply flag

**D-E1 (operator may override).** With the sleeve present in every regime the
B1 guard in `_sharpe_cadence_path` (`_apply_hurdle = _bench_on and bool(_bench_tkrs)`)
no longer keeps rule C inert, so on the first cycle after Tasks 1–4 land rule C
would apply live at the new `S_m` in whatever regime is current. Ruling: unset
`OPENCLAW_BENCH_RELATIVE_SIZING` in `.env` (shadow) as the FIRST rollout step,
user-scope johnbot restart, and re-flip only after two shadow cycles
(`bench_sizing.shadow[<regime>] … h=1 s_m=…` lines in #botjohn-log) show the
drop list and gross move. The 08-29 flip is thereby paused, not reverted in code.

**D-E2.** Order: (1) flag → shadow + restart; (2) code Tasks 1–4 (tests green);
(3) sleeve re-backtest as a transient unit (`--strategy-id S_beta_spy`, filtered
panel, `MemoryMax=3500M`, outside 13:00–20:15 UTC weekdays; the post-commit panel
hook then rebuilds the sleeve's panel rows); (4) `activation_assigner --strategy-id S_beta_spy`
(all four eligible) → `strategy_weights --rebuild`; (5) verify the cache payload
is `schema: 2` after the next sizer run and `S_m[LOW_VOL][1] ≈ 0.80`; (6) shadow
cycles; (7) re-flip. Steps 3–7 are operator/runbook items in the plan, not code.

**D-E3.** Record correction: changelog entry for this amendment retracts the
"−0.14 (H=1)" line (F5) and links this spec; memory + CLAUDE.md bullet updated.

---

## 6. Tests (named files; never the full suite while the fleet runs)

- `tests/backtest/test_benchmark_baseline.py` (extend): synthetic 3-regime
  series where contemporaneous and lag-1 Sharpe differ by construction (return
  on tagged day ≠ return on the following day); asserts (a) `regime_benchmark_sharpe`
  == H=1 column of `regime_benchmark_sharpe_by_horizon`, (b) H=1 equals the
  hand-computed next-day excess Sharpe, (c) H=3 union-day set size and value,
  (d) rf constant equality with `unified_backtest` / `trade_handoff_builder`,
  (e) thin regime ⇒ `None` at every H, (f) load failure ⇒ `{}`.
- `tests/execution/test_benchmark_sizing.py` (extend): schema-1 cache is a miss
  and gets rewritten as schema 2; `benchmark_horizon_days` 5 selects the H=5
  column; garbage/missing ⇒ H=1; `shadow_line` carries `h=`.
- `tests/strategies/test_beta_spy_exit_hook.py` (new): `should_exit` truth
  table (same regime ⇒ None; different ⇒ `'regime_exit'`; missing/unknown state
  ⇒ None); open-book run on a 12-bar synthetic SPY panel with a mid-window
  regime flip closes the lot on the flip bar with `strategy_exit:regime_exit`
  and opens a new lot tagged with the new regime.
- `tests/backtest/test_arrow_dictionary_read_equivalence.py` (extend):
  `tickers=['SPY']` equals the unfiltered panel sliced to SPY; `tickers=None`
  byte-identical to today.
- `tests/backtest/test_activation_assigner.py` (extend): `always_on=True` writes
  four `eligible=True` rows with the `benchmark_sleeve_always_on` reason while
  `diag` still reports the failing regimes; `always_on=False` unchanged.
- `tests/execution/test_strategy_weights_*` (new case): a benchmark sleeve with
  all-negative weights is NOT returned by `find_negative_across_all_eligible`.
- Guard: `tests/execution/test_sizer_benchmark_hurdle_wiring.py` still passes
  (conftest stubs `regime_benchmark_sharpe_for_sizing`; unchanged contract).

---

## 7. Out of scope / deferred

- Per-strategy or per-ticker horizon (H from the acting strategies' hold):
  the grid is cached so this is a sizer-side change later; not now (YAGNI —
  operator chose H = 1 as the system cadence).
- Costs inside `S_m`: none (the hurdle is a market return, not a strategy).
- Exit hook for any other live strategy: opt-in per strategy via a `_v2`
  candidate through the normal lane; nothing global.
- Stale comment at `unified_backtest.py:1404–1415` ("read by
  regime_qualification.qualifies_regime … promotion_service.js judgeRegimeSleeve
  as the excess-Sharpe-over-benchmark gate leg") — those readers were removed
  08-29 (T1/T2); the comment is corrected in Task 1 as a drive-by.
- Deferred minors from the 08-29 SDD ledger remain deferred.

## 8. Decisions recorded (operator may override)

| id | decision | source |
|---|---|---|
| D-A1/A2 | `S_m` forward, entry-tagged, engine estimator, grid {1,2,3,5,10,21}, H = 1 default | operator 21:27 UTC |
| D-A3 | rf 5 % in `S_m` (unit parity with sleeves) | F2, approved with design §1 |
| D-B1/B2 | sleeve exits on any regime change; hold stays 21 | operator 21:33 UTC ("c") |
| D-D1/D2 | sleeve eligible in all regimes regardless of slider; never auto-demoted | operator 21:33 UTC |
| D-E1 | shadow during rollout, re-flip after 2 shadow cycles | recommended §5, "go ahead" 21:55 UTC |
