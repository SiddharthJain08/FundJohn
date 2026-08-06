# Handoff spec — open remediations from the 2026-08-05/06 signal-outage investigation

**Status:** HANDOFF. Nothing below has been started. To be executed as a Fable task
in a fresh session.
**Grounded against the tree at commit `c697d62`.** Every path, line number, flag,
table and count below was verified live on 2026-08-06T09:04Z — not recalled.
**Scope:** defects *discovered* in the 08-05/06 session and *not yet begun*. Work
already shipped in that session is listed in §1.1 for context only — do not redo it.

---

## 0. Read this first

### 0.1 Standing constraints — non-negotiable

* 🔴 **NEVER DELETE FROM THE MASTER DATABASE.** `data/master/*.parquet` and the
  canonical PG tables (`execution_signals`, `signal_pnl`, `alpaca_submissions`,
  `data_coverage`, `data_columns`) are **append-only**. Columns/tickers/dates may
  only be ADDED. Any deprecation is a flag (`active=false`), never a `DELETE`.
* Never print or echo secret values from `.env`. Extract with
  `grep -m1 '^KEY=' .env | cut -d= -f2-`. **Never `source` .env.**
* `johnbot` is **user-scope**: `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart
  johnbot.service`. **Never start the system unit.**
* Never `git reset --hard`.
* Box is **2-core / 8GB / NO SWAP**. Serialise heavy work, `nice -n 19`. Run long
  compute as a **transient systemd unit** — the agent harness reaps
  session-attached tasks.
* Never leave the tree half-edited across a timer boundary (timer-spawned scripts
  pick up the working tree on their next run).
* Don't run the full test suite while the fleet is running
  (cross-contamination: 15 failed / 45 errors only in combined runs).
* Commits end with
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
* Ask before creating persistent state (new services, new timers, new bots).

### 0.2 Verification gotchas that cost time in the last session

1. 🔴 **`engine.py` accepts `--dry-run` and IGNORES it.** `_parse_run_date` uses
   `parse_known_args` *specifically* so "orchestrator-injected flags the engine
   doesn't implement (--dry-run)" don't crash it. **`PIPELINE_DRY_RUN=1` does NOT
   make the signals step dry — it writes real signals.** There is no safe way to
   rehearse the signals step against production. Measure sub-functions in
   isolation (read-only) instead. This is itself an open item — §7.
2. **Implementation filenames are Capitalised** (`S_earnings_sue_pead.py`). A
   lowercase `grep -ix` matches nothing and silently "proves" the opposite of the
   truth. Prefer `implementations/<id>.requirements.json` — it is the *declared
   data contract* and is unambiguous.
3. **Do not `pd.read_parquet('data/master/options_eod.parquet')` without
   `columns=`/`filters=`.** It OOM-killed a read-only probe on this box.
4. **Read counters, not exit codes.** The recurring failure class here is a
   command that exits 0 having skipped the work (`run_universe_shrink` without
   `--force`; `systemctl enable --now` firing a `Persistent=true` catch-up).
5. **A `pgrep -f <script>` self-matches** an agent shell whose own command line
   contains the script name. Match on process NAME (`pgrep -x node`) then grep the
   cmdline.
6. **Orphaned-but-plausible code is a live pattern in this repo.** Two confirmed
   cases: `correlation_matrix.py` (deleted `d4271f1`) and
   `fetch_fmp_earnings_calendar` (§3). **Before trusting any docstring that says
   "called daily by X", grep for an actual caller.**

### 0.3 Useful invocations

```bash
export POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-)
PYTHONPATH=src python3 -m pytest tests/execution -q          # NOT while the fleet runs
PYTHONPATH=src python3 -m system_checks --tag pipeline
```

---

## 1. Inventory

