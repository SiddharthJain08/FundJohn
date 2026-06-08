# SP-7 Phase D — init prompt (paste into a fresh session to start the phase)

**Written 2026-06-08 at Phase C build completion.** Parent spec §6:
`docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md`.
Phase C design: `docs/superpowers/specs/2026-06-07-sp7-phase-c-live-wiring-design.md`.
Phase C runbook (flip sequencing): `docs/sp7-phase-c-runbook.md`.
House rule: re-ground every named fact below against live state at YOUR plan
time before trusting it (feedback_spec_plan_codebase_grounding).

---

## The prompt

You are starting **SP-7 Phase D — Research Uplift**, the FINAL phase of the
universe-expansion sub-project. Phase D makes the expanded universe a
first-class research input: the research stack can now ORIGINATE strategies on
broad tiers, new mints confirm their tier on the ladder before going live, the
legacy Opus grid retires, and the operator gets a Universe dashboard. Follow
superpowers brainstorming → spec → plan → subagent-driven build; every gate
default-OFF; the operator remains the gate for merge + activation.

### Entry conditions (verify before brainstorming)

1. **Phase C merged + live-wired.** `OPENCLAW_LIVE_UNIVERSE_RESOLVER=1` in prod
   `.env`, the A4 clamp DELETED (`grep -rn universe_clamp src/` → only history),
   `OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE=1`. If the clamp still exists or the
   resolver gate is OFF, Phase C's flip is incomplete — STOP and finish the
   Phase C runbook first (D3's mint-time ladder needs live per-strategy
   universes; D2's tier menu needs real broad-tier data fetched by C2).
2. **Ladder operational + adoptions flowing.** `universe_ladder_runs` has a
   drained full run; `strategy_universe_recommendations` rows with
   `candidate_set_id LIKE 'sp7b-%'` show adoptions (auto or operator). The
   mint-time ladder (D3) reuses this exact machinery.
3. `python3 -m system_checks --check universe_shadow_parity` → PASS (or WARN
   classified all-sub-floor per runbook §3). If FAIL → the live resolver path
   has a code bug; fix before building on it.

### What Phase D delivers (parent spec §6 + Phase-C carry-forward backlog)

- **D1 — options_eligible chain-probe producer (HARD prerequisite, carry-forward).**
  `OPTIONS_ELIGIBILITY_CACHE = data/.cache/options_eligibility.json`
  (`run_ticker_metadata_step.py:13`) is READ by the metadata writer but has
  **zero writers repo-wide** (verified 2026-06-08), so `options_eligible` is
  FALSE for all ~447k metadata rows. Consequence: Phase C's
  `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE` gate is dead-on-arrival (logs
  "gate ON but 0 options-eligible" → falls back to universe_config). D1 builds
  the producer (Alpaca chain-probe per liquid name → cache → metadata snapshot
  column), then the archive gate becomes real. Without D1, three things stay
  inert: the options archive scoping, the PaperHunter `options_eligible_only`
  predicate, and any option-strategy tier mint.
- **D2 — PaperHunter tier-aware mint menu.** `paperhunter.md` §5 currently
  offers "12 vetted predicates" (the cap-independent + ADR/OTC set); the 4
  `LADDER_TIER_PREDICATES` (`liquid_tradable`, `tier_r1000`, `tier_r3000`,
  `tier_liquid` in `universe_default.py:98`) are ADOPTION-ONLY, not mintable.
  D2: (a) `{{AVAILABLE_DATA}}` gains per-tier descriptors (ticker counts,
  per-layer coverage spans — now real post-C2 fetch), (b) §5 menu exposes the
  tier predicates as real mint choices with guidance. Keep the equity-only
  caveat (etp/crypto/option strategies emit `inferred_universe_filter: null`).
  The orchestrator whitelist (`CANDIDATE_PREDICATES`, 16 entries) already
  validates them — confirm the mint→coder→register path threads tier refs.
- **D3 — mint-time ladder.** A new strategy runs the tier ladder ONCE at the
  promotion gate (`saturday_brain.js` → `promoteHighBucket` →
  `research_candidates`): candidate stage uses the PaperHunter-inferred
  predicate; the ladder confirms/corrects before live. Once-per-strategy
  invariant holds for mints (mirrors the adoption ladder's cadence).
  **Budget risk** — saturday-brain has a ~6 h ceiling; a per-mint ladder
  (4 tiers × precompute) could blow it. Design the ladder call to be
  bounded/async or gated to a separate window; do NOT inline a multi-backtest
  sweep into the Saturday rate loop without a budget guard.
- **D4 — legacy universe-recs decommission.** Retire the Sat 20:00 ET Opus grid
  mode: `run_mastermind.js --mode universe-recs` (gate `OPENCLAW_UNIVERSE_RECS`),
  the `openclaw-universe-recs.{service,timer}` units (`docs/universe-recs.*`),
  and the `universe_recs_health` system_check / doctor freshness. Phase B's
  tier-ladder already supersedes it (same adopt/reaction/`strategy_universe_recommendations`/
  dashboard plumbing) — D4 removes the now-redundant Opus grid, NOT the shared
  adoption plumbing Phase B reuses. Disable the timer + remove the mode; keep
  `lifecycle_universe_adoption` and `strategy_universe_recommendations`.
- **D5 — Universe dashboard page.** Add to the USER-facing dashboard
  (`src/channels/api/server.js`, :3000 → nginx :80 — NOT the :7870 control
  room): per-strategy adopted tier, ladder scores, last-run date, a Recompute
  button (triggers a single-strategy ladder run), envelope-size trend.

### Phase-C-vintage facts Phase D must know (verified 2026-06-08)

- **B0 re-bound backlog (optional, improves tier accuracy).** Historical
  `in_sp500` ceiling ≈476 (Wikipedia-CSV membership gap); the ladder's
  historical tiers undercount pre-2024 SP500 churn. Fix = source fuller SP500
  membership history, then re-run `scripts/sp7_b0_repair_metadata.py --months
  --start 2021-01-01 --end 2026-05-31 --resume` (idempotent UPDATE-supersede,
  derived cols `in_sp500/in_r1000/in_r3000/market_cap`). Not blocking D1-D5;
  improves backtest tier fidelity.
- **Fundamentals coverage-sentinel hygiene (optional).** Post-C2, adopted names
  with no FMP fundamentals re-fetch EVERY cycle (no `data_coverage` row written
  on empty returns — pre-existing, newly reachable via the adopted-union
  scope). Bounded by the 250/day FMP quota. A coverage-sentinel write on
  empty-return would converge it. Low priority; surface if quota WARNs appear.
- **CoverageIndex memory.** `CoverageIndex.from_parquet` is a ~1.65 GB transient
  (~5 s) per process on the 8 GB no-swap box. Any D-phase code building a
  resolver in-cycle (mint ladder, Recompute button) inherits this — keep builds
  sequential, never concurrent (reference_vps_two_core_cpu).
- **Live universe shape.** `universe_config` active equity ≈5,082 is a strict
  superset of the floored resolver envelope (~503) today; broad-tier adoptions
  are what widen the resolver side. The C2 envelope flip was delta-0 until a
  `tier_*` adoption lands — D2/D3 making tiers mintable is what finally exercises
  the wide path.
- Predicates: `CANDIDATE_PREDICATES` 16 entries, `LADDER_TIER_PREDICATES` 4
  (`src/strategies/universe_default.py:75,98`). PaperHunter menu = 12 today.
- Box: 2-core/8 GB/no-swap; chunked per-file pytest; sequential subagents;
  nightly heavy work inside 01:00–13:00 UTC Mon–Fri only (the ladder owns it).

### Key file map

| What | Where |
|---|---|
| Parent spec / C spec / C plan / C runbook | docs/superpowers/specs/2026-06-04-sp7-universe-expansion-design.md · 2026-06-07-sp7-phase-c-live-wiring-design.md · plans/2026-06-07-sp7-phase-c-live-wiring.md · docs/sp7-phase-c-runbook.md |
| options_eligible cache (READ; no producer) | src/pipeline/run_ticker_metadata_step.py:13 · data/.cache/options_eligibility.json |
| PaperHunter mint menu | src/agent/prompts/subagents/paperhunter.md (§5 ~line 133, {{AVAILABLE_DATA}}) |
| Predicates | src/strategies/universe_default.py (CANDIDATE_PREDICATES:75, LADDER_TIER_PREDICATES:98) |
| Mint / promotion gate | src/agent/curators/saturday_brain.js · run_mastermind.js (--mode universe-recs to retire) |
| Ladder machinery (reuse for mint) | src/backtest/{universe_grid_cli,universe_ladder_selection,universe_ladder_recs,precomputed_resolver}.py · scripts/run_universe_ladder.py · scripts/build_tier_membership.py (CoverageIndex now src/strategies/coverage_index.py) |
| B0 repair (backlog) | scripts/sp7_b0_repair_metadata.py |
| Legacy units to retire | docs/universe-recs.{service,timer} · system_checks universe_recs_health · doctor universe_recs_freshness |
| User dashboard (D5 page) | src/channels/api/server.js (:3000 → nginx :80) |
| Live wiring (C1/C2/C3 — Phase D builds on) | src/execution/{engine.py,live_universe.py} · src/pipeline/collector.js (applyResolverEnvelope, adoptedUnionScope) · src/strategies/universe_resolver.py (envelope_universe, --envelope) |
| Memory anchors | project_sp7_phase_c_live_wiring · project_sp7_phase_b_tier_ladder · project_sp7_universe_expansion · feedback_spec_plan_codebase_grounding · reference_vps_two_core_cpu |

### SP-7 definition-of-done (this phase closes the sub-project)

Parent spec §9 Phase D acceptance: **first new mint passes the mint-time
ladder; legacy Opus grid removed; Universe dashboard page live.** Add: D1's
options_eligible producer ships and `OPENCLAW_OPTIONS_ARCHIVE_RESOLVER_UNIVERSE`
becomes non-inert. When D1-D5 land + operator-activated, SP-7 (A→D) is COMPLETE.

### Conventions

TDD per task; subagent-driven with two-stage review (spec then quality);
spec → operator approval → plan → grep-verify every named symbol before
committing the plan → build; gates default-OFF with byte-identical-when-OFF
tests; shadow/dry-run proof before any live flip; never DELETE from master
data; the operator remains the gate for merge + activation.
