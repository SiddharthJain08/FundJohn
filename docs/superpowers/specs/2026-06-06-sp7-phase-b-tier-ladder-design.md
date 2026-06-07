# SP-7 Phase B: Tier-Ladder Universe-Determination Backtest — Design

**Spec date:** 2026-06-06
**Author:** BotJohn brainstorm session (operator-validated: 4 anchor decisions + 6 section approvals)
**Status:** Operator-approved 2026-06-06; proceeding to implementation plan
**Parent spec:** `docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md` (§4 Phase B)
**Predecessors:** SP-7 Phase A (merged 20c31ea, §11 activation complete 2026-06-06, clamp fix ab4238f), SP-2 Phase C (grid machinery, PR #10)

---

## 1. Context

SP-7 Phase A is complete: prices.parquet holds 5,071 tickers (5y backfill, 0 errors),
metadata v2 history spans 2021-07→2026-06, market_cap is EDGAR-derived
(shares_outstanding.parquet, 3,483 tickers), and the engine clamp
(`OPENCLAW_ENGINE_UNIVERSE_CLAMP=sp500`) holds live trading at kept≈591 while the
data widened underneath. Phase B delivers the once-per-strategy
universe-determination backtest: a nested tier ladder per strategy, a deterministic
selection rule, recommendation rows the existing adoption plumbing consumes, and
breadth-scaled conviction-threshold proposals.

This spec re-grounds §4 of the parent spec against live state (6-subsystem sweep,
2026-06-06) and **amends it** where grounding contradicted it. The largest finding:
the metadata history the ladder must backtest on is incoherent, so Phase B gains a
gated prerequisite (B0) before any ladder compute.

### Anchored facts (2026-06-06, all measured/queried this session)

- **Ladder machinery ~70% pre-built**: `MockResolver` (universe_resolver.py:101),
  the `resolver=` path through `unified_backtest._per_bar_simulate` (called once per
  bar, unified_backtest.py:521), and the 8-metric `blend_metrics`
  (universe_grid_cli.py:50) all exist. The t+1 fill model applies on the grid path.
  Grid runs write nothing to Postgres (universe_grid_cli.py:149).
- **"Regime-blended Sharpe" = `blend_metrics()['sharpe']`**: day-frequency-weighted
  mean of per-regime sharpes (weights from `regime_day_frequency`), <5-trade regimes
  nulled and renormalized over contributors. NOT `aggregate_metrics`' total sharpe.
- **Per-bar resolve cost (measured)**: `fetch_metadata_as_of` 0.17 s/bar (new
  psycopg2 conn per call); first resolve of a new calendar month ~4.96 s
  (`ParquetCoverage._load_month` re-reads prices.parquet). A 5y single-tier backtest
  pays ~8–9 min of pure resolution overhead — this is what caused the SP-2 Phase C
  5-min spawnSync timeouts (universe_recommender.js:217). Historical snapshots are
  monthly, so per-bar daily resolution is redundant for the backfilled period.
- **Snapshot span**: ticker_metadata_snapshots = 2021-01-31 → 2026-06-05, 76 distinct
  dates. For `as_of` < 2021-01-31 `fetch_metadata_as_of` returns ZERO rows → empty
  universe → zero signals (verified). `DEFAULT_START_DATE='2016-04-11'`
  (unified_backtest.py:70) is therefore a trap for resolver-backed runs.
- **BLOCKER (v1/v2 ghost rows)**: the metadata backfill used ON CONFLICT DO NOTHING,
  so the ~403 original (v1) symbols kept v1-only rows — `in_sp500=true,
  in_r1000=false, in_r3000=false, market_cap=NULL`. Verified at 2023-06-30 via the
  resolver's exact DISTINCT-ON query: AAPL/MSFT/NVDA/AMZN/GOOGL/JPM/XOM all resolve
  from `backfill_5y_v1` with `in_r1000=false`. **Historical r1000/r3000 tiers exclude
  every mega-cap**; historical in_sp500 ≈ 350 vs true ~503. Phase A acceptance
  checked *non-empty*, never *nesting* — a latent Phase-A acceptance gap.
- **BLOCKER (degenerate dailies)**: live_daily snapshots 2026-05-25→2026-06-03 (+
  partial 06-04) have `in_r3000=0, market_cap=0` (writer ran before EDGAR shares
  landed). Any `as_of` in that band resolves rank-tiers to empty; these rows also
  poison the live resolver (Phase C) permanently if left.
- **BLOCKER (options_eligible)**: false on ALL 438,772 snapshot rows (0 true; the
  writer's `options_cache` is empty in production). The parent spec's ×options
  ladder variant resolves to the empty set everywhere.
- **Repair inputs exist**: `data/sp500_historical_membership_v1.csv` (880 lines,
  ticker/added_on/removed_on, already read by universe_metadata.py:64);
  shares_outstanding.parquet covers 3,483 tickers (AAPL/MSFT/NVDA/JPM back to
  2008); `build_month_snapshot` + `market_cap_lookup` + the documented supersede
  path (`OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1` + `--source-tag`) all live.
- **Tier sizes (2026-06-05 snapshot)**: in_sp500=503, in_r1000=1000, in_r3000=3000,
  liquid_tradable (tradable∧active∧easy_to_borrow)=5,113; easy_to_borrow fully
  populated. Earliest v2 (2021-01-31): r1000=1000, r3000=2293, liquid≈3264.
- **Population**: strategy_registry status='approved' = **67** as of 2026-06-06
  (operator-directed reconcile this session: +S_btc_momentum,
  +S_commodity_etp_momentum, +S15_insider_opportunistic_short, S_intl_momentum_
  attention_regime deprecated→approved [no recorded reason existed],
  S_price_path_convexity pending→approved; via scripts/reconcile_strategy_registry.py).
  Zero strategies carry a non-default `universe_filter_ref` today.
- **Adoption plumbing**: strategy_universe_recommendations (no status enum —
  approved bool NULLABLE + adopted bool), `lifecycle_universe_adoption.
  adopt_universe_recommendation` (two-phase manifest+DB+audit; ValueErrors on
  candidates not in CANDIDATE_PREDICATES), Discord ✅/❌/⏸ reactions keyed on the
  `universe-rec:<id>` message footer, dashboard POST /api/universe-recs/:id/:action.
  **The live :7870 process predates these routes — both paths 404 until
  fundjohn-dashboard.service restarts.** 58 stale pending rows from 2026-05-25
  remain (age out of the 14-day window 2026-06-08).
- **Threshold machinery**: regime_sizer_params = 4 global rows (LOW_VOL=3,
  TRANSITIONING=4, HIGH_VOL=5, CRISIS=6), CHECK [1.0,10.0] at DB level; resolver
  `_resolve_min_cumulative_sharpe` at regime_blended_sizer.py:171 (params →
  pipeline_config fallback → 3.0); edited via direct PUT
  /api/config/regime-sizing/:regime on the **:3000 johnbot API server**
  (server.js:722, Conviction Gates sliders) — NOT the :7870 control room. Existing
  proposal pattern to mimic (shape only): strategy_regime_param_proposals
  (mig 078) — its apply path writes a different table and strategy_id is NOT NULL,
  so it cannot be reused directly.
- **Compute substrate**: 2-core / 8 GB / 0-swap box. Full panel load ≈1.7 GB RSS
  (load_prices_panels reads the whole parquet, no column scoping). Weekend-refresh
  full-roster spans: ~5h12m (2026-06-01) to ~20h45m (2026-05-31, incl. bocpd ~3.5h).
  The chunked OOM/watchdog loop in operator memory is NOT in committed code —
  refresh_backtests.sh is monolithic `--all-live`; the closest committed model is
  scripts/batch_backfill_backtests.py (subprocess-per-strategy + timeout + resume).
- **Scheduling substrate**: sp7-overnight-backfill user timer (Mon-Fri 01:00 UTC)
  is a confirmed no-op (sentinel `data/.sp7_backfill_armed` removed);
  overnight_backfill.sh is the proven night-window template (budget-to-13:00-UTC,
  TERM, --resume, sentinel self-disarm). Mon-Fri 20:00–21:40 UTC is a dense timer
  band (B1 shadow 20:45, watchdog 21:00, split-watcher 21:15, minute-bar accrual
  21:40) — ladder work must stay inside 01:00–13:00 UTC. Saturday excluded
  (weekend stack owns the box from 12:00 UTC).
- **Legacy universe-recs**: weekend_saturday.sh step 8 invokes
  `run_mastermind.js --mode universe-recs`, self-gated on OPENCLAW_UNIVERSE_RECS.
  The standalone openclaw-universe-recs.timer is disabled. The gate was commented
  out of .env on 2026-06-06 (operator-approved) — step 8 logs a skip until Phase B
  re-points it (§7).

## 2. Operator decisions (locked 2026-06-06)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Legacy universe-recs at Sat 12:00 UTC | **Skip step 8** (gate unset same session; legacy grid can't use the new caps, would mass-timeout, writes junk rows Phase B supersedes) |
| 2 | Metadata incoherence | **B0 repair approved** as Phase B's first gated deliverable (month-rebuild + supersede; NOT targeted-SQL-only; NOT defer) |
| 3 | Ladder population | **All 67 registry-approved** (registry reconciled to include all active trading strategies this session; ladder population = status='approved', no union special-case) |
| 4 | Runner architecture | **Approach C**: one-time tier-membership precompute + per-cell subprocesses through a PrecomputedResolver |

Carried from the parent spec: once-per-strategy cadence; recompute only via
12th-Saturday sentinel / dashboard button / mint-time (Phase D); operator remains
the adoption gate; √ln(N) proposals are proposals, never direct writes.

## 3. B0 — Metadata coherence repair (gated prerequisite)

**Monthly history (2021-01→2026-05, ~64 months):** re-run `build_month_snapshot`
for the FULL symbol set (v1 ∪ v2 universe per month) with `market_cap_lookup`
(EDGAR shares × split-adjusted close), in_sp500 from the membership CSV, and the
listed_date PIT filter; write via the documented supersede path
(`--source-tag backfill_5y_v3`, `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1`), audit rows
in backfill_audit. This replaces the v1 ghost rows wholesale.

**Degenerate dailies (2026-05-25→2026-06-04, 9 dates):** targeted repair — compute
market_cap = shares×close per symbol-date, then rank-fill in_r1000/in_r3000.
Observed-that-day columns (status/tradable/easy_to_borrow/exchange) untouched.

**B0 acceptance (all SQL, recorded in the spec's runbook):**
1. Mega-cap spot-check: AAPL/MSFT/NVDA/JPM in_r1000=true for every month ≥2021-07
   via the resolver's exact DISTINCT-ON query.
2. in_sp500 per month ≥ 460 AND ≥ 95% of CSV-reconstructable members present
   (operator re-bound 2026-06-07: the Wikipedia-scraped membership CSV yields
   ~483 members for mid-history dates — ~20 short of true ~503 — and ~7 of
   those are unbuildable: 6 delisted [no Alpaca history; same accepted class
   as Phase A's 94] + 1 late-listed. Structural max ≈476 at 2023-06 vs ~360
   pre-repair. The repair also INSERTs missing historical rows for SP-2-era
   SP500 members — see plan Task 3 amendment. BACKLOG: source a fuller SP500
   membership history, regenerate the CSV, re-run B0 months (idempotent).
   Original target was ≈500±15.)
3. r1000 = 1000 and r3000 = min(3000, ranked-pool) per month, ranked-pool reported.
   NOTE: the v2 build produced r3000=2389 at 2021-06-30 despite a 3,631-name cap
   pool — mechanics unexplained; any post-rebuild month with r3000 < 2,800 is
   investigated before ladder GO rather than waved through.
4. Nesting diagnostic: |sp500 \ r1000| per month reported (data-quality metric, not
   a hard gate — structural nesting comes from §4's tier predicates).
5. Repaired dailies: r3000=3000 on each of the 9 dates.
6. Live-parity guard: the engine clamp reads the latest snapshot (2026-06-05+,
   untouched by B0) — kept≈591 verified unchanged before/after.

**Ladder compute is gated on B0 acceptance** (Phase-A pattern: backfill → activation
→ acceptance → GO).

## 4. B1 — Tier predicates, membership precompute, ladder runner

### Predicates (universe_default.py + CANDIDATE_PREDICATES, lint-compliant)
- `liquid_tradable(meta, as_of)` = tradable ∧ status=='active' ∧ easy_to_borrow
  (parent-spec name; 5,113 on the 2026-06-05 snapshot).
- Nested tiers **by construction**:
  `tier_r1000` = in_sp500 ∨ in_r1000; `tier_r3000` = tier_r1000 ∨ in_r3000;
  `tier_liquid` = tier_r3000 ∨ liquid_tradable.
- Ladder chain: `sp500 ⊂ tier_r1000 ⊂ tier_r3000 ⊂ tier_liquid`. All four names in
  CANDIDATE_PREDICATES so adoption works unchanged. Existing r1000/r3000 predicate
  semantics are NOT modified (legacy rows reference them).

### Membership precompute (scripts/build_tier_membership.py — minutes per run)
- Resolve each tier per month over the window, including the 60-bar coverage floor
  (single hoisted coverage index — computed once, not per-month-per-strategy).
- Output: frozen `data/universe_tier_membership_<run_id>.parquet`
  (tier, month, symbols list) + per-tier N series (B3 input) + nesting diagnostics
  (B0 acceptance artifact). PIT by construction; a ladder run sees one consistent
  snapshot of history.
- **Window: 2021-07-01 → last trading day at run start.** Identical for all cells
  of a run; recorded on the run row; never DEFAULT_START_DATE. Early months have
  thinner floor-passing membership (5y price backfill ramps in) — uniform across
  tiers, recorded in the artifact.

### Runner
- `universe_grid_cli` gains `--membership-artifact <path> --tier <name>` backed by a
  new `PrecomputedResolver` (UniverseResolver subclass: dict lookup per bar by
  snapshot-month; keeps the AsOfInFutureError guard; no DB, no parquet scans).
  The legacy `--resolver-override` path is untouched (regression-tested).
- New table `universe_ladder_runs` (migration number assigned at build time, SP-2
  precedent): run header (run_id, window, artifact path, trigger) + one row per
  cell (strategy_id, tier, status ∈ queued/running/done/timeout/error/
  skipped_degenerate, metrics jsonb [8-key blend + trades_n + trade-list SHA-256 +
  resolved N_tier], duration, stderr tail).
- Queue driver `scripts/run_universe_ladder.py`:
  - Strategy-major order; **extremes-first** within a strategy: run sp500 and
    tier_liquid cells first; identical trade-list SHA ⇒ verdict
    `universe-independent`, middle tiers skipped (heuristic, recorded as such).
  - Per-cell budget: **2h default, 6h slow-list** (bocpd,
    pairs_trading_jump_diffusion_intraday, S_options_flow_confirmed_momentum —
    list maintained in the driver); timeout ⇒ status=timeout ⇒ ineligible in §5.
  - Strictly sequential, `nice -n 19`, one subprocess per cell (RSS freed between
    cells — the weekend-OOM discipline; the box is 2-core/8GB/0-swap).
  - Resumable: done cells skipped on re-entry; a TERM'd running cell resets to
    queued (idempotent).
- Nightly window: `scripts/overnight_ladder.sh` + user timer **Mon-Fri 01:00 UTC**
  (mirrors overnight_backfill.sh verbatim: sentinel `data/.sp7_ladder_armed`,
  budget to 13:00 UTC, `timeout --signal=TERM`, self-disarm when the queue is
  empty). Saturday excluded. EnvironmentFile=.env (POSTGRES_URI).
- Compute expectation (honest): with resolution overhead eliminated by the
  precompute, cell cost = panel load (~1 min) + strategy compute (scales with
  universe size for cross-sectional strategies; degenerate detection removes
  fixed-ticker waste). Estimate **3–10 nights**; the queue is resumable so the
  estimate is not load-bearing.

### Options-variant: explicitly deferred (scope cut)
options_eligible is false on all 438,772 snapshot rows — the ×options ladder branch
resolves empty today. Options-dependent strategies run the plain ladder; their rec
rationale notes the limitation. Follow-up backlog: populate options_eligible in the
daily writer via an Alpaca chain probe; run the variant when data exists.

## 5. B2 — Deterministic selection + recommendations + adoption reuse

**Selection (no LLM), in the queue driver when a strategy's cells complete:**
- Tier eligibility: blended sharpe non-None AND trades_n ≥ 30 (mirrors the
  weekend-coupling floor). timeout/error/empty cells ineligible.
- Winner = narrowest eligible tier; walking broader, a tier displaces the current
  winner iff ΔSharpe ≥ 0.10 (parsimony tie-break). None/ineligible always loses.
- Edge verdicts: all-ineligible → `no_signal` (rec keeps current predicate,
  tagged); extremes-identical → `universe-independent` (keeps current, tagged).
  Every one of the 67 strategies gets a recorded verdict.

**Output — exact reuse contract:** one row per strategy into
strategy_universe_recommendations: candidate_predicate = winning name (must be in
CANDIDATE_PREDICATES), candidate_set_id = `sp7b-1-<run_id>` (NOT NULL),
backtest_summary.grid = 4 tier entries in the legacy array shape
({name, sharpe, sortino, calmar, max_dd_pct, win_rate, trades_n,
mean_holding_days, mean_universe_size}) + ladder extras (window, N_tier, cell
statuses, trade SHAs), approved=NULL, deterministic rationale text.

**Discord:** per change-rec post to #universe-recs with the REQUIRED
`universe-rec:<id>` footer (reaction parser contract); no-change verdicts batched
into one summary line (nothing to adopt). **Stale rows:** the 58 pending 2026-05-25
legacy rows get a superseded rationale tag (UPDATE append; no deletion).

**Runbook owed by this section:** restart fundjohn-dashboard.service and curl-verify
GET/POST /api/universe-recs before the first rec posts (live process predates the
routes; both adoption paths 404 today).

## 6. B3 — Breadth-scaled conviction-threshold proposals (union-N rule)

regime_sizer_params.min_cumulative_sharpe is GLOBAL per-regime; tier adoption is
per-strategy. Reconciliation — **union-N**: on every adoption event, recompute
`N_union = |∪ adopted-universe memberships across all 67 strategies|` on the latest
snapshot (un-adopted strategies contribute sp500);
`factor = √(ln N_union / ln N_sp500)`; `proposed = current_base × factor` per
regime, clamped [1.0, 10.0] (DB CHECK enforces too). A new adoption supersedes
prior pending proposals.

Storage: new table `universe_threshold_proposals` mimicking
strategy_regime_param_proposals' SHAPE (proposed_at, proposer, regime_state,
current_row jsonb, proposed_min_cumulative_sharpe, basis jsonb {N_union, N_sp500,
factor, adoption_event}, status pending/approved/rejected/superseded, decided_at,
decided_by, applied_row jsonb) — the existing table/apply-path cannot be reused
(strategy_id NOT NULL; applies to strategy_regime_params).

Surfacing: **:3000 johnbot API server** (where the Conviction Gates sliders and
PUT /api/config/regime-sizing/:regime live) — GET list + Apply button that routes
through the existing PUT validation and stamps applied_row. **Never auto-applied.**

## 7. B4 — Recompute triggers (and only these)

1. **12th-Saturday sentinel**: Redis `sp7:ladder:last_full_run` (ISO date, no TTL,
   ioredis set idiom). weekend_saturday.sh step 8 is RE-POINTED from the legacy
   universe-recs invocation to the sentinel check: ≥12 weeks since last full run →
   arm `data/.sp7_ladder_armed` + seed the full queue (compute happens in the
   following nightly windows, not on Saturday). This retires the legacy step-8
   call slightly ahead of the parent spec's Phase-D schedule (gate already off,
   operator-approved 2026-06-06).
2. **Dashboard button**: per-strategy Recompute on :7870 → POST enqueues that
   strategy's cells into universe_ladder_runs + arms the sentinel; the nightly
   window picks it up.
3. **Mint-time** stays Phase D. Noted risk for that plan: a 4-tier ladder inside
   saturday-brain's 6h TimeoutStartSec needs a budget/defer decision.

## 8. Testing & error handling

- TDD throughout; subagent-driven build.
- Unit: predicate nesting property test (∀ meta: sp500 ⊆ tier_r1000 ⊆ tier_r3000 ⊆
  tier_liquid); PrecomputedResolver (PIT lookup, future-as_of guard,
  month-boundary semantics); selection rule (None handling, displacement,
  all-ineligible, degenerate verdicts); B3 math (factor, clamp, union recompute,
  supersede).
- Driver: resume/timeout/degenerate-skip against a fake CLI binary; TERM-resets.
- B0: golden-month builder test; acceptance SQL codified as a system_checks probe
  (`universe_tier_coherence`, strategies tag) so the ghost-row bug class cannot
  silently recur.
- Regression: legacy --resolver-override path byte-identical; predicate lint CI
  green for the 4 new predicates; legacy adoption flow adopts a tier rec in a
  test transaction.
- Error handling: cell error → status + stderr tail on the row; 3 consecutive
  errors on one strategy → strategy marked failed, queue moves on; window TERM
  resets the running cell to queued; Discord post failures non-fatal (retry next
  night); proposal writes transactional.

## 9. Risks

| Risk | Mitigation |
|---|---|
| B0 rebuild corrupts good rows | Supersede path only (documented v2-recovery), backfill_audit rows, acceptance before ladder GO, live-parity guard on the clamp |
| Rank tiers wrong where EDGAR shares are missing (~multi-class, e.g. BRK.B) | Known Phase-A gotcha (entity-level caps accepted); nesting diagnostic reports residuals; tier predicates union-force nesting |
| Ladder wall-clock blows estimate | Resumable queue + per-cell budgets; estimate is not load-bearing; nightly windows until done |
| Box OOM | Subprocess-per-cell (RSS freed), sequential, nice -19, 01:00–13:00 window, Saturday excluded |
| Selection on thin early-window membership | Window + N series recorded per run; trades_n ≥ 30 eligibility floor; parsimony default |
| Universe-overfit | Nested tiers only, once-per-strategy cadence, parsimony tie-break (parent-spec decisions) |
| Degenerate-detection false positive (extremes identical by chance) | SHA over full trade list (ticker+date+side); recorded as heuristic verdict, operator-visible, recomputable via dashboard button |
| Junk/stale rec mixing | 58 legacy rows superseded-tagged; candidate_set_id prefix sp7b- distinguishes ladder rows |
| Threshold proposal mis-scaling | Proposals never auto-applied; [1.0,10.0] DB CHECK; basis jsonb records the math; operator applies/edits |

## 10. Acceptance criteria (Phase B)

1. B0 acceptance (§3) passes; system_checks probe green.
2. Every registry-approved strategy at queue-seed time (67 today) has a ladder
   verdict row (winner / no_signal / universe-independent) + an adoptable rec
   where applicable.
3. √ln(N) proposals visible on the :3000 dashboard next to Conviction Gates;
   Apply works through the existing validation.
4. 12th-Saturday sentinel + dashboard Recompute button functional (sentinel arms,
   queue seeds, nightly window drains).
5. Legacy --resolver-override regression green; adoption flow adopts a tier rec
   end-to-end (after fundjohn-dashboard restart, curl-verified).

## 11. Out of scope

- Options-variant ladder (deferred until options_eligible populates; backlog item:
  chain-probe in the daily metadata writer)
- Mint-time ladder (Phase D)
- Live per-strategy resolver wiring + clamp retirement (Phase C)
- Signal-level corroboration overlay (parent spec)
- Partitioned-parquet storage rework (parent spec, revisit criterion unchanged)