| § | Item | Severity | Blocked on |
|---|---|---|---|
| **2** | **36 approved strategies emit ZERO live signals; 32 need only data that is already fresh** | 🔴 highest | nothing |
| **3** | `earnings.parquet` refresh is orphaned dead code — stale since 2026-04-30 | 🔴 high | nothing |
| **4** | `financials` covers 819 of 5231 tickers | 🟠 medium | nothing |
| **5** | Signals step has **no retry** — one abort loses the trading day | 🟠 medium | nothing |
| **6** | The 2310MB OOM co-tenant is unidentified | 🟠 medium | needs a recurrence (instrumented) |
| **7** | `engine.py` ignores `--dry-run` — the signals step cannot be rehearsed | 🟠 medium | nothing |
| **8** | Flag polarity: make `1` uniformly mean "live" | 🟡 low-risk/high-care | operator confirm |
| **9** | `OPENCLAW_REDEPLOY_EXTENDED_HOURS` removal | 🟡 | **operator decision — premises disputed** |
| **10** | Live-vs-backtest intensity gap (LOW_VOL 3.2×, TRANSITIONING 5.9×) | 🟠 | §2 may subsume it |

Separate, already specced, **not in scope here**:
`docs/specs/2026-08-05-similarity-from-backtest-spec.md` (strategy similarity from
backtest data + dead-path removal).

### 1.1 Already shipped 08-06 — do NOT redo

| commit | what |
|---|---|
| `51dd557` | signals-step timeout 300s → tunable `OPENCLAW_SIGNALS_TIMEOUT_SECONDS` (default 900) in **both** twin resolvers, + parity test |
| `6fb923b` | alerting: `signals_persisted_today` no longer warns every day; separates "engine completed with 0" (WARN) from "engine never completed" (FAIL); added `signals_step_timeout_headroom`; health digest no longer renders 0 signals as ✅ |
| `9bccab0` | `load_prices` universe pushdown + numpy scatter — peak RSS 2608MB → 2061MB, output bit-identical |
| `c697d62` | engine logs peak RSS + MemAvailable + top-5 co-tenants on every run |

---

## 2. 🔴 36 approved strategies have NEVER emitted a live signal — 32 of them unexplained

### 2.1 The measurement

Across the entire live window (`execution_signals`, 2026-04-10 → 2026-08-04, 76
trading days), **36 of 97 approved strategies have zero rows**, against large
backtest histories:

| strategy | backtest trades | live signals |
|---|---|---|
| `S_visibility_graph_rsi` | **733,646** | 0 |
| `S_ivol_mispricing_asymmetry` | 577,975 | 0 |
| `S_schur_damped_minvar_shrinkage` | 277,218 | 0 |
| `S_ast_trend_following_effect_in_stocks` | 260,912 | 0 |
| `S_expected_idiosyncratic_skewness` | 240,661 | 0 |
| `S_intramonth_momentum_cycle` | 201,980 | 0 |
| `S_empirical_bayes_shrinkage_mv` | 104,661 | 0 |
| … 29 more, down to `S_sparse_cca_mean_revert` (452) | | 0 |

Reproduce:

```sql
SELECT r.name,
  (SELECT COUNT(*) FROM strategy_backtest_trades t WHERE t.strategy_id=r.name) bt
FROM strategy_registry r WHERE r.status='approved'
  AND (SELECT COUNT(*) FROM execution_signals s WHERE s.strategy_id=r.name)=0
  AND (SELECT COUNT(*) FROM strategy_backtest_trades t WHERE t.strategy_id=r.name)>0
ORDER BY 2 DESC;
```

⚠ `S_visibility_graph_rsi` was **already** an open single-strategy follow-up (the
"0-signal wiring bug" from the registry↔manifest drift work). **This is that same
defect class at 36 instances, not 1.** Treat it as fleet-wide.

### 2.2 The units are comparable — this is settled, don't re-litigate

The obvious objection is that `strategy_backtest_trades` rows are realised
round-trips while `execution_signals` rows are pre-sizing candidates. Checked —
`src/backtest/unified_backtest.py:854`:

```python
for sig in signals[:instance.MAX_SIGNALS]:      # base.py:123, MAX_SIGNALS = 50
```

**Both sides are the output of the same `strat.generate_signals()`.** The backtest
additionally CAPS at `MAX_SIGNALS` and drops tickers with no bar / asset-gated. The
backtest is the *constrained* side. So live emitting fewer is real, not an artifact.

