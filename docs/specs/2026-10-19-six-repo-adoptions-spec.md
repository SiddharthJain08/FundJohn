# Six-repo adoptions — spec (2026-10-19)

Source: the six-repo fit review (TDengine, TimescaleDB, QuestDB, diffrax,
py-pde, cvxpy) — artifact `https://claude.ai/artifact/JhFXcMffLBmdyZWWdB1eVq`,
memory `project_six_repo_fit_review_20261019`, reviewer notes with every
file:line under `/root/.learnings/reviews/2026-10-19-six-repo-fit-review/`.
Operator approved ALL 25 actions on 2026-10-19 18:05 UTC ("go ahead with all
25 actions in the order you deem fit"). Controller rulings on the six owed
decisions:

- **R1** Tier 1 as written. The two one-off host operations — the
  `prices.parquet` re-sort (A1) and the two index drops (A6) — are run by the
  controller as transient systemd units outside the compute window
  (never between 18:45Z and 20:45Z on a trading day, never while a fleet
  unit is active), never by an implementer.
- **R2** Macro availability (D1) lands behind `OPENCLAW_MACRO_PIT=1`
  (default off, byte-identical when unset), writes `macro_pit` into
  `config_json`, and joins the existing pit/gap flip gate (G1 uniformity now
  requires `gap_fill='open'`, `financials_pit=true`, `macro_pit=true`).
  The pit/gap fleet epoch — already owed and now unblocked (atr_r is live
  since `OPENCLAW_TARGET_MODE=atr_r`) — is the last step of this campaign and
  flips all three flags together. Never stack epochs: the epoch starts only
  after every code stream has merged and after the weekend research units.
- **R3** `strategy_backtest_trades`: a materialised hot table
  `strategy_backtest_trades_primary` maintained inside the demotion
  transaction (E1). No DELETE, no archive, no partition move — superseded rows
  stay where they are. Archive policy remains an open operator decision; the
  hot table makes it non-urgent.
- **R4** Expression DSL (C7): compiler + lint integration + **three pilot
  conversions written as NEW candidate strategy ids** (`<orig>_dsl`), never
  in-place edits of approved strategies, so nothing re-gates and the pilots
  flow through the normal research promotion path.
- **R5** QP sizer (B6): shadow only — computes from the same inputs as the
  cascade, logs one `[qp_shadow]` line per cycle with the L1 distance to the
  heuristic targets, never routes an order. Promotion is a later operator
  decision after ≥2 weeks of lines.
- **R6** Hive-partitioned prices master (A7): implemented as a dual-write
  behind `OPENCLAW_PRICES_HIVE=1` with reader support behind the same flag
  and a parity script; the single-file master stays authoritative until the
  operator flips. The re-sort (A1) is the prerequisite and ships first.

## 0. Non-negotiables (every task)

- Master parquets and canonical Postgres tables are append-only (repo
  CLAUDE.md). New tables/columns only; never DELETE, never rewrite history.
  A one-off re-layout that preserves every row byte-for-byte (A1) is a
  re-write of the container, not of the data, and is allowed under R1.
- Backtest side is AUTHORITATIVE (2026-08-07 ruling). Anything that changes
  a backtest input (D1) lands flag-gated, default off, and flips only with a
  fleet epoch.
- Every new behaviour that can change sizing, signals or gate outcomes
  ships behind an `OPENCLAW_*` env flag, default = pre-change behaviour,
  byte-identical when unset. Pure bug fixes with no gated consumer (B2 OU
  estimator, B3 MC seed/bridge, A2 windowed reads, A4 doc/dormant) need no
  flag; the plan says which.
- Never read the whole `prices.parquet` or `options_eod.parquet` in a test or
  an implementer session. Tests use synthetic frames; parquet tests write a
  tiny file to `tmp_path`.
- Tests reach the REAL Postgres when `.env` is importable — stub every DB
  gate in fixtures (`monkeypatch` the connect function); fixture tickers must
  not be real symbols (`ZZTEST1`, not `AAA`).
- The box is 2-core / 8 GB / no swap and shared with the live pipeline.
  Implementers run single-file pytest only, `nice -n 19`, never the full
  suite, never while a fleet unit is active.
- `strategy_lint.ALLOWED_ROOTS` stays as is; new shared modules live under
  `src/strategies/` (already an allowed root) or `src/lib/`.
- Observability: every new gate/shadow emits one log line per cycle with a
  fixed prefix; a missing line means the process died, never "nothing to
  report". Never silent.
- Commit per task; edits, `git commit` and `git push` as separate tool
  calls (ERR-20261018-001).

## 1. Stream A — storage layout and reads (actions 1, 2, 6, 7, 18, 19, 20)

**A1 — writer discipline + one-off re-sort (action 1).**
`src/data/parquet_store._atomic_write` gains `write_page_index=True` and an
optional `row_group_size` (default 131072). `append_dedup` keeps insertion
order (daily appends are date-monotone once the file is sorted). New
`scripts/resort_prices_master.py --dry-run|--apply`: DuckDB
`COPY (SELECT * FROM read_parquet(p) ORDER BY date, ticker) TO tmp` under
`SET memory_limit='1GB'` + spill dir, takes the master's `.lock`, verifies
row count + per-column hash equality (DuckDB `SELECT count(*), sum(hash(...))`)
between old and new before `os.replace`, keeps `prices.parquet.pre-resort-<date>`
as a sidecar backup for 7 days. New system check
`src/system_checks/checks/parquet_layout.py` (`prices_rowgroup_span`): FAIL if
<95 % of row groups span ≤60 days (footer stats only), WARN if page index
absent. The script is run by the controller (R1), not by an implementer.

**A2 — windowed reads (action 2).** `run_ticker_metadata_step.py` and
`market_cap_lookup.py` replace the full-master `read_parquet` with
`parquet_store.read_filtered(..., where="date >= :cutoff")` (cutoff = 60
calendar days before as-of) and compute the 20-day ADV / latest close with
DuckDB `arg_max(close, date)` + windowed `avg(close*volume)`. Identical
outputs on a synthetic master (test). `GBMPathGen.from_parquet` moves to
Stream B (shares the file with B3).

**A3 — trailing-window live price load (action 20).** `BaseStrategy` gains
`max_lookback_days: int = 0` (0 = undeclared). `engine.load_prices` reads
`filters=[('date','>=',cutoff)]` where cutoff = today − max(declared
`max_lookback_days` over strategies about to run, floor 3 years) when
`OPENCLAW_PRICES_TRAILING_WINDOW=1` (default off). A lookback audit script
`scripts/audit_strategy_lookbacks.py` greps `rolling(`, `ewm(`, `shift(`,
`iloc[-N` constants per strategy and prints declared vs inferred; strategies
whose inferred lookback exceeds the window are listed. Flip after one
`--dry-run` signals parity day shows identical signal sets.

**A4 — docs and dormant code (action 7).** `close_proxy_snapshot.py:178`
docstring → 0.60 (the code is the behaviour; the env var is set nowhere).
`src/ingestion/ingest_prices_30m.py` (Polygon path, no unit references it)
is deleted; `docs/archive/changelog.md` records why.

**A5 — index drops (action 6).** Controller-run DDL, off-hours:
`DROP INDEX CONCURRENTLY idx_backtest_trades_strategy;` and
`idx_backtest_trades_regime` (both `idx_scan = 0`, lifetime). Recorded in a
migration file `164_drop_dead_backtest_trades_indexes.sql` that is idempotent
(`DROP INDEX IF EXISTS`) so a fresh clone matches.

**A6 — per-month options_eod files (action 18).** `parquet_store.write_options`
writes to `data/master/options_eod/<YYYY-MM>.parquet` (dedup only against the
current month's file) when `OPENCLAW_OPTIONS_MONTHLY=1`; `read_filtered`,
`max_date`, `row_count`, and the 21 non-strategy readers (listed in
`NOTES-timescaledb.md` F3) go through a new `parquet_store.options_dataset()`
that returns the single file or the directory as a pyarrow dataset. A one-off
`scripts/split_options_master.py` writes the monthly files from the current
master (DuckDB streaming, memory_limit 1GB) and verifies row counts per
month. Flag flips after the split + one collect cycle writes to the directory
and `row_count` agrees.

**A7 — hive-partitioned prices master (action 19, after A1 has soaked).**
`OPENCLAW_PRICES_HIVE=1` makes `write_prices` ALSO append to
`data/master/prices/year=YYYY/part-*.parquet` (date32 `date_d` column added,
`date` string kept) and makes `engine.load_prices`, `unified_backtest.load_prices_panels`,
`backtest_panel`, `asset_correlation` read via `pyarrow.dataset` with
`hive_partitioning`. `scripts/prices_hive_parity.py` compares the two
layouts (row count, per-ticker max date, sampled closes) and is the flip
gate. Single-file master stays authoritative until the operator flips.

## 2. Stream B — math fixes and the shadow QP (actions 3, 4, 5, 22, 23, 25)

**B1 — NNLS tangency (action 3).** `orthogonalization._tangency_side_sharpe`
solves `min_{x≥0} ‖Lᵀx − L⁻¹s‖²` with `scipy.optimize.nnls` where `R = LLᵀ`
(Cholesky of the shrunk R; fall back to eigen-clipped R if Cholesky fails);
Sharpe² = sᵀx*. The enumeration stays as `_tangency_side_sharpe_enum` and is
the test oracle (random sides n=2..8, |Δ| < 1e-9). `TANGENCY_MAX_ENUM` is
retired from the live path (kept for the oracle). All 12 invariants in
`tests/execution/test_tangency_sadj.py` must still pass unchanged.

**B2 — exact OU (action 4).** `pairs_scanner.ar1_half_life`: `phi = 1 + coef`;
require `0 < phi < 1`; `half_life = -ln2 / ln(phi)`. Same in
`S_pca_etf_stat_arb_reversion` (`kappa = -ln(b)`, `sigma_eq = std/sqrt(1-b²)`).
Test: on a simulated OU with known κ, the new estimator's bias at HL=2 d is
< 3 % (old is ≈18 %).

**B3 — MC seeding + Brownian-bridge crossing + parquet read (action 5).**
`intraday_path_montecarlo.run_path_mc(..., seed=None)`: `seed = seed if seed
is not None else zlib.crc32(f"{strategy_id}|{regime_state}|{proposal_id}|{stop}|{target}|{max_hold}".encode())`;
per-trade RNG = `random.Random((seed, trade_idx))`; `seed` persisted — migration
`165_mc_runs_seed.sql` adds `seed BIGINT` to `strategy_regime_intraday_mc_runs`
and `strategy_regime_mc_runs` (or whichever table `regime_param_montecarlo`
writes — the plan names it). `apply_policy` gains `bridge: bool = True`: after
the close check, if the barrier was not touched, sample a Brownian-bridge
crossing with `p = exp(-2·ln(S_{i-1}/B)·ln(S_i/B)/σ_bar²)` using the
generator's per-bar σ; `OPENCLAW_MC_BRIDGE=0` disables. Test: for pure GBM
with a stop, the MC stop-hit rate converges (n=20k paths, tolerance 1.5 pp)
to the reflection-principle first-passage probability; without the bridge it
is measurably below. `GBMPathGen.from_parquet` uses
`pq.read_table(path, columns=[...], filters=[('ticker','==',ticker)])`.
MC Sharpe annualisation: `sqrt(252 / max(hold_days, 1))` per trade (the
plan carries the exact line).

**B4 — breaker first-passage KPI (action 23).** `account_breaker.p_trip_within(days=21)`:
reflection principle for drifted BM with σ from the last 60 alpha-NAV daily
returns (`nav_ohlc_path()`), μ = 0, headroom `a = dd − DD_LIMIT`. Appended to
`format_line` as `p_trip_21d=0.38`. Advisory only; never gates; `n/a` when <20
NAV points.

**B5 — convex helpers (action 25).** `src/lib/convex.py`: `tangency_weights(mu, Sigma, long_only=True)`
(`min yᵀΣy s.t. μᵀy = 1, y ≥ 0`, Clarabel, returns normalised `p`) and
`min_evar_weights(L, z)` (perspective of log-sum-exp, exponential cone).
`S_markov_frontier_regimes` and `S_evar_tempered_stable_sector_etf` are
NOT edited (approved strategies; re-gate); the helpers ship with tests
against SLSQP on small problems and a StrategyCoder note. cvxpy and clarabel
are pinned in `requirements.txt`.

**B6 — shadow QP sizer (action 22).** `src/execution/qp_shadow_sizer.py`:
DPP-compiled problem over x ∈ ℝⁿ (USD): minimise `‖x−h‖²/NAV² + τ‖x−cur‖₁/NAV`
subject to `sgnᵀx = λ·NAV` (gross EQUALITY), `|x| ≤ cap_i`, `sgn∘x ≥ 0`,
`|1ᵀx| ≤ 0.6·λ·NAV`, `x_bench ≤ frac·NAV`; `solver=cp.CLARABEL` explicit;
τ default 0.0 (`OPENCLAW_QP_SHADOW_TAU`). Inputs are the cascade's own
`target_usd` (h), broker `cur`, caps and λ, taken at the point where
`_emit_orders_from_targets` receives its targets. Emits
`[qp_shadow] n=… l1_dist=… gross_qp=… gross_cascade=… spy_qp=… spy_cascade=… solve_ms=… status=…`
every trade cycle when `OPENCLAW_QP_SHADOW=1`; `status=infeasible|error:<cls>`
on failure, never raises into the sizer. Never routes.

## 3. Stream C — strategy-authoring layer (actions 9, 10, 11, 12, 13, 14, 15)

**C1 — mechanical lint rules (action 9).** `strategy_lint.py` gains rules
(WARN first; a `--strict` switch turns them FAIL; the orchestrator keeps
WARN until a fleet sweep is recorded in the changelog):
`negative_shift` (`.shift(-k)`/`lag(-k)` with a negative constant),
`unguarded_parquet_read` (`read_parquet`/`read_csv` call in a strategy file
whose module does not import `strategies.asof`), `rolling_without_min_periods`,
`raw_epsilon_literal` (`1e-6`…`1e-12` literals outside `safe_ops`).
Fleet census printed by `python -m strategies.strategy_lint --census`.

**C2 — windows (action 10).** `src/strategies/windows.py`: `state_window(key, *, nan_breaks=True) -> pd.Series[int]`,
`statecount(cond) -> pd.Series[int]`, `stateduration(cond) -> pd.Series[int]`,
`event_window(start, end, *, close_unfinished=False, same_bar_single=False, start_true_for=0, end_true_for=0) -> pd.Series[int]`
(episode id, 0 = flat), `session_gaps(index, tol) -> pd.Series[int]`,
`episodes_to_trades(episode_ids, price, dates, *, regime=None) -> list[dict]`.
Vectorised, causal, NaN-explicit. Parity tests against two existing loops
(`S_golden_cross_trend_signal_tv2` and `S_czar_loss_btc_directional`, copied
into the test as fixtures, not imported).

**C3 — indicators + safe_ops (action 11).** `src/strategies/safe_ops.py`:
`EPS = 1e-9`, `safe_div(a, b, fill=0.0)`, `safe_log(x)`, `clip_pos(x)`.
`src/strategies/indicators.py`: `atr(bars_or_close, n=14, *, method='true_range'|'close_diff', fallback_pct=0.02)`,
`realized_vol(close, n=20, *, log=True, annualize=252)`, `zscore(s, n, *, min_periods=None)`,
`mavg(s, n)`, `csum(s)`, `diff(s, *, ignore_negative=False)`, `rsi(close, n=14)`,
`sma`, `ema`. `BaseStrategy` exposes `self.ind` (module alias) and
`compute_stops_and_targets` keeps `method='close_diff'` (byte-identical).
`engine.py` HV20 and `backtest_panel` hv21 are NOT changed (re-gate risk);
a comment names the shared helper.

**C4 — crossing (action 12).** `windows.crossing(a, b, *, direction='up'|'down'|'any') -> pd.Series[bool]`
with the diffrax semantics (`prev ≤ && now >` for up); the two exit hooks are
NOT edited (approved strategies).

**C5 — session resampler (action 13).** `src/strategies/resample.py`:
`session_bars(wide, rule in {'W','M','Q'}, *, label='last_session', fill='none'|'prev')`
labelled on the last NYSE session of each bucket using
`src/lib/trading_calendar.py`; `next_session(date)`, `prev_session(date)`
re-exported. No existing strategy is migrated.

**C6 — event-study helper (action 15).** `src/research/event_study.py`:
`markout(events: DataFrame[ticker, event_date], prices_wide, offsets=range(-5, 21)) -> DataFrame[offset, mean_ret, median_ret, n]`
via DuckDB ASOF join on the trading calendar; CLI
`python -m research.event_study --master earnings|insider --since 2024-01-01`.

**C7 — expression DSL pilot (action 14).** `src/strategies/signal_expr.py`:
`compile_signal(expr: str, *, columns: set[str], ops=DEFAULT_OPS) -> Callable[[pd.DataFrame], pd.Series]`
using `sympy.parse_expr(expr, local_dict=..., evaluate=False)` with ONLY
declared columns as `Symbol`s and whitelisted ops as `Function`s
(`sma, ema, std, zscore, rank_pct, atr, rsi, pct_change, max, min, lag`);
rejects unknown symbols, unknown functions, `lag(n<1)`, any non-integer
window; compiles to a vectorised pandas callable over the panel; records
`required_lookback` and an AST hash (`formula_hash`). `BaseStrategy`
subclasses may declare `SIGNAL = "..."` and `SIGNAL_DIRECTION = 'long'|'short'`
plus `SIGNAL_THRESHOLD`; `generate_signals` default implementation evaluates
the compiled signal on the prefix panel and reads `iloc[-1]`. Three pilot
strategies as NEW candidate ids `<orig>_dsl` for `S_golden_cross_trend_signal_tv2`,
`S_amihud_illiquidity_premium`, `oxf_bollinger_momentum`, each with a parity
test showing identical signal dates on a synthetic panel vs the original's
rule. `generate_signatures.py` prefers `formula_hash` when present.
StrategyCoder prompt gains a DSL section.

## 4. Stream D — backtest engine and gates (actions 8, 16, 21)

**D1 — as-of helper + available_at (action 8).** `src/strategies/asof.py`:
`asof_align(right: DataFrame, left_index, *, on='available_at', by=None, tolerance=None, same_day='after_close'|'same_bar') -> DataFrame`
(merge_asof backward; `same_day='after_close'` ⇒ `allow_exact_matches=False`).
`aux_data_loader`: every master slice publishes `available_at`:
financials = existing PIT availability; macro = observation date + per-series
release lag from `MACRO_RELEASE_LAG_DAYS` (`INDPRO: 45`, `CPIAUCSL: 45`,
`UNRATE: 40`, `PAYEMS: 40`, `GDP: 90`, daily FRED series: 1; the plan lists
the series actually present in `macro.parquet`); insider = `date`
(filing date); options/iv = build date. `_macro_slice` uses `available_at`
when `OPENCLAW_MACRO_PIT=1`, else the legacy observation-date cut
(byte-identical). `config_json['macro_pit']` written unconditionally;
`scripts/pit_gap_flip_gate.py` G1 requires all three keys;
`scripts/pit_gap_flip_after_fleet.sh` flips all three flags. Parity test:
backtest `_macro_slice` vs the live `engine.load_aux_data` macro dict on one
synthetic frame, both flag states.

**D2 — indicator cache (action 16).** `BaseStrategy.cached(name: str, fn: Callable[[pd.DataFrame], pd.DataFrame | pd.Series], frame: pd.DataFrame)`:
computes `fn(frame_full)` once per (instance, name) and returns
`.loc[:frame.index[-1]]` when `frame.index` is a prefix of the cached index,
else recomputes. The full frame is supplied by the backtest loop via
`instance._cache_source = close_wide` before the OOS loop when
`OPENCLAW_BT_INDICATOR_CACHE=1` (default off); the live engine never sets
it, so `cached` degrades to a plain call. Parity assert in
`scripts/validate_strategy.py`: cached slice == fresh compute on 3 random
dates (`OPENCLAW_BT_CACHE_PARITY=1`). Measurement script
`scripts/measure_indicator_cache.py --strategy S_x` runs one strategy both
ways on a synthetic 1,000×2,746 panel and prints wall time + byte-identity of
the signal list. Opt-in per strategy; no existing strategy is migrated in
this campaign; three strategies named in the plan get a `_cached` NEW
candidate id for measurement.

**D3 — backtest trackers (action 21).** `src/backtest/trackers.py`:
`Tracker` protocol (`initialize(ctx)`, `handle(ctx, bar_idx)`, `finalize(info: dict)`),
`TrackerCollection`, `ExitCensusTracker`, `HookTracker`, `IntradayBarTracker`
(the existing counters, moved), `MaxRuntimeTracker(seconds)` raising
`FinishedBacktest`, `ConsistencyTracker` (abort on non-finite mark).
`unified_backtest._per_bar_simulate` calls the collection per bar; the
run JSON at `:1826-1899` is assembled from `run.info`. Guard: trade list and
`config_json` byte-identical before/after on three stored runs (`scripts/measure_tracker_parity.py`
replays from `strategy_backtest_runs.config_json`); `OPENCLAW_BT_MAX_RUNTIME_S`
default 0 (off).

## 5. Stream E — database and ops (actions 17, 24)

**E1 — hot table (action 17).** Migration `166_backtest_trades_primary.sql`:
`strategy_backtest_trades_primary` (same columns, PK `(run_id, trade_seq)`,
index `(strategy_id, entry_date)`), backfilled from the current primary runs
inside the migration (INSERT … SELECT, chunked by strategy to bound WAL).
`unified_backtest` persist path: inside the same transaction as the
demotion UPDATE, `DELETE FROM strategy_backtest_trades_primary WHERE strategy_id=%s`
then INSERT the new run's rows (this table is a cache, not a canonical table —
the CLAUDE.md append-only list is unchanged). Readers switched:
`strategy_similarity.py` (3 queries), `server.js:1886`, `backtest_panel.py:251,327`,
`backtest_coupled_recs.py:255`, `run_universe_shrink.py:192`. Consistency
system check `backtest_trades_primary_sync` (row count per primary run equal).

**E2 — job-stat table (action 24).** Migration `167_scheduled_job_runs.sql`:
`scheduled_job_runs(id, job_name, source in ('systemd','cron'), started_at, finished_at, success bool, rc int, host, pid, note)`,
written by `src/lib/job_stat.py` (`record_start(job)`, `record_finish(job, rc)`)
and `src/lib/job_stat.js`; wired into `cron-schedule.js`'s 26 entries via one
wrapper and into systemd units via `ExecStartPost`/`ExecStopPost` drop-in
snapshots under `docs/systemd/` (NOT installed by implementers). System check
`scheduled_jobs_overdue`: a job whose last success is older than 2× its
expected interval WARNs. No backoff/retry semantics (TimescaleDB's bgw retry
is explicitly not lifted).

## 6. Sequencing

1. Streams B and A (Tier-1 parts A1 writer+script, A2, A4, A5 file) run
   first, ≤2 implementers concurrently across streams, each on its own
   worktree; merge each stream to main when its final review is clean.
2. Controller host ops after the first merge, off-hours: A1 re-sort
   (transient unit), A5 index drops.
3. Streams C and D next; D merges after C (both touch `base.py`; D rebases).
4. Stream E, then A6/A7/A3 (the bigger storage jobs) as a second Stream A
   wave.
5. Final: C7 pilots and D2 `_cached` ids registered as candidates; pit/gap
   epoch armed (R2).

## 7. Verification

Per task: single-file pytest green, `-W error` clean, reviewer approval.
Per stream: scoped whole-branch review on opus; merged-tree tests for the
files touched. Host ops: before/after footer stats (A1), `pg_relation_size`
(A5), `row_count` parity (A6/A7). Live watch: first trade step after the B6
merge shows a `[qp_shadow]` line; first backtest after D3 shows identical
`config_json` keys; `prices_rowgroup_span` check PASS after A1.
