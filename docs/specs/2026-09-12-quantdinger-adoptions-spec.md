# QuantDinger adoptions — spec (2026-09-12)

Source: the QuantDinger fit review (artifact
`https://claude.ai/code/artifact/0a02f84e-0bb6-4d70-9cb1-d0c648064917`,
memory `project_quantdinger_fit_review_20260911`). Operator approved ALL
actions on 2026-09-12 16:14 UTC with these rulings:

- **R1** Tier 1 approved as written. Item 1 (fundamentals PIT lag) may lower
  the fundamentals sleeve's Sharpes and demote strategies — accepted. Item 2
  (gap fill) lands behind a flag now and flips with the fleet epoch AFTER the
  atr_r epoch (`fleet-target-epoch-20260913`, Sun 08:05Z → uniform ≈ Tue
  09-15). Never stack epochs.
- **R2** Breaker (item 4): 10 % drawdown from the alpha-sleeve peak, 3 % of NAV
  daily loss, operator-flag re-arm. **HIGH_VOL and CRISIS are NOT exempt** —
  and the existing per-position `position_circuit_breaker.py` must ALSO stop
  skipping HIGH_VOL/CRISIS.
- **R3** Event-window gate (item 5): **block new entries** from T-1 through the
  release session. **HIGH_VOL and CRISIS are NOT exempt.** Benchmark sleeve:
  not exempt by default (consistent with the 09-04 premarket-veto ruling);
  expose `OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1` as an operator switch.
- **R4** Research-lane items (6–9) proceed now, in whatever order is most
  efficient.
- Items 19–22 keep their DEFER verdict (GDELT, trailing-stop state,
  limit-entry fills, FRED regime features).

## 0. Non-negotiables (apply to every task)

- Master parquets and canonical Postgres tables are append-only (repo
  CLAUDE.md). New tables/columns only; never DELETE, never rewrite history.
- Backtest side is AUTHORITATIVE (08-07 ruling). Any live/backtest
  disagreement is fixed on the live side unless the backtest is provably
  look-ahead — items 1 and 2 are exactly that case.
- Every new behaviour ships behind an env flag whose unset value is
  byte-identical to today's behaviour, unless the item is a pure bug fix
  that the operator has explicitly approved (items 3, 10, 11, 16, and the
  circuit-breaker regime change).
- 2-core / 8 GB / no swap: never load whole `prices.parquet` or
  `options_eod.parquet`; slice by date/ticker; no always-on threads; no new
  packages.
- Production = working tree on `main`; timer-spawned scripts pick up the tree
  on their next run. Work on a worktree branch; merge to main only when the
  whole stream is green; never leave main half-edited across a timer boundary.
- Tests on this box reach the REAL DB (`.env` loads at import) — stub gates
  in fixtures; never run the full suite while the fleet runs; never include
  `test_regime_stratified_backtest`.
- Every file:line cited below was grep-verified on 2026-09-11 against main
  `d9dbdf06`; re-verify before editing (lines drift).
- Log to `docs/archive/changelog.md` (newest first) per stream, not to
  CLAUDE.md.

## 1. Stream A — make the gates truthful (items 1, 2, 17 + hygiene)

### A1 Fundamentals point-in-time availability (item 1)
- Today: `src/strategies/aux_data_loader.py:444-459` `_financials_slice`
  selects `df['date'] <= ts`; `date` = FMP statement period-end
  (`src/pipeline/backfillers/fmp.py:119-150` stores no filing date).
  Consumers: S_ast_asset_growth_effect, S_ast_roa_effect_within_stocks,
  S_accrual_anomaly (`aux_data_loader.py:451-453`), S10, S_bankruptcy.
- Change: compute `available_at` per (ticker, period-end) =
  first earnings report date in `earnings.parquet` (already loaded at
  `aux_data_loader.py:104`) that is > period_end and ≤ period_end + 120 d;
  else period_end + 60 d. Slice on `available_at <= ts`. Live path
  (`engine.py` aux['financials']) already sees only filed data, so no live
  change — but add a parity test that the backtest slice at "today" equals
  the live dict shape.