### 2.3 Already ruled out — do NOT re-chase

| hypothesis | evidence against |
|---|---|
| they raise exceptions | `engine.run_strategies` (`engine.py:1321-1335`) logs `{id} FAILED` + traceback and counts `errored` separately from genuine zeros via `last_run_stats`. 08-04 logged `eod_compute_health: rc=0 ok=97/97`. They run **clean** and return `[]`. |
| universe excludes their tickers | the narrow ones trade **GLD, SPY, QQQ, IWM, TLT, XLE, XLF, EEM, EFA, SLV, SHY** — all present in the 5231-ticker live universe |
| regime gating | they are zero across **all** regimes, including cells where `strategy_regime_params.eligible` is true |
| **stale aux data** | **only 4 of 36** — see §2.4 |

### 2.4 Partition by declared data contract

Built from `src/strategies/implementations/<id>.requirements.json` `required`:

| bucket | count | members |
|---|---|---|
| requires a STALE master | **4** | `S_ast_earnings_announcement_premium`, `S_earnings_sue_pead`, `S_pre_earnings_vol_runup` (earnings); `S_ivol_mispricing_asymmetry` (financials) |
| requires **`prices` ONLY** | **31** | `S_visibility_graph_rsi`, `S_schur_damped_minvar_shrinkage`, `S_expected_idiosyncratic_skewness`, `S_intramonth_momentum_cycle`, `S_robust_min_variance_hedge`, `S_gold_trend_momentum_vol_target`, `oxf_rsi2_meanrev`, `S_btc_momentum`, `S_ast_short_term_reversal_in_stocks`, `S_bayes_stein_shrinkage_mvo`, `S_empirical_bayes_shrinkage_mv`, `S_ast_residual_momentum_factor`, `S_ast_trend_following_effect_in_stocks`, `S_triangulated_stat_arb_triplets`, `S_ast_fed_model`, `S_btc_equity_spillover`, `S_regime_age_momentum`, `S_52wk_low_capitulation_reversal`, `S_ast_rebalancing_premium_in_cryptocurrencies`, `S_downside_beta_premium`, `S_amihud_illiquidity_premium`, `S_ast_momentum_and_reversal_combined_with_volatility_effect_in_stocks`, `S_ast_market_sentiment_and_an_overnight_anomaly`, `S_overnight_intraday_tug_of_war`, `S_vwap_closing_pressure`, `S_sparse_cca_mean_revert`, `S_ast_momentum_factor_effect_in_stocks`, `S_ast_pairs_trading_with_country_etfs`, `S_wti_brent_spread_mean_reversion`, `S_split_session_cluster_garch_gmv`, `S_mvgarch_nig_crra_portfolio` |
| requires insider+prices (both FRESH) | 1 | `S_microcap_insider_purchase_momentum` |

⇒ **32 of 36 depend only on data that is fresh and complete** (`prices` max date
2026-08-05 / 5231 tickers; `insider` 2026-08-05 / 4508 tickers). **The dominant
cause is NOT data starvation and is still unknown.**

### 2.5 What to do

**Phase A — instrument once, diagnose all 32 (do this first; do NOT start
per-strategy).** The engine already distinguishes error/zero, but not *why* zero.
Add per-strategy diagnostics to `engine.run_strategies` around the
`generate_signals` call (`engine.py:1321`): input shape actually handed to the
strategy (`strat_prices.shape`, `len(strat_universe)`, aux keys present + row
counts), and wall time. One run then explains most of the 32 at once.

Leading hypotheses, in the order the evidence favours:

1. **Per-strategy universe slicing.** `OPENCLAW_LIVE_UNIVERSE_RESOLVER=1` makes
   `live_universe.build_strategy_universes` hand each strategy its OWN slice
   (`engine.py:2016`). A strategy whose resolved slice is empty/tiny gets a panel
   it cannot trade, while its backtest ran on the full chosen tier. **Check
   `strat_universe` length per strategy first** — this is the cheapest decisive
   probe and would explain a large fraction at once.
