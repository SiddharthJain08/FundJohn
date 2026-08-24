# Five-Repo Adoptions Implementation Plan (P1–P3, R1–R5, S1–S3, X1)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Tasks are dispatched to subagents in batches; subagents DO NOT commit — the
> orchestrating session reviews, runs targeted tests, and commits sequentially.
> Checkboxes track completion.

**Goal:** Implement all approved adoptions from the 2026-08-24 five-repo diligence
(artifact 9e8ebbaa): sizing upgrades (P1–P3), research-gate upgrades (R1–R4),
swarm-research upgrades (S1–S3), and the new cointegration pairs strategy (X1),
with X1's heavy compute scheduled in overnight quiet windows through the week.

**Architecture:** Everything ships shadow-first or flag-gated where it can change
live behavior; deterministic gates stay authoritative; no cvxpy in the live path;
all new pipeline stages are code-enforced (never LLM-discretionary) with validated
JSON contracts.

**Tech stack:** Python 3.13 / pandas 3.0.2 / pypfopt 1.6.0 / statsmodels 0.14.6 /
quantstats 0.0.81 / scipy 1.15.3 (all verified installed); Node (johnbot curators);
PostgreSQL migrations; systemd transient units for heavy compute.

**Spec:** claude.ai/code/artifact/9e8ebbaa-ee2d-483d-a8b6-7444ad831cc6 (report §1–§4)
+ memory `project_five_repo_optimization_review_20260824.md`.

## Global constraints

- 2-core / 8GB no-swap VPS: heavy compute only as transient systemd units,
  `nice -n 19`, never during 13:00–20:15 UTC market lane; nightly window opens
  ~21:30 UTC (fleet is at 149 done / ~1 outstanding, effectively idle).
- Live lane: 15:00 ET compute / 15:55 execute / 16:15 collect. No johnbot
  restarts required by any task below (env fallbacks read .env directly).
- Tests hit the REAL DB (.env loads at import): stub gates in fixtures, never use
  real-looking fixture tickers ("AAA" is a real ETF — use ZZT* fakes), run ONLY
  targeted test files, never the full suite.
- `OPENCLAW_FLATTEN_ON_ZERO_CONVICTION=1` and the min-acting gate are ARMED live:
  nothing may change the live S_adj scale without a shadow/dist log first.
- JS/py gate twins (lifecycle.py ↔ promotion_service.js ↔ regime_qualification.py)
  must stay value-synced — every threshold change lands in all of them.
- Subagents: edit ONLY the files your task lists; do not commit; do not touch
  `src/strategies/manifest.json` / `strategy_signatures*.json` (dirty from the
  weekend research run — not ours).
- Append-only doctrine for master data; atomic writes (tmp + os.replace).

## Already done (2026-08-24 morning, commit `11011b4`)

- [x] V1: verified `OPENCLAW_EOD_RECONCILE=1` in live .env — per-ticker cap fires
      in the production lane; no change needed.
- [x] R5: `paperhunter.md` gate text now describes the live per-regime sleeve rule.
      DB `regime_eligibility_thresholds` (min_sharpe 0.5) left untouched — it feeds
      the Sunday live-P&L review lane and matches the operator's activation slider.
- [x] HRP shadow re-armed: `OPENCLAW_PYPORTFOLIOOPT_SHADOW=1` in .env + dotenv
      fallback in `run_pyportfolioopt_shadow.py` (child env is frozen at johnbot
      start). First data row expected at today's 15:55 ET cycle.
- [x] statsmodels 0.14.6 installed (`--break-system-packages`, matching the box
      convention) and pinned in requirements.txt.

---

## Batch 1 — parallel (disjoint files)

### Task D1+D2: Pairs scanner + ledger backfill (X1 foundation)

**Files:** Create `src/pipeline/pairs_scanner.py`, `scripts/backfill_pair_ledger.py`,
`tests/pipeline/test_pairs_scanner.py`.

**Produces:** `data/derived/pair_ledger.parquet`, append-keyed by `as_of` (Monday
dates). Columns: `as_of, ticker_a, ticker_b, industry, beta, alpha, half_life_days,
sigma_spread, eg_pvalue, fdr_q, fdr_pass, cost_ok, approved, spread_mean, n_obs`.
`approved = fdr_pass(this scan) AND fdr_pass(previous scan) AND half-life band AND
cost_ok` (first-ever scan: approved requires 2nd scan — persistence rule).

