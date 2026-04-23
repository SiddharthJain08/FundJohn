# Plan-Mode Prompt — Onboard Top-10 Cohort as Research Candidates

> Paste everything below the `---` marker into the plan-mode entry of your
> CLI. It is addressed to **MasterMindJohn** (research manager) with a
> delegated planning agent. The plan-mode agent must return a phased,
> dependency-aware execution plan — **not** execute yet.

---

## 0. Role & Mission

You are the **planning agent** working under MasterMindJohn, the research
manager for the FundJohn / OpenClaw hedge-fund bot network. Your single
deliverable from this plan-mode invocation is a **phased, dependency-
aware execution plan** that, once approved and run by downstream coding
agents, onboards the ten strategies in `top10_strategies_2026/` into the
live FundJohn research pipeline as *research candidates* (staging
status), such that they clear the existing staging gates and enter the
next daily cycle's queue.

Do **not** implement anything in this pass. Produce a plan. Treat each
phase as a work package that a subordinate coding agent will later pick
up. Flag anywhere you need human confirmation before that coding agent
starts.

## 1. Authoritative Context

- **Repo root (local dev):** `/sessions/nifty-gracious-mendel/mnt/FundJohn Initialization/`
- **Repo root (VPS):** `/root/openclaw/`
- **GitHub:** `github.com/SiddharthJain08/FundJohn` (main branch canonical)
- **Runtime workspace (VPS):** `/root/openclaw/workspaces/default`
- **Master data dir (VPS):** `/root/openclaw/data/master/`
- **Canonical docs:** `README.md`, `PIPELINE.md`, `ARCHITECTURE.md`,
  `LEARNINGS.md` — read these before writing the plan.
- **Cohort folder (the thing being onboarded):**
  `top10_strategies_2026/` — read `TOP10_README.md`, `deploy/top10_manifest.json`,
  and every file under `implementations/` before writing the plan.
- **Budget ceiling:** $400/month total, enforced via `config/budget.json`
  (dollar-based, not token-based). The cohort adds $0 net recurring cost;
  any plan step that implies a new paid data source must be flagged and
  justified.

## 2. The Ten Strategies

Your plan must treat these as a single atomic cohort unless a staging
gate forces fallback to a subset:

| ID | Class | Archetype | Horizon | Primary data deps |
|---|---|---|---|---|
| S_HV13_call_put_iv_spread | CallPutIVSpread | equity X-section | 7d | options_eod + iv_history |
| S_HV14_otm_skew_factor | OTMSkewFactor | equity X-section | 10d | options_eod + iv_history |
| S_HV15_iv_term_structure | IVTermStructureSlope | options-vol | 7d | options_eod + vol_indices |
| S_HV17_earnings_straddle_fade | EarningsStraddleFade | event-driven | 1d | options_eod + earnings_calendar |
| S_HV20_iv_dispersion_reversion | IVDispersionReversion | options-vol basket | 10d | options_eod (SPX + components) |
| S_TR01_vvix_early_warning | VVIXEarlyWarning | regime classifier | 10d lead | vol_indices |
| S_TR02_hurst_regime_flip | HurstRegimeFlip | regime classifier | 20d lead | prices (SPY ≥120d) |
| S_TR03_bocpd | BOCPDDetector | regime classifier | 10d lead | prices (SPY ≥200d) |
| S_TR04_zarattini_intraday_spy | ZarattiniIntradaySPY | intraday single | same-day | prices_30m + vol_indices |
| S_TR06_baltussen_eod_reversal | BaltussenEODReversal | intraday X-section | 30 min | prices_30m + prices |

## 3. Target End-State (Definition of Done)

A plan is acceptable only if, when executed end-to-end, it produces the
following observable results on the VPS:

1. **Manifest registration.** Each of the 10 strategies appears as a
   row in FundJohn's research-candidate staging registry with `status =
   "staging"`, `phase = "shadow"`, and full metadata parity with the
   cohort manifest at `top10_strategies_2026/deploy/top10_manifest.json`
   (academic_ref, data_dependencies, holding horizon, archetype,
   regime_filter, promotion_criteria).
2. **Data dependencies satisfied.** The four new parquet files
   (`vol_indices.parquet`, `prices_30m.parquet`, `earnings_calendar.parquet`,
   `iv_history.parquet`) exist under `/root/openclaw/data/master/` with
   current-session data; daily cron entries exist to refresh them.
3. **Engine integration.** `engine_patches/aux_metrics.py` is installed
   and wired into `_load_options_aux()` / `_load_market_data()`; one
   synthetic engine tick produces non-empty `opts_map` and `market_data`
   containing every field each strategy consumes.
4. **Staging gates cleared.** Each of the 10 strategies passes the
   existing staging gate battery (at minimum: import test,
   `generate_signals({}, {}) -> []` smoke, manifest schema validation,
   data-dependency reachability, regime_filter vs active regime check).
   Failing strategies must be surfaced with their specific gate failure
   and a remediation step, not silently dropped.
5. **Daily-cycle queuing.** Each passing strategy is queued into the
   next daily cycle's candidate list (the same queue MasterMindJohn
   already drains nightly). Discord posts a structured onboarding
   receipt with the 10 IDs and their gate statuses.
6. **Rollback plan.** A single command (script path + args) that
   removes all 10 from the registry, un-wires the engine patch, and
   leaves the four parquet files in place (data is harmless to keep).

## 4. Required Planning Phases

Structure the plan as **exactly seven phases** in this order. Each phase
must declare: (a) its concrete deliverables, (b) its entry criteria
(what must be true before it starts), (c) its exit criteria (what a
downstream agent will verify), (d) its blocking dependencies on earlier
phases, and (e) the human checkpoint required (if any).

### Phase 1 — Discovery & Preflight
- Read all four canonical docs and the cohort folder. Enumerate the
  **actual** staging-gate definitions MasterMindJohn uses today (from
  code, not from memory or assumption) and record each gate's pass
  condition.
- Verify the four required parquet files either exist or have a working
  ingest script; produce a table of {file, exists?, last_updated,
  ingest_script}.
- Verify engine.py's current `_load_options_aux()` / `_load_market_data()`
  signatures — confirm the aux_metrics.py integration snippet still
  applies unchanged; if engine surface has drifted, produce a reconciled
  patch.
- **Exit criterion:** a written preflight report flagging every gap
  between assumed state and actual state.

### Phase 2 — Data-Layer Readiness
- Sequence the four ingest scripts for a one-time historical backfill
  (`ingest_vol_indices --rebuild --from 2020-01-01`, `ingest_prices_30m
  --rebuild --from 2023-01-01`, `ingest_earnings_calendar --window 14`,
  `ingest_iv_history --rebuild`).
- Wire cron entries per `TOP10_README.md` section "Daily cron".
- Define a data-health check (row counts, null rates, last-date
  freshness) per file, to be invoked before every daily cycle. Fail
  loud in Discord if any check trips.
- **Exit criterion:** all four files present, fresh, and passing the
  health check; cron dry-run succeeds.

### Phase 3 — Engine & Aux Integration
- Install `engine_patches/aux_metrics.py` under
  `/root/openclaw/src/engine_patches/` and wire `build_opts_map` +
  `build_market_data` into engine.py per the `ENGINE_PATCH_SNIPPET` at
  the bottom of that module.
- Run a single engine tick on today's data; dump the resulting `opts_map`
  and `market_data` to a JSON artifact; assert every field enumerated
  in `TOP10_README.md` § "Data dependencies" is populated for a sample
  ticker (SPY + at least 10 single-name equities).
- **Exit criterion:** sample tick JSON committed as a gate artifact; any
  missing field triggers a Phase 1.5 data-fix cycle.

### Phase 4 — Strategy Code Installation
- Copy the ten strategy files from `implementations/` into
  `/root/openclaw/src/strategies/implementations/` exactly as-is (they
  carry a 3-tier import fallback; the production tier `from
  ..base_strategy` should activate automatically).
- Run the smoke test baked into `deploy/deploy_top10.sh` (phase 7 of
  that script: import each class, instantiate, call
  `generate_signals({}, {})`, assert list return). Capture the output
  and attach to the plan's run receipt.
- **Exit criterion:** 10/10 smoke pass, zero import errors, no runtime
  warnings beyond harmless DeprecationWarnings.

### Phase 5 — Manifest Registration in Staging
- Transform `deploy/top10_manifest.json` into however many staging-
  registry rows MasterMindJohn's system expects (this is discovered in
  Phase 1, not assumed here). One row per strategy. Set:
  - `status = "staging"`
  - `phase = "shadow"`
  - `source = "top10_cohort_2026_04"`
  - Full data_dependencies, academic_ref, promotion_criteria propagated.
- Run the staging-gate battery (discovered in Phase 1) against each
  row. Record pass/fail per gate per strategy.
- For any gate that fails, produce a *specific* remediation plan (not a
  hand-wave): name the fix, the file, and the one-line test that would
  flip the gate to green.
- **Exit criterion:** 10/10 strategies in staging with all gates green,
  or a remediation PR queued for each failing gate with a named owner
  (human or agent).

### Phase 6 — Daily-Cycle Queuing
- Confirm the mechanism by which MasterMindJohn drains the staging
  queue into the nightly cycle. Write each of the 10 staged strategies
  into that queue with the correct priority ordering (suggest
  ordering: liquid-universe first — S_TR04, S_TR06, S_HV13, S_HV14 —
  then vol-structure — S_HV15, S_HV20, S_HV17 — then regime classifiers
  — S_TR01, S_TR02, S_TR03).
- Schedule the first post-deploy daily cycle and capture its planned
  start time, expected signal count, and the Discord channel where its
  receipt will land.
- **Exit criterion:** queue state snapshot committed; cycle schedule
  confirmed.

### Phase 7 — Observability, Rollback, & Sign-Off
- Verify the existing Discord signal formatter renders a sample Signal
  from each of the 10 strategies without breakage (per-strategy
  formatting round-trip test).
- Define dashboards / logged metrics per strategy: signals-per-session,
  generate-time p95, gate-fail rate, `signal_params` field coverage,
  and downstream paper-book PnL once it starts accruing.
- Write the one-command rollback (file path + args) and dry-run it on a
  shadow copy of the registry.
- Produce the final onboarding receipt: one paragraph per strategy,
  listing ID, gate status, first-queued cycle, first expected signal
  ETA, and the promotion criteria from `top10_manifest.json`.
- **Exit criterion (and plan-mode sign-off):** receipt posted; human
  acknowledgement required before the first live queuing.

## 5. Cross-Cutting Requirements

These apply to every phase, not just one:

- **Idempotency.** Every automated step must be safely re-runnable.
  Ingest scripts already support `--rebuild` vs incremental; the deploy
  script must not duplicate registry rows on re-run.
- **Dry-run parity.** Every destructive step gets a `--dry-run` mode
  that logs what it would do without writing. The plan must call out
  which dry-runs are mandatory before the real run.
- **Budget gate.** Any step that would bump monthly spend above the
  $400 budget must be blocked by an explicit human confirmation
  checkpoint, not auto-approved.
- **Gate-failure handling.** The plan must never silently drop a
  failing strategy from the cohort. A strategy failing a gate in
  Phase 5 either: (a) gets remediated and re-queued in the same plan
  run, or (b) is deferred to a named follow-up ticket. In case (b),
  the other 9 still proceed to Phase 6.
- **Doc updates.** Whichever of README / PIPELINE / ARCHITECTURE /
  LEARNINGS are materially affected by the onboarding must be updated
  in the same plan run — never in a follow-up. LEARNINGS.md in
  particular should capture the "what surprised us" notes from
  Phases 1, 3, and 5.
- **Token economy.** This plan will be executed by downstream coding
  agents; each phase should fit within a single agent context window.
  Phases longer than ~12k tokens of work must be split. Explicitly
  declare which files each phase's agent needs to read and which it
  should avoid re-reading.

## 6. Validation & Acceptance Criteria (plan-mode output quality bar)

Your plan is rejected and must be revised if any of the following are
true:

1. Any phase lacks all five required fields (deliverables, entry,
   exit, dependencies, human checkpoint).
2. The staging-gate battery is described generically rather than
   enumerated concretely against real code (this requires Phase 1 to
   actually run discovery before writing Phase 5).
3. The plan assumes a field, file, or function exists without having
   declared a verification step for it in Phase 1.
4. The plan does not have a rollback for Phase 3 (engine patch),
   Phase 4 (strategy install), or Phase 5 (manifest registration).
5. The plan does not explicitly account for all 10 strategies at every
   staging-gate check (no aggregation without per-strategy evidence).
6. The plan proposes new paid data sources or external APIs not
   already in the $400/mo budget envelope.
7. The plan omits a Discord receipt at Phase 7.

## 7. Output Format

Produce the plan as a single markdown document with these top-level
sections:

```
# FundJohn Top-10 Cohort Onboarding Plan
## Summary (≤ 150 words)
## Preconditions & Assumptions (each flagged as verified in Phase 1 or not)
## Phase 1 — Discovery & Preflight
## Phase 2 — Data-Layer Readiness
## Phase 3 — Engine & Aux Integration
## Phase 4 — Strategy Code Installation
## Phase 5 — Manifest Registration in Staging
## Phase 6 — Daily-Cycle Queuing
## Phase 7 — Observability, Rollback, & Sign-Off
## Cross-Cutting Commitments
## Risk Register (top 5, with mitigation + owner)
## Rollback Procedure (one command path)
## Human Checkpoints (ordered list with what decision each asks)
## First-Cycle Expectations (per-strategy: signal count band, latency band)
```

Each phase section must include subsections: **Deliverables**,
**Entry Criteria**, **Exit Criteria**, **Dependencies**, **Human
Checkpoint (if any)**, **Estimated Agent-Hours**, **Files To Read**,
**Files To Write/Modify**.

## 8. What You Specifically Must Not Do in Plan Mode

- Do not edit files.
- Do not run scripts beyond read-only discovery (`ls`, `cat`, `git log`,
  parquet metadata reads).
- Do not make assumptions about staging-gate definitions — if Phase 1
  cannot find them in code, say so and escalate as a human checkpoint.
- Do not compress the plan to save tokens. Verbosity is welcome where
  it improves auditability.
- Do not skip or re-order the seven phases.
- Do not promise anything about live-money PnL; this cohort goes to
  `shadow` phase only and stays there until the promotion criteria in
  `top10_manifest.json` are met.

## 9. Final Self-Check Before You Return the Plan

Before handing off, confirm:

- [ ] Every phase names its entry + exit criteria.
- [ ] Every claim about the existing system is traceable to a file:line
      citation discovered in Phase 1.
- [ ] Every gate discovered in Phase 1 is exercised in Phase 5.
- [ ] Every new file written is also described under "Files To Modify"
      of the relevant phase.
- [ ] All 10 strategies appear by ID in Phase 5 and Phase 6.
- [ ] Rollback exists for Phases 3, 4, 5.
- [ ] Budget stays at or below $400/month across all proposed changes.
- [ ] Discord receipt is explicit at Phase 7.

Return the plan. I will review, flag revisions, and on sign-off hand it
to the execution agents. No execution begins until I approve.