2. **Panel geometry.** Backtest hands `generate_signals` a per-bar slice; live
   hands the full wide panel. A strategy indexing on `.iloc[-1]` vs a date lookup
   can silently produce nothing.
3. **`apply_equity_calendar`** (`engine.py:1320`, gated
   `OPENCLAW_EQUITY_TRADING_CALENDAR=1`, currently **ON**) reshapes the panel for
   equity-class strategies only. Crypto keeps the union calendar.
4. **`MAX_SIGNALS`/cohort ranking**: `cohort_base.py:99` truncates. If the live
   ranking input is degenerate, the head is empty.

**Phase B** — only after Phase A, take the residual strategies individually.
Start with `S_visibility_graph_rsi` (largest backtest history, prices-only, and
already a known open bug).

**Done when:** every one of the 32 is classified as either (a) fixed and emitting,
(b) genuinely-correct silence with a written reason, or (c) demoted out of
`approved` so the registry stops claiming it is live.

⚠ **Do not "fix" a strategy into emitting by loosening its thresholds.** Several of
these were re-gated on honest costs deliberately; the goal is to explain the
silence, not to manufacture signals.

---

## 3. 🔴 `earnings.parquet` refresh is orphaned dead code

### 3.1 Evidence

| master | rows | tickers | max date | last_updated |
|---|---|---|---|---|
| **earnings** | 1,843 | **378** (7% of the 5231 the engine asks for) | 2026-07-15 | **2026-04-30** |

`src/ingestion/pipeline.py:546` defines `fetch_fmp_earnings_calendar`, whose own
docstring says:

> *"Called daily by pipeline_orchestrator to keep earnings.parquet current."*

**It has ZERO callers.** `grep -rn fetch_fmp_earnings_calendar` across the tree
returns only the definition, and
`grep -c earnings src/execution/pipeline_orchestrator.py` returns **0**.

Consequence, visible in every engine run:
`Earnings calendar loaded: 0 upcoming events` (`engine.py:904` — it filters
`date >= today`, and the newest row is 2026-07-15).

Related: `src/strategies/sync_data_ledger.py:70` records
`'earnings_calendar': 'yfinance',  # FMP earning_calendar bulk endpoint returns 403`
— so the FMP bulk endpoint was known-dead and the ledger was pointed at yfinance,
but no working refresh path was wired in its place.

### 3.2 What to do

1. Decide the source: the ledger says **yfinance**; `fetch_fmp_earnings_calendar`
   is FMP and its bulk endpoint 403s. Do not resurrect the FMP path without
   confirming the endpoint works on the current key.
2. Wire an actual daily refresh, and **register it in `system_checks`** so a
   3-month silence cannot recur (see §3.3).
3. Backfill the gap 2026-04-30 → today. **APPEND ONLY** — `earnings.parquet` is a
   master (§0.1). `scripts/backfill_earnings.py` exists (last touched Apr 16);
   audit before trusting it.
4. Delete or wire `fetch_fmp_earnings_calendar`; leaving an orphan with a lying
   docstring is what caused this.

### 3.3 The generalisable defect

This is the **third** confirmed instance of *plausible code nothing calls*
(`correlation_matrix.py`, `fetch_fmp_earnings_calendar`), and the second of *a
master silently ceasing to update*. Add a `system_checks` probe with tag `storage`
that FAILs when any master in `data/master/` has a `last_updated`/`date` max older
than its expected cadence. `src/system_checks/checks/` already has freshness
probes (`options_aux_freshness.py`, `metadata_snapshot_freshness.py`) — follow
their shape. **This probe is worth more than the earnings fix itself.**

---

## 4. 🟠 `financials` covers 819 of 5231 tickers

`data/master/financials.parquet`: 5,822 rows / **819 tickers**, max date
2026-06-30. The engine's own redeploy preflight asks for `{'financials': 5231}`.

Blocks at minimum `S_ivol_mispricing_asymmetry` (577,975 backtest trades, 0 live).
Same treatment as §3: find the refresh path, verify it has a caller, backfill
append-only, add the freshness probe. Lower severity than earnings only because it
is fresher and has broader coverage.