- Flag: `OPENCLAW_FINANCIALS_PIT=1` selects the new rule; unset = legacy
  (byte-identical). Default flips to 1 in the drop-in for the post-atr_r
  epoch (see A4).
- Tests: synthetic financials + earnings frames; a period ending 03-31 with
  an earnings date 05-05 is invisible on 04-15 and visible on 05-05; with
  no earnings row it becomes visible on 05-30.

### A2 Gap-through-stop fills at the open (item 2)
- Today: `src/backtest/unified_backtest.py:391-411` `_bar_exit(direction,
  high, low, stop_loss, target_1, dt_priority)` returns the stop LEVEL on
  any low touch; callers `simulate_trade` (:470-481) and
  `open_book.advance_open_book` (:134-138). `quick_backtest.py:477-480`
  already fills at `bar.open` when `open <= stop` (oracle test
  `tests/backtest/test_backtest_oracles.py:75-84`).
- Change: `_bar_exit` gains an `open_` argument. When
  `OPENCLAW_BT_GAP_FILL=open`: long stop fills at `min(open_, stop)` when
  `open_ <= stop` (short mirrored: `max(open_, stop)`); target on a
  favourable gap fills at the open likewise (`open_ >= target` ⇒ fill at
  `open_`, mirrored). Double-touch priority unchanged. Unset / `level` =
  byte-identical to today. Record `gap_fill` in the result metadata block
  (`unified_backtest.py:1438-1470`).
- Both call sites pass the bar open. The exit-hook stepper's position dict
  is unchanged.
- Measurement script `scripts/measure_gap_fill_impact.py`: for stored
  primary runs, re-price every stop exit whose exit-bar open is beyond the
  stop using prices.parquet sliced to those (ticker, date) pairs; print per-
  strategy Δmean pnl_pct and the count. Read-only; must run under 1 GB.
- Tests: long/short gap-through-stop, gap-through-target, no-gap unchanged,
  flag unset identical to the frozen expected values.