**Algorithm (from spec §4):** universe = Postgres `universe` where active, joined
to `industry` (fallback `sector`; drop null buckets); bucket cap 50 by ADV desc;
within bucket 90d log-return corr ≥ 0.6 prefilter; Engle-Granger
`statsmodels.coint` both directions (keep min p) on trailing 504d closes (≥90%
non-NaN both legs); Benjamini-Hochberg across ALL pairs tested this scan, q<0.10;
hedge: OLS log(A)~log(B) over the 504d window; AR(1) θ on spread → half-life
∈ [5,30] td, hard-reject θ≥0; cost gate:
`(z_entry−z_exit)·sigma_spread ≥ K·4·mean(leg_cost_bps)/1e4` with z_entry=2.0,
z_exit=0.5, K=2.0, leg costs from `data/derived/ticker_cost_bps.json` (fallback
10bps). CLI: `--as-of --window 504 --min-corr 0.6 --fdr-q 0.10 --cost-k 2.0 --out`.
Prices via pyarrow predicate-pushdown slices (asset_correlation.py pattern) —
never load the full panel. Atomic parquet append.

**Backfill driver:** `backfill_pair_ledger.py --start 2023-09-04 --end <today>`
runs the scanner per Monday sequentially, logging per-scan counters (pairs tested /
fdr passed / approved). Documented proxy: current universe membership for
historical scans (survivorship caveat noted in module docstring — coverage filter
naturally excludes recent listings per as-of).

**Tests (synthetic, no DB):** BH-FDR against a hand-computed 5-p-value case;
half-life recovers a known OU θ within 15%; cost-gate arithmetic exact-value case;
persistence rule (approved only on 2nd consecutive pass); bucket cap.

### Task P3+R3: Tail statistics + tearsheets

**Files:** Create `src/backtest/tail_stats.py`, `scripts/generate_tearsheet.py`,
`src/database/migrations/148_sleeve_tail_stats.sql`,
`tests/backtest/test_tail_stats.py`. Modify `src/backtest/unified_backtest.py`
(persist path only).

**tail_stats.py:** `sleeve_tail_stats(pnl_pct: sequence, alpha=0.05) ->
{'sortino': float|None, 'cvar_5': float|None, 'downside_dev': float|None}` —
plain numpy, None below 20 obs. Migration 148 adds nullable `sortino NUMERIC,
cvar_5 NUMERIC` to `strategy_backtest_regimes`. unified_backtest persist wiring is
try/except non-fatal (advisory only — NOT a gate).

**generate_tearsheet.py:** `--run-id` (or `--strategy latest`) reconstructs daily
returns from `strategy_backtest_trades` (realized pnl_pct grouped by exit date over
the run window — approximation documented) → `quantstats.reports.html` →
`output/tearsheets/<strategy>_<run_id>.html`. FIRST verify quantstats 0.0.81 works
against pandas 3.0.2 (`qs.reports.html` on a synthetic series); if its pandas
internals break, fall back to a minimal self-rendered HTML (stats table + equity
curve via matplotlib) and record that in the module docstring. Hook: called
best-effort from unified_backtest after persist (env `OPENCLAW_BT_TEARSHEET=1`,
default ON, subprocess, non-fatal).

### Task R1: Benchmark-relative promotion criterion

**Files:** Create `src/backtest/benchmark_baseline.py`,
`tests/backtest/test_benchmark_baseline.py`. Modify
`src/strategies/lifecycle.py` (PROMOTION_THRESHOLDS),
`src/backtest/regime_qualification.py`, `src/lib/promotion_service.js`.

**benchmark_baseline.py:** `regime_benchmark_sharpe(start, end) -> dict[regime,
float]` — SPY daily closes from prices.parquet (sliced read) × regime tags from
`data/master/historical_regimes.parquet`; annualized Sharpe of SPY close-to-close
returns on the days tagged each regime; None below 40 obs (gate then skips the
criterion for that sleeve — fail-open, logged).

**Gate change (all three twins, value-synced):** new threshold
`min_excess_sharpe_vs_benchmark = 0.0`; sleeve qualifies only if
`sleeve_sharpe > benchmark_sharpe[regime] + min_excess` (in addition to existing
rules). Ship ARMED, but the qualification result must log both verdicts
(`gate_v2` vs `gate_v2+bench`) the first Sunday so the operator sees the delta.
JS twin reads the python-computed benchmark values persisted on the run row
(add `benchmark_sharpe_by_regime` JSONB to `strategy_backtest_runs` in the same
migration 148) — the JS side never recomputes finance math.

