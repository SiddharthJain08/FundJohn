# Options surface v2 / CBOE OI / macro rf / NYSE calendar — rollout runbook

Spec: docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md · Plan: docs/superpowers/plans/2026-09-04-options-surface-cboe-oi-rf-calendar.md

This runbook is authored on `worktree-options-surface-v2` alongside Tasks 1–14
(NYSE session calendar, `backtest/risk_free`, `strategies/options_surface`,
CBOE open interest, holiday strategy on NYSE closures). The operational steps
below (panel rebuild, re-backtests, timer enablement) are executed by the
controller on the main tree after merge — the state marked "TO BE FILLED by
the rollout run on main" is not yet known from this branch and must not be
guessed.

## State after the build (filled 2026-09-06 18:00 UTC from the rollout run on main)
- Rollout unit `options-surface-rollout-20260906` ran 2026-09-06 02:00 UTC. Surface master `data/master/options_surface.parquet`: **150,777 rows, 46 sessions 2026-06-29..2026-09-04**, built in 10,514 s (the last three 5-session chunks took ~40 min each under memory pressure; the box OOM-killed an unrelated watcher at 02:23 UTC).
- Enriched panel `data/master/options_aggregates_enriched.parquet` rebuilt 04:55 UTC: **150,777 rows** (one per surface row — the "~250,000" estimate assumed v1's row count over a longer history); v1 copy preserved at `data/derived/options_aggregates_enriched.v1-2026-09-04.parquet`.
- Verification (2026-09-06 12:45 UTC): `options_aux_freshness` **PASS** (newest 2026-09-04, 4,169 tickers, OI-derived fields honest); `options_oi_coverage` **PASS** (544 tickers carry CBOE OI on 2026-09-04). Latest-session panel: SPY `iv30` 0.1196 / `iv90` 0.1388 / `iv_rank` 4.8 / `rv_20` 0.076 / `vrp` 0.043 / `pcr_oi` 2.60 / `gex` 103,588 / `max_pain` 764; AAPL `iv30` 0.2447; XOM `iv30` 0.2697 — all as the backtest definition predicts (the pre-fix live values were 0.40 / 0.50 / 0.53). `pcr_oi` non-null 544, `gex` 543. Among tickers that carry `iv30`, `iv_rank` is non-null for 98 %.
- 🔴 **Coverage finding:** `iv30` is non-null for only **28.8 % of the 4,169 panel tickers (30.5 % of the 3,861 liquid-tier names)** on 2026-09-04, against **100 %** in the v1 panel: v2's smile gate (≥ 5 IV-bearing strikes on both sides of spot per expiry) plus the 30-day bracket / ±10-day one-sided rule drop every thin chain v1 covered with a single ATM strike (57 % of liquid names fit ≥ 1 expiry, 43.8 % fit ≥ 2). Four live strategies read `iv30`/`iv_rank`/`vrp`. Fixed by amendment §H of the v3 spec (`docs/specs/2026-09-06-options-mfiv-rnd-synthetic-engine-spec.md`, merged to main `399f240b`): a v1-style |Δ| .40–.60 band point per thin expiry feeds the 30/90-day interpolation, flagged `iv30_source='atm_band'`, one-sided tolerance 10 → 20 d; smile-only keys stay smile-only. The v3 rollout (`docs/runbooks/2026-09-06-options-surface-v3-rollout.md`, transient unit `surface-v3-rollout-20260907`, Mon 2026-09-07 11:05 UTC) rebuilds the master and panel under that contract. **Until it lands, the runbook's `iv_rank_nonnull ≥ 80 %` flip threshold is unreachable by construction — do not flip `OPENCLAW_OPTIONS_SURFACE` on the 09-08 line; read the `iv30_src smile=…% band=…%` split on the line instead.**
- Re-backtests (`scripts/rebacktest_options_sleeve.sh`, 04:55–08:00 UTC, `--start-date 2023-09-04`, const rf): `S21_iv_hv_spread` run `3f22dd8b` total −1.74 (448 trades; LOW_VOL 1.32 / 88 trades vs SPY bench 1.27, TRANSITIONING −2.15 / 360, no HIGH_VOL/CRISIS trades in that window); `shv8_gamma_theta_carry` run `928b7687` total 0.11 (1,106 trades; LOW_VOL −1.91 / 436, TRANSITIONING −0.05 / 670). The unit was then **OOM-killed inside `S_HV19` (3.4 GB peak vs MemoryMax 3500M)** — S_HV19, S_HV20, S_options_flow and the holiday strategy did not run. Two lessons: (1) `--strategy-file` rows land under the FILE-STEM id (`shv8_gamma_theta_carry`), not the manifest id (`S_HV8_gamma_theta_carry`), so the sleeve script cannot refresh the canonical rows the assigners read; (2) the 2023-09+ window is not comparable with the fleet's 2016+ "before" numbers. **The before → after table therefore comes from the fleet epoch** (`refresh_backtests_resumable.js`, manifest ids, macro rf, default window) — running since 2026-09-06 08:05 UTC — and the v3 rollout unit re-queues the five options strategies in the fleet checkpoint after the §H panel lands, so their canonical rows are re-derived on the final panel by the Mon 2026-09-07 21:30 UTC nightly. Read them with `SELECT strategy_id, run_at, total_sharpe, config_json->'rf'->>'source' FROM strategy_backtest_runs WHERE primary_window AND strategy_id IN ('S21_iv_hv_spread','S_HV8_gamma_theta_carry','S_HV19_iv_surface_tilt','S_HV20_iv_dispersion_reversion','S_options_flow_confirmed_momentum') ORDER BY run_at DESC` and compare with the spec §0 "before" numbers (S21 −2.48/−0.83/−1.66/−0.05 LV/TR/HV/CR; S_HV8 0.78/−0.41/0.38/−0.91; S_HV19 0.99/−0.09/0.68/2.55; S_HV20 −0.24/−0.01/0.64/0.91). Holiday tv1 (no manifest id) re-runs via `--strategy-file` inside the v3 rollout unit.
- Assigners: activation + eligibility re-gate at the normal cadence; nothing was promoted or demoted by this rollout.

## Why this rollout exists (spec §0)
Two live defects, root-caused on `/root/openclaw` main `791adfb1` (2026-09-04):
1. **`iv_rank` constant 50.0 for every ticker, silently.** The live path (`engine.load_aux_data`) gates `iv_rank` on a non-empty OI-by-strike map; `options_eod.parquet`'s OI column is 100 % NULL for its whole history, so every ticker fell back to a hardcoded 50.0 with no signal. The backtest path computed a real 252-session percentile of `iv_front` (min 20 obs). Four live strategies and one candidate were qualified on the backtest number and traded on the constant.
2. **Live `iv30` ≠ backtest `iv30`.** Live took the mean IV of *all* strikes of the nearest expiry (on a Thursday capture, the 1-DTE chain — 71 % null IV): SPY 0.402 / AAPL 0.502 / XOM 0.530 on 2026-09-03. Backtest computed `iv_front` = the |Δ| .40–.60 mean of the front ≤45-day expiry: SPY 0.125 / AAPL 0.264 / XOM 0.291. The two paths never agreed on what "30-day IV" means.

Fix (this branch, Tasks 1–14): one shared module, `src/strategies/options_surface.py`,
consumed by both `execution/engine.py` (live, behind a flag) and
`scripts/compute_rolling_options_fields.py` (backtest panel) — one function,
one history table, both consumers. Riding along on the same code paths: CBOE
open interest (banked since 2026-08-21, read by nothing until `strategies/options_oi.py`
+ the `options_oi_coverage` check), a macro risk-free module (`backtest/risk_free`,
DGS3MO from `macro.parquet`) replacing six hardcoded-5% sites, and an NYSE
session calendar (`src/lib/trading_calendar.py`) replacing ad hoc weekday/federal-holiday
logic everywhere including the holiday strategy.

## Flags
| flag | now | flip when |
|---|---|---|
| `OPENCLAW_OPTIONS_SURFACE` | 0 (shadow) | **AUTOMATED 2026-09-06** (operator: "you may flip the options surface automatically as well"): `scripts/options_surface_flip_after_shadow.sh --apply` on transient timer `options-surface-flip` (Tue–Fri 21:50 UTC) flips `.env` to 1 and restarts user-scope johnbot only when (G1) `data/master/options_surface.parquet` carries the v3/§H contract (`iv30_source` column, every latest-session row at version 3 — i.e. the v3 rollout unit landed), (G2) ≥ 2 clean `[options_surface] shadow` lines on ≥ 2 distinct days since 2026-09-08 — clean = `version=3`, n ≥ 3,000, iv30 old/new median in 1.5–3.5, rv20_nonnull ≥ 95 %, iv_rank_nonnull ≥ 60 %, vrp_nonnull ≥ 60 %, mfiv_nonnull ≥ 80 % (over tickers with ≥ 2 fitted expiries), dur < 180 s, and no `v2 build failed` / `partial shadow` warning that day (the 80 % iv_rank threshold below assumed v1-like coverage; §H restores most of it and 60 % is the floor below which the served dict would starve the strategies), (G3) the five options strategies' latest primary backtest rows post-date the v3 panel (their eligibility was re-derived on the same feature definitions the live dict serves), (G4) outside the weekday 13:00–20:15 UTC compute window. The line is read from the dedicated `logs/options_surface_shadow.log` (the daily-cycle step log keeps only a 4,000-char tail and drops it). Posts the verdict to #botjohn-log nightly either way; stops its own timer once applied. Re-arm after any reboot. Kill switch: `OPENCLAW_OPTIONS_SURFACE=0` + user-scope johnbot restart. Original manual rule kept for reference: flip after the first clean line — n ≥ 3,000, iv30 old/new median in 1.5–3.5, iv_rank_nonnull ≥ 80 %, rv20_nonnull ≥ 95 %, vrp_nonnull ≥ 80 %, dur < 180 s; `spot_stale` near 100 % is EXPECTED at the 15:00 ET compute. Must not flip before the OI keys exist in the live dict — they do since `92fc9d11`. |
| `OPENCLAW_RF_SOURCE` | const (shadow) | after ≥ 5 live `[rf_shadow]` lines and one backtest. Emitting sites: `aggregate_metrics` (backtest, also written to `config_json['rf']`), `bench_realized` (one line per 20-day Sharpe — book and SPY — on every daily report) and `benchmark_baseline` (one line per regime at `h=1`, whenever the S_m grid is computed, in the `trade` step's log). Where to look: the `trade` step surfaces `benchmark_baseline`'s lines (`regime_blended_sizer_live._ensure_logging`) and the `report` step surfaces `bench_realized`'s — the latter only since the 2026-09-05 fix wave gave `send_report.py` the same idempotent `_ensure_logging()` the sizer and premarket gate already had; before that its root logger sat at WARNING with no handler and every `[rf_shadow]` line was discarded. Override with `OPENCLAW_REPORT_LOG_LEVEL`; set `=macro`, restart johnbot; then schedule the fleet re-backtest below. Risk-free series is DGS3MO (3-month constant-maturity Treasury, FRED, from `macro.parquet`). |

## Fleet re-backtest under macro rf (operator-triggered, weekend)
`strategy_backtest_runs.config_json->'rf'->>'source'` distinguishes populations. Run the fleet as serial transient units (`scripts/rebacktest_options_sleeve.sh` pattern over `--all-live`) in a quiet window; the assigners then re-gate. Until then, gate levels mix const (older rows) and macro (newer rows) — the Sharpe delta is ≤ ±0.1 over the 2023-09+ window.

## Watch list
- Tue 2026-09-08 15:00 ET: `[options_surface] shadow` line; `[rf_shadow]` lines; `[exit_hook]` unaffected.
- `python3 -m system_checks --check options_aux_freshness` and `--check options_oi_coverage` daily. `options_oi_coverage` FAILs below `min_tickers=400` tickers carrying CBOE OI on the latest session while a CBOE capture for the prior day exists; real coverage is ~550 tickers/session (11 sessions banked since 2026-08-21 by `openclaw-cboe-chains.timer`, 17:00 ET Mon–Fri).
- `openclaw-trading-calendar.timer` — monthly refresh of `data/master/trading_calendar.parquet`. **Not yet installed/enabled in production** as of this authoring (2026-09-05); the calendar master itself is already built (15,127 sessions, 1970-01-02..2029-12-24) but the timer install/enable is a post-merge operational step. `master_freshness` gains cadence `trading_calendar.parquet: fetched_at < 45 d` — watch for staleness once the timer is live.
- S5_max_pain re-backtest once ≥ 60 CBOE sessions exist (≈ 2026-11-12).
- Holiday strategy (`S_holiday_seasonality_energy_etf_tv1`) is now on NYSE closures, not the federal calendar (task 4 on this branch) — re-backtest owed, covered by `scripts/rebacktest_options_sleeve.sh` above.

## Facts as of authoring (2026-09-05, this branch)
- Trading-calendar master: already built, 15,127 sessions, 1970-01-02..2029-12-24. `openclaw-trading-calendar.timer` not yet installed/enabled — done post-merge.
- Surface master (`data/master/options_surface.parquet`) and the v2 enriched panel do not exist yet in production — built post-merge by Step 1 (`scripts/build_options_surface.py --start 2026-06-29 --end 2026-09-04` then `scripts/compute_rolling_options_fields.py`).
- Real CBOE OI coverage: ~550 tickers per session, 11 sessions banked since 2026-08-21.
- `options_oi_coverage` check: `min_tickers=400`.
- Risk-free series: DGS3MO (3-month constant-maturity Treasury, FRED, via `macro.parquet`).
- Fleet re-backtest under macro rf is operator-triggered (not part of this rollout's automatic path).
- Mon 2026-09-07 is Labor Day (market closed) — first live shadow compute is Tue 2026-09-08 15:00 ET.
- The `OPENCLAW_OPTIONS_SURFACE` flag must not flip before Tasks 13/14's OI keys exist in the live dict — confirmed present on this branch — and only after a clean shadow line.

## Rollback
- Options: `OPENCLAW_OPTIONS_SURFACE=0` serves the legacy dict; the legacy block is intact in engine.py. The backtest panel can be restored from the v1 copy (`data/derived/options_aggregates_enriched.v1-2026-09-04.parquet`).
- rf: `OPENCLAW_RF_SOURCE=const`.
- Calendar: **rename** `data/master/trading_calendar.parquet` (never delete a master — CLAUDE.md core invariant) or point `OPENCLAW_TRADING_CALENDAR_PATH` at another file → the library falls back (alpaca probe, then weekday) with a WARNING. Expect backtests to SLOW DOWN under the alpaca probe tier: it is a per-session network call, not a parquet lookup.

## Known-transient warning between merge and rollout Step 1
`master_freshness` WARNs `missing: options_surface.parquet` from the moment
this branch merges until Step 1 builds the surface master on main. The check
entry (`('date', 5)`) ships with the code; the file it names is created by the
rollout. This is expected — do not treat it as a regression, and do not silence
the check.