---

## 5. 🟠 The signals step has no retry

`daily_cycle_node.js` / `daily-cycle.js` contain **no retry logic**: `rc != 0`
aborts the run and nothing downstream executes. On 2026-08-05 that cost the entire
trading day's signals.

⚠ **Do not cite 07-29 as evidence a retry exists.** 07-29 was OOM-killed at 19:03
and `execution_runs` shows a successful 223s run at 19:16 — but that came from
`logs/sameday_retry_2026-07-29.log`, a **manual**
`pipeline_orchestrator.py --reason sameday-compute-retry` invocation. It is the
only such log that has ever existed and no code or systemd unit anywhere on the
box matches `sameday.retry`. **The recovery was a human.**

**Design care:** a retry must be idempotent w.r.t. `execution_signals` (append-only
master) and must not double-submit. Bound it (one retry), and make it emit a
distinct alert so a silent-retry pattern doesn't mask a degrading step. Note the
timeout is now 900s (`51dd557`), so a retry has room inside the 15:00→15:55 ET
compute→execute window — verify that arithmetic before implementing.

---

## 6. 🟠 The OOM co-tenant is unidentified

At the 08-05 14:05 kill the kernel recorded a **global** OOM
(`constraint=CONSTRAINT_NONE`): engine `anon-rss` 3.88GB **plus a second python3 at
2310MB**, both `oom_score_adj=100`, total **8134MB across 59 tasks vs 7939MB RAM**.
08-03 was the same shape (engine 4601MB + a 1732MB python3).

Ruled out — **do not re-chase**:
* not an engine child — `engine.py` only spawns the alpaca **Go** binary
* not the intraday HMM — it ticks :00/:15/:30/:45
  (`OPENCLAW_INTRADAY_15MIN_PREFETCH=1`); nothing at 14:05
* not `acting-ingest` — that ran at 18:30 on 08-05

`adj=100` is inherited from `johnbot.service`, so it is *some* johnbot-spawned
python. `c697d62` now logs peak RSS + MemAvailable + top-5 residents on every
engine run. **Wait for the next occurrence, then read the log** — the process is
gone by post-mortem time, which is exactly why this stayed unsolved.

`9bccab0` bought 547MB, which exceeds the overshoot on both days, so recurrence may
stop on its own. If it does, this item closes by observation. **Do not spend
effort here before a recurrence.**

---

## 7. 🟠 `engine.py` ignores `--dry-run`

`_parse_run_date` (`engine.py:1924`) deliberately swallows unknown flags so
orchestrator-injected `--dry-run` doesn't crash the step — with the effect that
**`PIPELINE_DRY_RUN=1` runs the signals step for real and writes to
`execution_signals`.**

This blocked profiling the OOM directly and will block validating §2 and §5. Give
the engine a real `--dry-run`: compute everything, log the summary, **skip the DB
writes** (`execution_signals`, `execution_runs`, `daily_signal_summary`, the
lifecycle passes). Guard with a test asserting no rows are written.

Note `PIPELINE_DRY_RUN` is documented (`resolve_script.js:9`) as "Each script's
`--dry-run` handler is responsible for skipping its own external writes" — the
contract exists; the engine just never implemented its half.

---

## 8. 🟡 Flag polarity — make `1` uniformly mean "live"

Operator directive from the 08-05 session. **Verified counts as of `c697d62`**
(an earlier estimate of "67 consumers" was wrong):

| flag | consumers | current live value | meaning |
|---|---|---|---|
| `OPENCLAW_EOD_SIGNAL_REGISTER` | **22** | `0` | `0` **selects same-day mode — the live configuration**. `1` = T+1 EOD semantics. |
| `OPENCLAW_CLOSE_EXEC_LIVE` | **7** | `0` | legacy into-close execution |

`engine.py:125-133` `_eod_signal_register_gate_on()` is the mode selector:
`1` ⇒ `target_date = T+1`; `0` ⇒ same-day `target_date = T`.
`doctor.py:434` **fails if both are 1** — they are mutually exclusive flows.