### A3 Exit-reason census + cost drag in the backtest JSON (item 17)
- Add to the run's metadata: `exit_reasons: {reason: {n, mean_pnl_pct,
  median_hold_days}}` and `cost_drag_bps` (Σ modelled cost / Σ |gross pnl|
  over trades, in bp). Sources: the per-trade `exit_reason` and cost fields
  already produced by `simulate_trade`/`advance_open_book`. Never changes a
  Sharpe. Tests: a 3-trade synthetic run yields the expected dict.

### A4 Post-atr_r re-gate epoch (ops)
- After `fleet-target-epoch-20260913` reaches uniform and the target-mode
  flip either fires or is rejected (Tue 09-15 21:45Z), rotate the checkpoint
  (`data/.refresh_backtests.done.pre-pit-gap-<date>`), add drop-in
  `pit-gap.conf` (`OPENCLAW_FINANCIALS_PIT=1`, `OPENCLAW_BT_GAP_FILL=open`)
  to `openclaw-fleet-overnight-resume.service`, and arm a weekend transient
  unit via `scripts/fleet_weekend_window.sh` for Sat/Sun 09-19/20. Live flip
  = `.env` + user-scope johnbot restart, automated on a gated timer modelled
  on `scripts/target_mode_flip_gate.py` (gates: fleet uniform on the new
  config, outside compute window). Owed after flip: weights → floors →
  activation (canonical sequence; `run_universe_shrink --force`).
- Hygiene in the same stream: delete the stale `S_fomc_presell_spy_long`
  row at `src/strategies/registry.py:148` and its `.pyc`; update the two
  manifest reasons at `manifest.json:1962,2030` ("prices_30m … no longer
  collected daily" is false — master refreshed 2026-09-10) and re-queue
  those two strategies as candidates.

## 2. Stream B — broker truth (items 3, 14, 15)

### B1 Broker stop fills → `signal_pnl` and the cooldown (item 3)
- Today: `src/execution/afterhours_tp.py:562-610` `classify_exit_fills`
  tags filled stop/stop_limit legs (`kind='stop'`) from
  `order list --status closed --nested`; `run_exit_fill_reporter` (:614-655)
  only posts to Discord with a seen-set file, every 10 min on
  `openclaw-afterhours-stop-monitor.timer` (`--monitor`). Cooldown reader
  `regime_blended_sizer.py:2297-2316` `_load_recent_stopouts` reads
  `signal_pnl.close_reason='stop_loss'`; `engine.py:2049-2057` infers that
  at EOD from close ≤ stop×0.98.
- Change: in `run_exit_fill_reporter`, for every classified fill with
  `kind in ('stop', 'ah_exit')` not yet seen, call
  `open_reconcile.drop_signal_close(cur, signal_id, ticker, fill_price,
  reason='stop_loss', closed_at=filled_at)` for each OPEN `execution_signals`
  row on that ticker/side (there may be several; close all — that is what
  the broker did). Idempotent by the existing seen-set plus a
  `broker_exit_fills` row (see B2). Keep the Discord post.
- Fix `afterhours_tp.py:619-620` `--limit 200`: use the keyset loop at
  :141-150 (`--after-order-id --direction asc`).
- Tests: a fixture fill list closes exactly the matching open signals with
  `close_reason='stop_loss'` and the fill price; a second run is a no-op;
  a fill on a ticker with no open signal only logs.

### B2 `broker_fills` fact table + realized-slippage digest incl. exit legs (item 14)
- New table (migration): `broker_fills(activity_id TEXT PRIMARY KEY,
  order_id TEXT, parent_order_id TEXT, client_order_id TEXT, ticker TEXT,
  side TEXT, order_type TEXT, order_class TEXT, qty NUMERIC, price NUMERIC,
  filled_at TIMESTAMPTZ, ingested_at TIMESTAMPTZ DEFAULT now())`, fed by
  the existing paginated `account activity list --activity-types FILL`
  poll in `alpaca_reconcile.py:55-93` with `INSERT … ON CONFLICT DO
  NOTHING`. Also add `filled_at TIMESTAMPTZ` to `alpaca_submissions`
  (currently only `reconciled_at`, `alpaca_reconcile.py:270-277`;
  `submitted_at` exists per `043_alpaca_submissions.sql:25`).
- Exit-leg join: a fill whose `parent_order_id` matches a submission's
  `alpaca_order_id` is that signal's exit; compute `exit_slippage_bps` vs
  the signal's stop/target level (signed adverse-positive) and persist on
  `signal_pnl` (new nullable column).
- Digest: `fill_slippage:` line in the `#trade-reports` daily post
  (`send_report.py:392`, pattern of `bench_realized_line` :658-662):
  n, mean/median/p90 entry bp (from mig-145 `fill_slippage_bps`), n/mean
  exit bp, latency median (filled_at − submitted_at), Σ$ cost, and a verdict
  vs our per-ticker half-spread artifact (`unified_backtest.py:94-118`):
  OK if median ≤ 1.5× modelled, WARN ≤ 3×, FAIL above. Never QD's crypto bands.
- Tests: migration applies; ingest dedups; exit join + bp math on a fixture;
  digest line renders with n=0 as "n/a".

### B3 Per-ticker ownership ledger (item 15)
- Nightly (in `reconcile` step): for each ticker, `account_qty` (broker
  positions), `signal_qty` (Σ open `execution_signals` qty by side),
  `unknown = account_qty − signal_qty`, `status ∈ {ok, unallocated,
  shortfall}` with tolerance 0.1 % extra / 0.5 % shortfall / 1-share dust.
  Persist to `position_ownership(cycle_date, ticker, account_qty,
  signal_qty, unknown_qty, status)` (append-only). Log only on status
  transition.