**Tests:** synthetic prices+regimes parquet fixtures (tmp dir): flat SPY ⇒ Sharpe
≈ 0 and a positive-drift sleeve passes; SPY sleeve itself must FAIL its own
benchmark (excess = 0 is not > 0); missing regime coverage ⇒ None ⇒ criterion
skipped.

### Task P1: Ledoit-Wolf shrinkage (shadow-first)

**Files:** Create `src/execution/shrinkage.py`, `tests/execution/test_shrinkage.py`.
Modify `src/execution/asset_correlation.py`, `src/execution/orthogonalization.py`
(γ resolution only), `src/execution/strategy_similarity.py` (rebuild: estimate +
store γ̂ in the artifact).

**shrinkage.py:** `lw_corr(panel: pd.DataFrame) -> (pd.DataFrame, float)` —
Ledoit-Wolf constant-correlation target via
`pypfopt.risk_models.CovarianceShrinkage(...).ledoit_wolf(shrinkage_target=
"constant_correlation")` + `cov_to_corr`; `lw_gamma(panel) -> float` returns just
the intensity δ̂.

**Asset side:** in `price_return_corr`, when `OPENCLAW_ASSET_CORR_LW=1`
(default `shadow`): build the dense 63d returns panel it already slices, compute
LW corr; `shadow` mode logs `[asset_corr_lw] shadow: mean|Δρ|=…,
clusters_now=…, clusters_lw=…` (clusters via the existing filter at thr 0.70)
and returns the legacy estimator; `1` returns the LW estimator. MIN_OBS pair
handling unchanged (pairs below 20 obs still forced 0 after shrinkage).

**Tangency side:** `strategy_similarity.rebuild()` computes γ̂ = `lw_gamma` on the
dense sub-panel of strategies with ≥60 return obs (fallback: None) and stores
`{"lw_gamma": γ̂}` alongside the matrix artifact. `orthogonalization` γ resolution
becomes: env `OPENCLAW_TANGENCY_SHRINK` override → artifact `lw_gamma` when
`OPENCLAW_TANGENCY_LW=1` → `TANGENCY_SHRINK_DEFAULT=0.10`. DEFAULT: LW flag
unset ⇒ byte-identical current behavior; every sizer cycle logs
`[tangency_lw] would_use_gamma=… (current=0.10)` when the artifact carries γ̂.
Flip to `1` only after one live-cycle dist comparison (flatten is armed — global
constraint).

**Tests:** LW corr of a known 3-asset panel is PSD (min eigenvalue ≥ −1e-10) and
off-diagonals shrink toward the mean; γ resolution precedence (env > artifact >
default); shadow mode returns legacy values byte-identical.

---

## Batch 2 — after Batch 1 review (sequential where files shared)

### Task S1: Pre-backtest red-team gate

**Files:** Create `src/agent/curators/strategy_redteam.js`,
`scripts/redteam_regression_check.js`, `tests/fixtures/redteam/lookahead_fixture.py`,
`tests/fixtures/redteam/clean_fixture.py`. Modify
`src/agent/research/research-orchestrator.js` (insert stage validate → **redteam**
→ backtest).

Reuse the claude-bin invocation idiom from `mastermind_code_review.js`. Contract:
reviewer returns STRICT JSON `{verdict, findings:[{concern, severity:
"critical"|"warning", evidence}]}`, hand-validated in JS (no new deps); malformed
JSON ⇒ one retry ⇒ then WARN-and-pass (never silently block on infra failure —
log `redteam_infra_fail`). Any `critical` finding ⇒ `implementation_queue.status
= 'redteam_blocked'` + reason, gate-decision emitted (`emitGateDecision`
gateName:'redteam'); warnings annotate but pass. Checklist in the prompt:
future-bar/shift(-1) usage, off-by-one window alignment, full-sample parameter
fitting, survivorship assumptions, signal-never-fires. Regression check: reviewer
must flag the look-ahead fixture (a deliberate `close.shift(-1)` signal) and pass
the clean fixture; `redteam_regression_check.js` exits non-zero otherwise.

### Task D3+D4: S_coint_pairs_sector_v2 strategy + parity oracle

**Files:** Create `src/strategies/implementations/S_coint_pairs_sector_v2.py`,
`tests/strategies/test_coint_pairs_v2.py`. Modify `src/strategies/registry.py`
(_IMPL_MAP entry only).

