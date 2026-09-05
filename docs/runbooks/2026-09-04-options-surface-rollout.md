# Options surface v2 / CBOE OI / macro rf / NYSE calendar — rollout runbook

Spec: docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md · Plan: docs/superpowers/plans/2026-09-04-options-surface-cboe-oi-rf-calendar.md

This runbook is authored on `worktree-options-surface-v2` alongside Tasks 1–14
(NYSE session calendar, `backtest/risk_free`, `strategies/options_surface`,
CBOE open interest, holiday strategy on NYSE closures). The operational steps
below (panel rebuild, re-backtests, timer enablement) are executed by the
controller on the main tree after merge — the state marked "TO BE FILLED by
the rollout run on main" is not yet known from this branch and must not be
guessed.

## State after the build (TO BE FILLED by the rollout run on main)
- Surface master: `data/master/options_surface.parquet` rows=**TO BE FILLED by the rollout run on main** dates=**TO BE FILLED by the rollout run on main** (first session 2026-06-29 per Step 1's `--start 2026-06-29 --end 2026-09-04`).
- Enriched panel rebuilt **TO BE FILLED by the rollout run on main** (expected ~250,000 rows per Step 1); v1 copy preserved at `data/derived/options_aggregates_enriched.v1-2026-09-04.parquet` before the rebuild.
- Verification (Step 1 `system_checks`): `options_aux_freshness` and `options_oi_coverage` results — **TO BE FILLED by the rollout run on main**. Expected: SPY `iv30` ≈ 0.12–0.13 (not the live-path 0.402 seen pre-fix on 09-03), `iv_rank` non-null > 90 % of tickers with ≥ 20 sessions, `pcr_oi` non-null ≈ 550 (current real CBOE coverage is ~550 tickers/session across the 11 sessions captured since 2026-08-21).
- Re-backtests (`scripts/rebacktest_options_sleeve.sh`, run **TO BE FILLED by the rollout run on main**): before → after table per regime for the seven strategies (`S21_iv_hv_spread`, `S_HV8_gamma_theta_carry`, `S_HV19_iv_surface_tilt`, `S_HV20_iv_dispersion_reversion`, `S_options_flow_confirmed_momentum`, `S_pre_earnings_vol_runup`, `S_holiday_seasonality_energy_etf_tv1`) — **TO BE FILLED by the rollout run on main** (before = the pre-rebuild `strategy_backtest_regimes` numbers referenced in spec §0; after = the post-rebuild run under `config_json->'rf'->>'source'`).
- Assigners: activation + eligibility re-gate at the normal cadence (daily activation step; Monday 04:00 UTC weights rebuild). No manual promotion required or performed by this rollout.

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
| `OPENCLAW_OPTIONS_SURFACE` | 0 (shadow) | after the first clean `[options_surface] shadow …` line — Tue 2026-09-08 15:00 ET compute (Mon 09-07 is Labor Day, market closed, so no compute that day). Clean = n ≥ 3,000, iv30 old/new median in 1.5–3.5, iv_rank_nonnull ≥ 80 %, rv20_nonnull ≥ 95 %, vrp_nonnull ≥ 80 %, dur < 180 s (the last three added by the 2026-09-05 final fix wave — `rv20_nonnull`/`vrp_nonnull` would have caught the as-of `rv_20` defect, `dur` guards the shadow-mode cost budget `OPENCLAW_OPTIONS_SURFACE_BUDGET_S`, default 240 s; a `spot_stale` near 100 % is EXPECTED at the 15:00 ET compute and is not a defect). Set `=1` in `.env` and `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service`. Must not flip before Tasks 13/14's OI keys exist in the live dict — they do, on this branch. |
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