- Enforcement (flag `OPENCLAW_OWNERSHIP_BLOCK=1`, default unset = report
  only): the sizer skips OPENS and ADDS on tickers whose latest status ≠ ok;
  exits and flattens are never blocked. Surface a `[ownership]` line in the
  trade step and a system check `position_ownership_clean`.
- Tests: fixture broker vs signals produces the expected statuses; the
  sizer blocks an open and allows a reduce when the flag is set.

## 3. Stream C — risk (items 4, 5, 16 + circuit breaker)

### C1 Alpha-sleeve drawdown + daily-loss breaker (item 4)
- Inputs: NAV series `logs/pnl_daily_ohlc.json` (`bench_realized.py:23,40-43`,
  written by `server.js:2546,2671`) and the broker's live equity; SPY sleeve
  value = benchmark-ticker market value; `alpha_nav = equity − sleeve_value`.
  Opening equity snapshot per session (new `account_daily_open` table or the
  first OHLC sample of the day) with `estimated=true` when reconstructed.
- Rules: `dd = alpha_nav / rolling_peak(alpha_nav) − 1 ≤ −0.10` OR
  `(equity − opening_equity)/opening_equity ≤ −0.03` ⇒ BREACH. Evaluated
  by the existing 5-min RTH cron that runs `position_circuit_breaker.py`
  (`src/engine/cron-schedule.js:810-820`) — no new thread. **All regimes.**
- Action on BREACH (flag `OPENCLAW_ACCOUNT_BREAKER=1`; unset = log a
  `[account_breaker] shadow …` line only): write `account_breaker_state`
  (`halted=true, reason, breached_at, peak, dd, daily`), flatten every
  NON-benchmark open position via the existing orphan-close path
  (`open_reconcile.flatten_signal_close`, `regime_liquidator` primitives —
  RTH only, poll-to-terminal), post to `#trade-reports`, and have the sizer
  refuse alpha OPENS/ADDS while `halted`. `S_beta_spy` positions and entries
  untouched. Failed close submissions retry next tick (state stays
  `pending_flatten`).
- Re-arm: operator only — `OPENCLAW_ACCOUNT_BREAKER_REARM=<breached_at iso>`
  in `.env` clears that specific halt (so a stale value cannot re-arm a
  later breach). Peak resets to current alpha NAV on re-arm.
- Tests: synthetic NAV paths trip each rule; regime does not exempt;
  benchmark positions never in the flatten list; re-arm token semantics.

### C2 Per-position circuit breaker: no regime exemption (ruling R2)
- `src/execution/position_circuit_breaker.py` header + `:8-9`: remove the
  "skips HIGH_VOL/CRISIS (independent mode)" branch so the 2 %-of-NAV
  per-position cutoff runs in every regime. Update its docstring, the spec
  reference note, and tests.

### C3 Macro-event calendar + T-1 entry block (item 5)
- Master `data/master/macro_events.parquet` (append-only, dedup on
  (event, scheduled_at)): columns `event` (FOMC_DECISION, CPI, NFP, PCE,
  GDP_ADV, FOMC_MINUTES), `scheduled_at` (UTC), `session_date` (NYSE
  session via `lib.trading_calendar`), `source`, `ingested_at`. Sources,
  keyless: Fed FOMC calendar page (annual schedule, HTML),
  BLS release schedule (CPI/NFP; HTML/ICS), BEA schedule (GDP/PCE). Nasdaq
  economic calendar as an optional cross-check only. Ingester
  `src/ingestion/ingest_macro_events.py` + monthly timer
  `openclaw-macro-events.timer` + one-off backfill 2017→2027 (FOMC
  schedule is published a year ahead). System check `macro_events_fresh`
  (next event ≥ 30 d ahead present).