Reads `data/derived/pair_ledger.parquet` (pyarrow, filtered `as_of <=
prices.index.max()`, latest as_of, `approved` rows only) — look-ahead-safe by
construction. Per approved pair with both legs in the passed universe/panel:
`spread_t = log(A)−beta·log(B)−alpha`, `z = (spread − mean60)/std60` computed
from the passed prices panel; ENTRY edge-trigger `|z_t| ≥ 2.0 AND |z_{t−1}| <
2.0` emits both legs (short rich / long cheap, dollar-neutral via beta);
z-backstop: no entry when `|z_t| ≥ 4.0`. Study `base.py` Signal fields first and
express exit via the engine idiom (declared cadence ≈ min(3·half_life, 30)d +
compute_stops_and_targets); document any idiom-forced deviation from the spec in
the module docstring. First registry-only (NO manifest entry — candidate minting
goes through the normal lifecycle after the first clean backtest).

**Parity oracle (R4):** independent from-first-principles reference implementation
of z/edge-trigger inside the test file (numpy-only, no shared helpers) diffed
against `generate_signals` output on synthetic two-pair fixtures (ZZT* tickers,
tmp ledger parquet): identical (ticker, direction, entry-day) sets required, incl.
a no-retrigger case (z stays > 2 for 5 bars ⇒ exactly one entry) and a
backstop case (z jumps 1.8→4.2 ⇒ no entry).

### Task P2: Strategy-level HRP shadow

**Files:** Modify `scripts/run_pyportfolioopt_shadow.py`,
`src/execution/pyportfolioopt_shadow_sizer.py`. Create
`tests/execution/test_hrp_strategy_level.py`.

Add `method='hrp_strategy'` row per run: per-strategy daily-return panel via the
loaders `strategy_similarity.return_correlation` already uses (252d, strategies
with weight in the current regime and ≥60 obs; flat days = 0.0, documented);
`HRPOpt(returns).optimize()`; diff vs the live S_adj-implied allocation
(normalized `|daily_weight|` per acting strategy). ALSO fix the existing
ticker-level row: scale HRP weights to the live book's realized gross before
diffing (the 2026-05-14 row's 100%-vs-2.5% comparison is the documented flaw).
Shadow only — never routes; decision after ≥20 rows.

---

## Batch 3 — after Batch 2

### Task R2: Pre-backtest factor screen

**Files:** Create `src/backtest/factor_prescreen.py`,
`tests/backtest/test_factor_prescreen.py`. Modify
`src/agent/research/research-orchestrator.js` (stage redteam → **prescreen** →
backtest).

Runs the candidate's `generate_signals` over the last 60 trading days on a
300-ticker slice of its resolved universe (120s timeout, subprocess): reports
`{signals_total, active_days, direction_balance, ic_mean (if scores), turnover}`.
HARD-block only degenerate outcomes (zero signals in 60d, or 100% constant
output) ⇒ `status='prescreen_failed'`; everything else annotates. Saves the 900s
backtest only in the degenerate case by design — conservative on purpose.

### Task S3: Promotion dissent

**Files:** Create `src/agent/curators/promotion_dissent.js`,
`src/database/migrations/149_promotion_dissents.sql`. Modify
`src/agent/curators/auto_approval.js` (after successful candidate→live
transition, non-blocking).

One claude-bin call per successful promotion (few/week): structured dissent
`[{concern, severity, evidence}]` over what Sharpe can't see (regime-window
undersampling, correlation with incumbents beyond similarity, mechanism
plausibility, cost-at-size; feed it the run metrics incl. new sortino/cvar_5 from
P3 and top-5 similarity neighbors). Writes `promotion_dissents(strategy_id,
promoted_at, dissent JSONB, model)`, posts a summary to #botjohn-log (existing
webhook helper — Cloudflare-UA gotcha). Non-veto by design; failure logs and
never blocks the promotion.

### Task S2: Strategycoder tournament (flag-gated)

**Files:** Modify `src/agent/research/research-orchestrator.js` (`_codeFromQueue`).
Create `tests/` coverage as JS-side dry-run script `scripts/tournament_dryrun.js`.

Behind `pipeline_config.tournament_variants` (default 1 = OFF; operator sets 3):
for Tier-A rows, N strategycoder calls with an explicit interpretation-variant
directive, files suffixed `_tv{a,b,c}`; each variant: validate → redteam →
prescreen → backtest (STRICTLY SERIAL); winner by primary-run Sharpe with
≥min-trades; winner renamed to canonical path + queue row updated; losers'
files deleted with a `tournament_loser` gate-decision log (fingerprint dedup runs
ONCE per paper before the fan-out; losers are NOT written to the ejected-
signatures archive). Stays OFF until one supervised Saturday-brain run.

---

## X1 compute schedule (quiet windows)