⚠ **This is a semantic inversion, not a config edit.** Setting
`EOD_SIGNAL_REGISTER=1` today would switch execution back to T+1. The safe method:
introduce a positively-named flag (e.g. `OPENCLAW_SAMEDAY_SIGNAL_TARGET=1`),
migrate all 22 consumers, keep the old name honoured for one epoch, then retire it.
Update `doctor.py:434`'s interlock in the same change or it will police the wrong
pair.

`OPENCLAW_SAMEDAY_EXEC=1` is already correctly polarised — use it as the model.

---

## 9. 🟡 `OPENCLAW_REDEPLOY_EXTENDED_HOURS` — BLOCKED, operator decision

The operator asked for this flag to be deleted, giving two reasons. **Both appear
to be wrong, so the change was held.** Do not delete it without an explicit
operator decision.

* *"we no longer have regime detection in extended hours"* — the intraday HMM cron
  is `'*/5 9-19 * * 1-5'` (`cron-schedule.js:681`) and its own comment says it
  extends "through 19:55 to cover Alpaca's after-hours session (16:00-20:00)".
  Regime detection **does** run in extended hours.
* *"this redeploy path is likely outdated"* — `scripts/redeploy_pipeline.py` is the
  live regime-change mechanism and was modified as recently as `02e7755` to add the
  intraday news gate.

4 consumer sites: `cron-schedule.js:679` (comment),
`redeploy_pipeline.py:15` (docstring), `:490` (comment), `:493` (the actual read).
It is `0`, so after-hours redeploys are already refused — the flag is the
off-switch for a path that still fires.

**If the real intent is "never trade after hours,"** the correct change is
retiring the extended-hours *execution* branch in `alpaca_executor.py` (`:1750`
`extended_hours` param, `:1787-1789` appends `--extended-hours`), not removing the
guard. Put that to the operator as the alternative.

---

## 10. 🟠 Live-vs-backtest intensity gap

Per eligible strategy×regime cell, signals per **active** day:

| regime | eligible cells | never fired live | backtest | live | live regime days |
|---|---|---|---|---|---|
| CRISIS | 29 | 29 | 102.4 | — | **0** |
| HIGH_VOL | 22 | 18 | 63.3 | 48.0 | 12 |
| LOW_VOL | 19 | 6 | 66.0 | **20.5** | 57 |
| TRANSITIONING | 14 | 11 | 25.2 | **4.3** | 28 |

⚠ **CRISIS 29/29 is EXPECTED, not a defect** — the system has never traded a CRISIS
regime (zero CRISIS rows in all live history). Do not report it as a finding.

⚠ **The 890→108 signals/day collapse at the week of 2026-07-13 is the deliberate
honest-cost re-gate**, not a bug. Do not chase it.

Real residual: LOW_VOL **3.2×** and TRANSITIONING **5.9×** below backtest, same
function, backtest capped. HIGH_VOL's 1.3× rests on only 12 days.

**No evidence links this to §2** — they may share a cause or not. Do §2 first; if
the §2 root cause is per-strategy universe slicing (§2.5 hypothesis 1), it very
likely explains this too, and this item closes for free. **Re-measure §10 after §2
before investigating it separately.**

---

## 11. Suggested order

1. **§2 Phase A** — one instrumented run explains the most, and gates §10.
2. **§3** earnings + **§3.3 the master-freshness probe** — independent, concrete,
   root cause already found.
3. **§7** real `--dry-run` — unblocks safe validation of everything else.
4. **§5** retry, **§4** financials.
5. **§10** re-measure (may auto-close).
6. **§8** flag polarity — care, not difficulty.
7. **§9** — only after the operator rules.
8. **§6** — only on recurrence.

---

## 12. Open questions for the operator

1. **§9** — is the intent to remove the guard, or to retire after-hours execution
   entirely? The two are opposite changes.
2. **§2** — for strategies whose silence turns out to be *correct*, should they be
   demoted out of `approved`? The registry currently claims 97 live strategies
   while 36 have never emitted anything.
3. **§8** — confirm the migration-with-alias approach is acceptable, since a
   straight rename is a flag-day change across 22 consumers.