- Gate: in the sizer, before rule C / acting gate, if `today ∈ [T-1,
  T]` of any event with `importance=high` (FOMC_DECISION, CPI, NFP), drop
  every NEW open/add (`entry_blocked_reason='macro_event:<name>'`); exits,
  reductions and flattens untouched. **All regimes.** Benchmark sleeve
  included unless `OPENCLAW_EVENT_GATE_EXEMPT_BENCH=1`. Flag
  `OPENCLAW_EVENT_GATE=1`; unset = shadow line `[event_gate] shadow …`
  listing what would have been blocked.
- Backtest mirror: `unified_backtest` skips entries on the same sessions
  when `OPENCLAW_EVENT_GATE=1` (backtest authoritative — the live gate must
  not exist without its backtest twin). Include in the post-atr_r epoch
  drop-in ONLY if the operator confirms after seeing the shadow lines;
  otherwise the flag stays shadow live and off in backtest.
- Delete the stale `S_fomc_presell_spy_long` registry row (Stream A
  hygiene). Re-testing the pre-FOMC drift is a research item, not this spec.

### C4 Wire the social term in the premarket scorer (item 16)
- `src/pipeline/run_premarket_scan.py:191-192,207-208,231` hardcode
  `social_post_count_window=0`, `social_bear_ratio=0.0`. The Reddit /
  StockTwits stages (`run_sentiment_step.py:239-262`) run via
  `scripts/run_premarket_news.py` at 9:05 ET (`cron-schedule.js:408`).
  Read their per-ticker output (grep the tables/JSON they write) for the
  scan's tickers and pass real values; if absent, keep 0 and log
  `[premarket] social=absent`. Add `social_source` to the scan's log line.
  Tests: scorer receives non-zero social inputs from a fixture file.

## 4. Stream D — research lane (items 6, 7, 8, 9)

### D1 Rank-IC / quantile-turnover / decay screen (item 6)
- New `src/research/factor_ic_screen.py`: given a strategy's signal
  panel (or a factor column), compute per-rebalance Spearman rank IC at
  horizons 5/10/21 sessions, ICIR (√(252/H)), first-/second-half IC,
  rank autocorrelation, quintile long-short mean return, monotonicity,
  Jaccard turnover × round-trip cost. Uses
  `factor_prescreen.load_price_window` (`:391-436`) sliced to 2 years —
  never the full panel. Output JSON `{ic: {H: …}, icir, ic_half, rank_ac,
  ls_q5q1, monotonic, turnover, verdict ∈ {pass, weak, flat}}`.
- Wire into the research orchestrator after red-team and before the
  backtest slot (`research-orchestrator.js` around `:1041`); `flat` ⇒ skip
  the backtest and record the reason; `weak` ⇒ proceed but annotate.
  Thresholds: flat if |IC_5| < 0.01 AND |ls_q5q1| < cost; weak if ICIR <
  0.3. Flag `OPENCLAW_IC_SCREEN=1` (unset = compute + log only).
- Tests: a synthetic factor that is next-week return + noise scores pass;
  pure noise scores flat; a reversed factor scores negative IC.

### D2 Outcome-calibrated auto-approve floor + evidence cap (item 7)
- `src/strategies/proposal_manager.py:311` reads a static floor; `:403`
  and `:473` default 0.8 vs 0.85 at `:311`. Unify the default at 0.85 in
  ONE constant.
- `src/metrics/mastermind_calibration.py` already computes per-bucket
  hit rates over `mastermind_proposal_outcomes` (30-d live-Sharpe direction
  match, Brier). Add `calibrated_confidence(raw, bucket_table) =
  raw × clip(hit_rate(bucket)/bucket_midpoint, 0.5, 1.0)` when the bucket
  has n ≥ 8, else `raw`; and an evidence cap
  `cap = {none: 0.35, low: 0.55, medium: 0.75, high: 1.0}` keyed on the
  proposal's decisive-window closed-trade count (<10 none, <30 low, <100
  medium) and staleness (>45 d ⇒ one level down). `auto_approve` compares
  `min(calibrated, cap)` to the floor; records raw, calibrated, cap, and
  which bound bit in the proposal row. Flag `OPENCLAW_PROPOSAL_CALIBRATED=1`
  (unset = compute + log the would-be decision).