- **Mon 21:30 UTC:** transient unit `x1-ledger-backfill` — `backfill_pair_ledger.py`
  over ~156 Mondays (est. 1–4h, nice -19, MemoryMax guard). Verify counters Tue am.
- **Tue 21:30 UTC:** transient unit `x1-backtest` — `unified_backtest
  --strategy-file S_coint_pairs_sector_v2.py --universe-cap tier_liquid`; tearsheet
  auto-generated (R3); review Wed am, iterate params if needed.
- **Wed–Fri nights:** re-run after any iteration; eligibility_assigner after a
  clean run; candidate minting via normal lifecycle; Sunday sweep judges it with
  the R1-enhanced gate.
- Chain units so ledger job and backtest never co-run; nothing scheduled inside
  the 13:00–20:15 UTC market lane.

## Verification & rollout checklist

- [ ] Batch 1 tasks green on targeted tests, committed one commit per task.
- [ ] Today 22:00 UTC: confirm `pyportfolioopt_shadow_runs` gained today's row.
- [ ] After 1 live cycle with P1 shadow logs: review Δρ/cluster/γ̂ lines, then arm
      `OPENCLAW_ASSET_CORR_LW=1` + `OPENCLAW_TANGENCY_LW=1`.
- [ ] Wed: first X1 backtest results + tearsheet reviewed.
- [ ] First Sunday: R1 dual-verdict log reviewed; S3 dissents visible in
      #botjohn-log; S2 remains OFF until supervised.
- [ ] Push to origin after each batch.

## Post-landing runbook (copied from the SDD ledger 2026-08-24 20:40 UTC)

## TUESDAY 08-25 RUNBOOK (pre-written for post-compaction recovery)
1. `journalctl -u x1-ledger-backfill.service --no-pager | grep -E "as_of|approved|errors_dropped" | tail -20` — expect ~156 per-week lines; sanity: fdr_pass/approved counts non-zero in later weeks; errors_dropped small.
2. `python3 -c "import pandas as pd;d=pd.read_parquet('data/derived/pair_ledger.parquet');print(d.as_of.nunique(),'scans',d.approved.sum(),'approved rows',d[d.approved].as_of.max())"`
3. If ledger sane, schedule first X1 backtest (single-strategy path → tearsheet auto-generated):
   `systemd-run --on-calendar="2026-08-25 21:30:00 UTC" --unit=x1-backtest-1 --property=Nice=19 --property=MemoryMax=3000M --property=RuntimeMaxSec=14400 --property=WorkingDirectory=/root/openclaw --property=EnvironmentFile=/root/openclaw/.env --setenv=PYTHONPATH=/root/openclaw/src --setenv=OPENCLAW_BT_TEARSHEET=1 /usr/bin/python3 -m backtest.unified_backtest --strategy-file src/strategies/implementations/S_coint_pairs_sector_v2.py --universe-cap tier_liquid`
   (verify the exact CLI flags with `python3 -m backtest.unified_backtest --help` first; PYTHONPATH=src per research-orchestrator's invocation.)
4. Mon-evening checks owed: `SELECT run_date, method, notes FROM pyportfolioopt_shadow_runs ORDER BY run_date DESC LIMIT 4` (expect 2026-08-24 rows for 'hrp' AND 'hrp_strategy'); grep the 15:55 sizer log for `[asset_corr_lw] shadow:` — if ABSENT, the sizer subprocess did not receive OPENCLAW_ASSET_CORR_LW from johnbot's frozen env → add the same dotenv fallback the HRP runner got (asset_correlation.py mode resolver) or schedule a johnbot restart.
5. After ≥1 shadow cycle with the `[asset_corr_lw]` line: review mean_abs_delta_rho / cluster deltas → flip .env OPENCLAW_ASSET_CORR_LW=1. Tangency LW arming waits for an lw_gamma artifact (returns density) — not this week.

- R2 preflight 08-24 20:37 UTC: WALL 4.65s / RSS 544MB on production defaults (budget PASS).
- x1-ledger-backfill timer moved to 22:05 UTC (fleet retries S_mingle_factor_graph_portfolio at 21:30 — nightly OOM, pre-existing).
- OPERATOR DECISIONS OWED: (1) apply R1's benchmark leg in activation_assigner / eligibility_assigner / lifecycle.py:593 (today: candidate→live only); (2) arm S2 with pipeline_config.tournament_variants=2 for one supervised Saturday; (3) flip OPENCLAW_ASSET_CORR_LW=1 after reviewing the shadow log; (4) redteam_blocked / prescreen_failed statuses need sweep + resurrect-script + dashboard-chip coverage (follow-up).