- Restore a calibration addendum to the sizing-proposal prompt
  (`comprehensive_review.js:275-280` removed it 2026-05-19): the bucket
  table from the doctor check, verbatim, as the corpus curator already gets
  (`mastermind.js:632-725`).
- Tests: bucket remap math; cap by count and staleness; floor compare uses
  the min; env-unset path logs and does not change the decision.

### D3 Zero-signal WARN gate (item 8)
- `src/strategies/validate_strategy.py:172-190` computes `signal_count` on
  synthetic bars; `:216-217` `ok = len(errors)==0`. Add
  `warnings.append('zero_signals_synthetic')` when 0 and the strategy is
  not `calendar_edge` / regime-gated (read the manifest flags). In
  `research-orchestrator.js` (~`:1330`), on that warning skip the red-team
  LLM call and mark the candidate `needs_signal_check`; never BLOCK.

### D4 Garman-Klass range-vol low-vol variant (item 9)
- New `src/strategies/implementations/S_low_volatility_us_gk63.py`
  cloned from `low_volatility_us.py` (252-d close-to-close std decile):
  rank on 63-session Garman-Klass variance
  `0.5·ln(H/L)² − (2ln2−1)·ln(C/O)²` (mean over the window), same universe,
  same rebalance cadence, same decile. Register in `registry.py` +
  `manifest.json` as `candidate`; requirements file mirrors the parent.
  Goes through the normal gates; no special treatment.

## 5. Stream E — reliability + measurement (items 10, 11, 12, 13, 18)

### E1 One owned, renewed run lock (item 10)
- Today: Python `LOCK_KEY='pipeline:running'` (`pipeline_orchestrator.py:38`),
  `r.set(key,'1',nx=True,ex=7200)` (:158), `release_lock` only in
  `finally` (:826), `return 0` on "already running" (:779-782);
  JS `engine:run_lock:<date>` (`daily-cycle.js:141-143`).
- Change: ONE key `pipeline:run_lock:<date>` used by BOTH; value
  `host:pid:start_iso`; TTL = current step's timeout + 120 s, renewed by
  the step runner before each step (`pipeline_orchestrator.run_step`
  `:603`, JS `runSubprocess`). Acquire: if held, read the value; if the PID
  is dead on this host (pattern `src/lib/manifest_lock.js:25`) take over
  and log `stale lock from pid N`; else exit **non-zero (rc=75)** with
  `[lock] held by …` and post to Discord via the failure notifier. Remove
  the `return 0` path. `--force-resume` keeps its bypass but logs loudly.
- Tests: acquire/renew/takeover with a fake Redis; both twins agree on the
  key (a JS test and a Python test read the same constant from one place:
  `src/lib/run_lock_key.json` or an env-less shared constant).

### E2 MemoryMax on daily-cycle steps + `OnFailure=` on 20 units (item 11)
- `pipeline_orchestrator.run_step` (`:603`, bare Popen) and
  `daily_cycle_helpers.runSubprocess` wrap the child in
  `systemd-run --scope -p MemoryMax=<OPENCLAW_STEP_MEMORY_MAX, default
  4500M>` using the existing `src/lib/capped_spawn.js` contract (Python:
  a 20-line twin `src/lib/capped_spawn.py`). rc=137 already maps to the
  bounded retry (`:871-882`).
- Drop-ins `OnFailure=openclaw-failure-notify@%n.service` for the 20
  services listed in the fit review (cboe-chains, options-eligibility,
  options-archive, fmp-profiles, edgar-shares, finra-short-interest,
  macro-rates, nasdaq-earnings-calendar, rf-flip, options-surface-flip,
  fleet-overnight-resume, premarket-scan@, afterhours-stop-monitor,
  afterhours-tp-premarket, afterhours-tp-monitor, afterhours-redeploy,
  amcheck, research-commit, research-retry@, sp5-cleanup,
  refresh-universe-sizes, premarket-realized-backfill — verify the exact
  unit names with `ls /etc/systemd/system`; never on `failure-notify@`
  itself). Snapshot into `docs/systemd/`.

### E3 Per-process heartbeat / identity (item 12)
- `src/lib/proc_heartbeat.py` (+ JS twin): on start and every 60 s, write
  Redis hash `proc:<host>:<pid>` = `{argv, step, rss_mb, started_at,
  updated_at}` with TTL 180 s; called by the orchestrator step runner,
  fleet children (`refresh_backtests_resumable.js` spawn), collectors and
  the premarket scan. System check `proc_registry` lists live entries;
  `co-tenant` doctor line names any python > 1 GB by argv. Port the
  stdout-idle wedge detector (`pipeline_orchestrator.py:600-633`) to
  `daily_cycle_helpers.runSubprocess`.

### E4 AST import allowlist for LLM-written strategies (item 13)
- `src/strategies/strategy_lint.py`: `ast.walk` over the candidate file;
  allow `import`/`from` roots {strategies, numpy, pandas, scipy,
  statsmodels, sklearn, math, datetime, typing, dataclasses, collections,
  itertools, functools, logging, json, re, pathlib (read-only use),
  lib.trading_calendar}; reject `os`, `subprocess`, `socket`, `requests`,
  `urllib`, `http`, `shutil`, `sys` (except `sys.path`? no — reject),
  `builtins.open` calls, `__import__`, `exec`, `eval`, `compile`,
  `importlib`. Run before the first import in
  `research-orchestrator.js:1041` (and in `validate_strategy.py:80-87`);
  violation ⇒ candidate rejected with the list of offending nodes; existing
  fleet files are linted once in a test that asserts zero violations (fix
  or allowlist any legitimate hit found).

### E5 Live KPIs: profit factor, expectancy, payoff, MAE, calendar (item 18)
- Extend the portfolio stats in `src/channels/api/server.js:1225-1269`
  (epoch-scoped like today) with `profit_factor`, `expectancy_pct`,
  `payoff_ratio`, `mae_median_pct` (needs per-signal MAE — from
  `trade_daily_marks` low/high vs entry), `monthly_returns` (from the OHLC
  NAV store) and `win_days/lose_days`. Render as one row of tiles + a
  12-month strip on the portfolio page. Read-only SQL; no schema change
  except MAE if it needs a column.

## 6. Sequencing

1. Stream E1/E2 + B1 + C4 first (safe, no gate effect; land on a Sat/Sun
   so the Monday cycle picks them up together). johnbot restart required
   for the JS runner changes.
2. Stream A1/A2/A3 flagged; measurement script; hygiene. Merge; epoch A4
   only after the atr_r flip resolves (Tue 09-15 21:45Z).
3. Stream C1/C2/C3 shadow first (two clean shadow days), then flip C1 and
   C3 by operator confirmation of the shadow lines.
4. Stream B2/B3, D1–D4, E3–E5 in any order; D4 needs a fleet slot
   (nightly).

## 7. Verification (before any "done")
- Per task: pytest on the task's tests + the touching module's existing
  tests, chunked per the box rules.
- Per stream: `python3 -m system_checks --tag <domain>`; `doctor.py --quick`.
- Merge gate: whole-branch review (fable) + fix wave; changelog entry;
  `docs/systemd/` snapshot for any unit change.
- Ops proof: the first live cycle after each merge is watched in
  `#trade-reports` / `#botjohn-log` for the new lines (`[lock]`,
  `fill_slippage:`, `[account_breaker] shadow`, `[event_gate] shadow`,
  `[ownership]`, `[premarket] social_source=`).
